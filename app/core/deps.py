"""
Dependency: extract and validate API key from Authorization header.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import anyio
from fastapi import Depends, Header, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session, func, select, update
from starlette.concurrency import run_in_threadpool

from app.core.auth import AccountDisabledError
from app.core.config import get_cloud_daily_limit, get_concurrency_settings
from app.core.database import engine
from app.core.logger import logger
from app.core.timeutil import local_day_start_utc, seconds_until_local_midnight
from app.models.schema import UsageLog, User
from app.services import auth_throttle, ip_allowlist, metrics

_bearer = HTTPBearer(auto_error=False)


def _enforce_daily_limit() -> bool:
    """Whether to hard-block requests once the daily spending limit is hit.

    Defaults to True (current behavior). Set ``ENFORCE_DAILY_LIMIT=false``
    in the gateway env to flip to "log-only" mode, where overages are
    warned about in the log but no 429 is raised. Useful during
    onboarding / load testing / when billing isn't configured yet.

    Read at call time rather than import time so you can toggle it
    without restarting the whole worker (in practice env vars don't
    live-reload, but this at least doesn't cache the value anywhere).
    """
    return os.getenv("ENFORCE_DAILY_LIMIT", "true").lower() not in (
        "false",
        "0",
        "no",
        "off",
    )


def _mask(key: str) -> str:
    """Return a short redacted preview of an API key for log messages.

    Shows the first 8 characters and the length so you can diagnose
    "client sent the wrong key" vs "client sent no key" vs "key got
    mangled to a different length" without ever logging the full secret.
    """
    if not key:
        return "<empty>"
    return f"{key[:8]}… (len={len(key)})"


def _check_daily_limit(session: Session, user: User) -> None:
    """Reject if user has exceeded their daily spending limit.

    Every account has a limit: there is no per-user "unlimited". A limit of
    0 means a $0 budget, so every request is refused. The only escape hatch
    is gateway-wide: env ``ENFORCE_DAILY_LIMIT=false`` → soft mode (log only).

    In soft mode we still compute the cost and emit a WARNING so an
    operator tailing the log sees the overage — we just don't 429 it.
    """
    today_start = local_day_start_utc()
    stmt = (
        select(func.coalesce(func.sum(UsageLog.cost_usd), 0))
        .where(UsageLog.user_id == user.id)
        .where(UsageLog.created_at >= today_start)
    )
    today_cost = float(session.exec(stmt).one())
    if today_cost < user.daily_limit_usd:
        return

    if _enforce_daily_limit():
        metrics.observe_rejection("daily_limit")
        if user.daily_limit_usd <= 0:
            detail = "Daily spending limit is $0 for this account. Contact your administrator."
        else:
            detail = f"Daily spending limit (${user.daily_limit_usd}) exceeded. Today: ${today_cost:.4f}."
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=detail,
            headers={"Retry-After": str(seconds_until_local_midnight())},
        )
    # Soft mode: let the request through but make the overage visible.
    logger.warning(
        "Daily limit exceeded (soft mode) | user={} limit=${} today=${:.4f}",
        user.username, user.daily_limit_usd, today_cost,
    )


_CLOUD_LABEL = "Cloud (Azure + Bedrock)"
_CLOUD_BACKENDS: tuple[str, ...] = ("azure", "bedrock")


def effective_cloud_limit(user: User) -> float | None:
    """The user's cloud cap: their own ``cloud_daily_limit_usd`` when set
    (0 included), else the gateway-wide ``[app].cloud_daily_limit_usd``.
    None = no cloud cap. Only consulted for users who may reach a cloud
    backend: without access a request never gets this far."""
    if user.cloud_daily_limit_usd is not None:
        return max(0.0, user.cloud_daily_limit_usd)
    return get_cloud_daily_limit()


def _check_backend_daily_limit(
    session: Session,
    user: User,
    backend: str,
    *,
    allow_fallback: bool = False,
) -> bool:
    """Enforce the cloud cap for a request bound to ``backend``.

    One cap covers Azure and Bedrock together (``effective_cloud_limit``):
    the user's own ``cloud_daily_limit_usd`` when set, else the gateway-wide
    ``[app].cloud_daily_limit_usd``. Today's spend on both backends counts
    against it, so a user splits it between them however they like. Cloud spend also counts toward
    the overall ``daily_limit_usd`` (checked in ``_check_daily_limit``); this
    is not a parallel budget. No cap configured = cloud is bounded by the
    overall limit only. ``0`` is a $0 cap: cloud is refused (or, with
    ``allow_fallback``, served on-prem).

    Returns True when the request may proceed to the cloud backend.

    When the cap is exhausted:
      - ``allow_fallback=False`` (the dedicated ``/azure/v1/*`` / ``/aws/v1/*``
        surfaces, and ``/v1/*`` unless the operator enabled the fallback) →
        HTTP 429 with ``Retry-After`` set to the next local midnight.
      - ``allow_fallback=True`` → returns False so the caller can serve the
        request from the on-prem vLLM path instead. Nothing is raised: the
        overall daily limit was already enforced at auth, so "on-prem is
        exhausted too" is the ordinary 429 the caller never reaches here.

    Honors the same ``ENFORCE_DAILY_LIMIT`` soft-mode escape hatch as the
    overall check: in soft mode the overage is logged and the request stays
    on the cloud backend (there is no 429 for the fallback to replace).
    """
    if backend not in _CLOUD_BACKENDS:  # pragma: no cover - programming error
        raise ValueError(f"unknown cloud backend {backend!r}")
    limit = effective_cloud_limit(user)
    if limit is None:
        return True

    today_start = local_day_start_utc()
    stmt = (
        select(func.coalesce(func.sum(UsageLog.cost_usd), 0))
        .where(UsageLog.user_id == user.id)
        .where(UsageLog.created_at >= today_start)
        .where(UsageLog.backend.in_(_CLOUD_BACKENDS))
    )
    spent = float(session.exec(stmt).one())
    if spent < limit:
        return True

    label = _CLOUD_LABEL
    if _enforce_daily_limit():
        if allow_fallback:
            return False
        metrics.observe_rejection("cloud_limit")
        if limit <= 0:
            detail = f"{label} daily spending limit is $0."
        else:
            detail = (
                f"{label} daily spending limit (${limit}) exceeded. "
                f"Today ({label}): ${spent:.4f}."
            )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=detail,
            headers={"Retry-After": str(seconds_until_local_midnight())},
        )
    logger.warning(
        "{} daily limit exceeded (soft mode) | user={} limit=${} today=${:.4f}",
        label, user.username, limit, spent,
    )
    return True


def _check_azure_daily_limit(session: Session, user: User) -> None:
    """Reject (429) if the user's cloud cap is spent, for an Azure request.
    See ``_check_backend_daily_limit``."""
    _check_backend_daily_limit(session, user, "azure")


def _check_bedrock_daily_limit(session: Session, user: User) -> None:
    """Reject (429) if the user's cloud cap is spent, for a Bedrock request.
    See ``_check_backend_daily_limit``."""
    _check_backend_daily_limit(session, user, "bedrock")


def check_cloud_budget(user: User, backend: str, *, allow_fallback: bool = False) -> bool:
    """Standalone cloud-cap check for call sites without a request-scoped
    session — i.e. the unified ``/v1/*`` handlers, which only learn the request
    is cloud-bound after peeking the model alias. Opens a short-lived session;
    blocking, so async callers should wrap it in ``run_in_threadpool``.

    Returns True to proceed to ``backend``; False (only with
    ``allow_fallback=True``) when the cloud cap is exhausted and the request
    should be served on-prem instead; raises HTTP 429 otherwise.
    """
    with Session(engine) as session:
        return _check_backend_daily_limit(
            session, user, backend, allow_fallback=allow_fallback
        )


def ensure_azure_budget(user: User) -> None:
    """Raise HTTP 429 when the user's cloud cap is exhausted
    (``check_cloud_budget(user, "azure")`` without fallback)."""
    check_cloud_budget(user, "azure")


def ensure_bedrock_budget(user: User) -> None:
    """Raise HTTP 429 when the user's cloud cap is exhausted
    (same shape/contract as ``ensure_azure_budget``)."""
    check_cloud_budget(user, "bedrock")


# ── API key last use ──
# users.api_key_last_used_at feeds access reviews (stale keys), so it needs
# hour precision, not per-request writes: each worker writes a user's row at
# most once per KEY_USE_RESOLUTION_S, and the UPDATE is conditional so
# several workers don't rewrite it within the same window.
KEY_USE_RESOLUTION_S = 3600
_key_use_lock = threading.Lock()
_key_use_last: dict[int, float] = {}


def _key_use_due(user_id: int | None, now: float | None = None) -> bool:
    if user_id is None:
        return False
    now = time.monotonic() if now is None else now
    with _key_use_lock:
        last = _key_use_last.get(user_id)
        if last is not None and now - last < KEY_USE_RESOLUTION_S:
            return False
        _key_use_last[user_id] = now
        return True


def _record_key_use(user_id: int) -> None:
    """Stamp ``api_key_last_used_at``. A side effect: never fails the request."""
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        stale = now - timedelta(seconds=KEY_USE_RESOLUTION_S)
        col = User.api_key_last_used_at
        with Session(engine) as session:
            session.exec(
                update(User)
                .where(User.id == user_id, (col.is_(None)) | (col < stale))  # type: ignore[union-attr,operator]
                .values(api_key_last_used_at=now)
            )
            session.commit()
    except Exception as exc:
        logger.warning("Recording API key use failed | user_id={} error={}: {}", user_id, type(exc).__name__, exc)


def reset_key_use_cache() -> None:
    """Forget which users were stamped recently (tests)."""
    with _key_use_lock:
        _key_use_last.clear()


def _authenticate(
    credentials: HTTPAuthorizationCredentials | None,
    x_api_key: str | None,
    *,
    check_daily_limit: bool,
    request: Request | None = None,
) -> User:
    # NOTE: request-header capture for observability lives in
    # RequestMetaMiddleware (pure ASGI), NOT here — a contextvar written in this
    # sync dependency runs in a threadpool copy and is lost at the _log_usage seam.
    #
    # This dependency deliberately opens its OWN short-lived session instead of
    # taking ``Depends(get_session)``. A ``yield`` dependency's exit code runs
    # only after the response has been sent (FastAPI >= 0.118 restored that
    # ordering, streaming responses included), so a request-scoped session
    # would keep its pooled PostgreSQL connection checked out — idle in
    # transaction — for the whole lifetime of a streaming completion. With
    # many concurrent Claude Code streams that exhausted the QueuePool
    # (``QueuePool limit of size 20 overflow 15 reached``) and every new
    # request then failed at auth. The session below is closed before this
    # function returns, so a stream never holds a DB connection.
    # Support both `Authorization: Bearer <key>` (OpenAI-style) and
    # `x-api-key: <key>` (Anthropic-style) for client compatibility.
    #
    # When both headers are present we try each in turn rather than picking
    # one and giving up — this matters for Claude Code, which may send both
    # headers when ANTHROPIC_AUTH_TOKEN and ANTHROPIC_API_KEY are configured
    # (or when a corporate proxy injects its own Authorization header). The
    # user should still get in as long as *some* credential they sent is
    # valid for the gateway.
    candidates: list[str] = []
    if credentials is not None and credentials.credentials:
        candidates.append(credentials.credentials)
    if x_api_key and x_api_key not in candidates:
        candidates.append(x_api_key)

    if not candidates:
        logger.warning(
            "Auth rejected: no credentials (no Authorization header and no x-api-key)",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Provide Authorization: Bearer <key> or x-api-key header.",
        )

    # An address that has tried many different invalid keys is refused before
    # any lookup, valid key or not (app/services/auth_throttle.py).
    client_addr = request.client.host if request is not None and request.client else None
    wait_s = auth_throttle.retry_after(client_addr)
    if wait_s is not None:
        metrics.observe_rejection("auth_throttle")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed authentication attempts from this address. Try again later.",
            headers={"Retry-After": str(wait_s)},
        )

    # Recorded after the session is closed (a separate short write), and even
    # when the budget check below refuses the request: the key was valid.
    touch_id: int | None = None
    try:
        with Session(engine) as session:
            user: User | None = None
            for key in candidates:
                user = session.exec(select(User).where(User.api_key == key)).first()
                if user is not None:
                    break

            if user is None:
                # Log a masked preview of every credential the client sent so
                # operators can tell "wrong key" from "no key" from "mangled key"
                # without leaking the full secret to the log file.
                previews = ", ".join(_mask(k) for k in candidates)
                logger.warning(
                    "Auth rejected: no user found for candidates [{}] | client={}",
                    previews, client_addr or "-",
                )
                auth_throttle.record_failure(client_addr, candidates)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid API key.",
                )
            if user.is_disabled and not user.is_admin:
                # Admin bypass mirrors get_web_user — if an admin row is somehow
                # flagged disabled (only possible via direct DB edit; the toggle
                # endpoint blocks self-disable), they can still call the API to
                # fix the situation. Non-admins always get rejected.
                logger.warning("Auth rejected: user '{}' is disabled", user.username)
                raise AccountDisabledError(user.username)
            # API IP allowlist: needs the account, so it runs here, before the
            # budget check (a refused address is a 403, not a 429). Memory only;
            # a no-op while [app].ip_allowlist_mode is "off".
            if request is not None:
                ip_allowlist.check(request, user)
            if _key_use_due(user.id):
                touch_id = user.id
            if check_daily_limit:
                _check_daily_limit(session, user)
    finally:
        if touch_id is not None:
            _record_key_use(touch_id)
    # ``user`` is now detached: its columns are loaded (SQLModel does not
    # expire on close) and downstream code only ever reads them.
    return user


def get_today_usage(user: User) -> dict:
    """Today's spend and token totals for ``user``, split by backend
    (``stats.get_user_daily_summary``). Short-lived session; blocking."""
    from app.services.stats import get_user_daily_summary

    with Session(engine) as session:
        return get_user_daily_summary(session, user.id)


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> User:
    """API-key auth for billable endpoints: rejects disabled accounts,
    applies the IP allowlist and 429s once today's spend has reached
    ``daily_limit_usd``."""
    return _authenticate(credentials, x_api_key, check_daily_limit=True, request=request)


def get_current_user_unmetered(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> User:
    """Same auth as ``get_current_user`` but without the daily-limit check,
    for read-only account endpoints (``GET /v1/me``) that must still answer
    once the budget is spent — that is exactly when a client asks."""
    return _authenticate(credentials, x_api_key, check_daily_limit=False, request=request)


def require_azure_access(
    user: User = Depends(get_current_user),
) -> User:
    """Dependency for /azure/v1/* endpoints — additionally checks the Azure
    access flag and the cloud daily cap.

    Uses ``ensure_azure_budget`` (short-lived session) rather than a
    request-scoped ``Depends(get_session)`` for the same reason as
    ``get_current_user``: the connection must not outlive the auth step."""
    if not user.can_use_azure and not user.is_admin:
        logger.warning("Azure access denied for user '{}'", user.username)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Azure access not granted. Contact your administrator.",
        )
    ensure_azure_budget(user)
    return user


def require_bedrock_access(
    user: User = Depends(get_current_user),
) -> User:
    """Dependency for /aws/v1/* endpoints — additionally checks the Bedrock
    access flag and the cloud daily cap (short-lived
    session via ``ensure_bedrock_budget``, see ``get_current_user``)."""
    if not user.can_use_bedrock and not user.is_admin:
        logger.warning("Bedrock access denied for user '{}'", user.username)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bedrock access not granted. Contact your administrator.",
        )
    ensure_bedrock_budget(user)
    return user


# ── Per-user concurrency limit ──
#
# Wraps an auth dependency in a ``yield`` dependency that holds a lease in
# ``concurrency_leases`` (app/services/concurrency.py) while the request is in
# flight. The exit runs only after the response has been sent — streaming
# bodies included (FastAPI >= 0.118) — the same ordering that made a
# request-scoped DB session pin a pool connection for a whole stream. Here
# that ordering is the point, and no connection is held in between: acquire
# and release are separate short transactions.

_CONCURRENCY_RETRY_AFTER_S = 5


@asynccontextmanager
async def _concurrency_slot(user: User, endpoint: str) -> AsyncIterator[None]:
    """Hold one concurrency slot for ``user`` around the request.

    - mode ``off``, admins, and ``concurrency_waived`` users: no-op.
    - ``monitor``: the lease is always taken; going over the limit logs a
      WARNING and the request is served.
    - ``enforce``: over the limit → 429 with ``Retry-After``.
    - the lease store failing (DB trouble) never fails the request: it is
      logged and the request proceeds unlimited (fail-open), so the limiter
      can't become an outage of its own.
    """
    mode, limit = get_concurrency_settings()
    if mode == "off" or user.is_admin or user.concurrency_waived:
        yield
        return

    from app.services import concurrency

    try:
        lease_id, active = await run_in_threadpool(
            concurrency.acquire, user.id, limit, endpoint, enforce=(mode == "enforce"),
        )
    except Exception as exc:
        logger.warning(
            "Concurrency check failed, allowing request | user={} endpoint={} error={}: {}",
            user.username, endpoint, type(exc).__name__, exc,
        )
        yield
        return

    if active >= limit:
        logger.warning(
            "Concurrency limit {} | user={} in_flight={} limit={} endpoint={}",
            "hit" if lease_id is None else "exceeded (monitor)",
            user.username, active + (0 if lease_id is None else 1), limit, endpoint,
        )
        if lease_id is None:
            metrics.observe_rejection("concurrency_limit")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Too many concurrent requests: {active} already in flight "
                    f"(limit {limit}). Retry when one finishes."
                ),
                headers={"Retry-After": str(_CONCURRENCY_RETRY_AFTER_S)},
            )

    try:
        yield
    finally:
        # Shielded: when the client disconnects mid-stream, Starlette cancels
        # the response task group and anyio re-delivers that cancellation at
        # every await — an unshielded release would be cancelled before its
        # DELETE ran and the slot would stay taken until the TTL expired.
        with anyio.CancelScope(shield=True):
            try:
                await run_in_threadpool(concurrency.release, lease_id)
            except Exception as exc:
                logger.warning(
                    "Concurrency lease release failed (expires on its own) | user={} lease={} error={}: {}",
                    user.username, lease_id, type(exc).__name__, exc,
                )


def _limited(auth_dependency: Callable[..., User]) -> Callable[..., AsyncIterator[User]]:
    """Build a dependency that authenticates via ``auth_dependency`` and then
    holds a concurrency slot until the response has been sent."""

    async def dependency(
        request: Request,
        user: User = Depends(auth_dependency),
    ) -> AsyncIterator[User]:
        async with _concurrency_slot(user, request.url.path):
            yield user

    dependency.__name__ = f"limited_{auth_dependency.__name__}"
    return dependency


# Inference endpoints only (chat / responses / messages). Cheap metadata calls
# — count_tokens, tokenize, render, models — and the short single-pass
# embeddings / rerank / systemone keep the plain auth dependencies.
limited_current_user = _limited(get_current_user)
limited_azure_access = _limited(require_azure_access)
limited_bedrock_access = _limited(require_bedrock_access)
