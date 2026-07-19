"""
Dependency: extract and validate API key from Authorization header.
"""

from __future__ import annotations

import os

from fastapi import Depends, Header, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session, func, select

from app.core.auth import AccountDisabledError
from app.core.database import engine, get_session
from app.core.logger import logger
from app.core.timeutil import local_day_start_utc
from app.models.schema import UsageLog, User

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

    Two escape hatches:
      - ``user.daily_limit_usd <= 0`` → treated as unlimited for this user
      - env ``ENFORCE_DAILY_LIMIT=false`` → gateway-wide soft mode (log only)

    In soft mode we still compute the cost and emit a WARNING so an
    operator tailing the log sees the overage — we just don't 429 it.
    """
    # Per-user unlimited: 0 (or negative) disables the check entirely.
    if user.daily_limit_usd <= 0:
        return

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
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Daily spending limit (${user.daily_limit_usd}) exceeded. Today: ${today_cost:.4f}.",
        )
    # Soft mode: let the request through but make the overage visible.
    logger.warning(
        "Daily limit exceeded (soft mode) | user={} limit=${} today=${:.4f}",
        user.username, user.daily_limit_usd, today_cost,
    )


def _check_azure_daily_limit(session: Session, user: User) -> None:
    """Reject if the user has exceeded their Azure-specific daily sub-limit.

    Azure spend always counts toward the overall ``daily_limit_usd`` (checked
    in ``_check_daily_limit``); this additionally caps the Azure portion on
    its own. ``azure_daily_limit_usd`` of ``None`` or ``<= 0`` means "no
    separate Azure cap" — the default for every user, preserving pre-feature
    behavior exactly.

    Honors the same ``ENFORCE_DAILY_LIMIT`` soft-mode escape hatch as the
    overall check.
    """
    limit = user.azure_daily_limit_usd
    if limit is None or limit <= 0:
        return

    today_start = local_day_start_utc()
    stmt = (
        select(func.coalesce(func.sum(UsageLog.cost_usd), 0))
        .where(UsageLog.user_id == user.id)
        .where(UsageLog.created_at >= today_start)
        .where(UsageLog.backend == "azure")
    )
    azure_cost = float(session.exec(stmt).one())
    if azure_cost < limit:
        return

    if _enforce_daily_limit():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Azure daily spending limit (${limit}) exceeded. "
                f"Today (Azure): ${azure_cost:.4f}."
            ),
        )
    logger.warning(
        "Azure daily limit exceeded (soft mode) | user={} limit=${} today=${:.4f}",
        user.username, limit, azure_cost,
    )


def ensure_azure_budget(user: User) -> None:
    """Standalone Azure sub-limit check for call sites without a request-scoped
    session — i.e. the unified ``/v1/*`` handlers, which only learn the request
    is Azure-bound after peeking the model alias. Opens a short-lived session;
    blocking, so async callers should wrap it in ``run_in_threadpool``.

    Raises HTTP 429 when the user's Azure sub-limit is exhausted.
    """
    with Session(engine) as session:
        _check_azure_daily_limit(session, user)


def _check_bedrock_daily_limit(session: Session, user: User) -> None:
    """Reject if the user has exceeded their Bedrock-specific daily sub-limit.

    Bedrock spend always counts toward the overall ``daily_limit_usd``; this
    additionally caps the Bedrock portion on its own. ``NULL`` or ``<= 0``
    means "no separate Bedrock cap" (the default). Honors the same
    ``ENFORCE_DAILY_LIMIT`` soft-mode escape hatch as the overall check.
    """
    limit = user.bedrock_daily_limit_usd
    if limit is None or limit <= 0:
        return

    today_start = local_day_start_utc()
    stmt = (
        select(func.coalesce(func.sum(UsageLog.cost_usd), 0))
        .where(UsageLog.user_id == user.id)
        .where(UsageLog.created_at >= today_start)
        .where(UsageLog.backend == "bedrock")
    )
    bedrock_cost = float(session.exec(stmt).one())
    if bedrock_cost < limit:
        return

    if _enforce_daily_limit():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Bedrock daily spending limit (${limit}) exceeded. "
                f"Today (Bedrock): ${bedrock_cost:.4f}."
            ),
        )
    logger.warning(
        "Bedrock daily limit exceeded (soft mode) | user={} limit=${} today=${:.4f}",
        user.username, limit, bedrock_cost,
    )


def ensure_bedrock_budget(user: User) -> None:
    """Standalone Bedrock sub-limit check for the unified ``/v1/*`` handlers
    (same shape/contract as ``ensure_azure_budget``)."""
    with Session(engine) as session:
        _check_bedrock_daily_limit(session, user)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
    session: Session = Depends(get_session),
) -> User:
    # NOTE: request-header capture for observability lives in
    # RequestMetaMiddleware (pure ASGI), NOT here — a contextvar written in this
    # sync dependency runs in a threadpool copy and is lost at the _log_usage seam.
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
        logger.warning("Auth rejected: no user found for candidates [{}]", previews)
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
    _check_daily_limit(session, user)
    return user


def require_azure_access(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> User:
    """Dependency for /azure/v1/* endpoints — additionally checks the Azure
    access flag and the per-user Azure daily sub-limit."""
    if not user.can_use_azure and not user.is_admin:
        logger.warning("Azure access denied for user '{}'", user.username)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Azure access not granted. Contact your administrator.",
        )
    _check_azure_daily_limit(session, user)
    return user


def require_bedrock_access(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> User:
    """Dependency for /aws/v1/* endpoints — additionally checks the Bedrock
    access flag and the per-user Bedrock daily sub-limit."""
    if not user.can_use_bedrock and not user.is_admin:
        logger.warning("Bedrock access denied for user '{}'", user.username)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bedrock access not granted. Contact your administrator.",
        )
    _check_bedrock_daily_limit(session, user)
    return user
