"""
Billing and observability: what the gateway records about a request.

``_log_usage`` is the one seam every billable request passes through, whatever
backend served it (vLLM, Azure, Bedrock): it prices the request, writes the
``usage_logs`` row, emits the Langfuse generation, feeds the Prometheus
metrics and the slow-request line. ``_log_error`` is its counterpart for a
request that failed (never billed). Debug and metadata calls (``render``,
``tokenize``, ``count_tokens``) must not reach either.

Nothing here may fail or delay a request: each side effect is wrapped, and
the database write and the Langfuse call run off the event loop
(``_submit_background``).
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import os
from decimal import Decimal
from typing import Any

from sqlmodel import Session

from app.core.config import MODEL_ROUTING, PRICING_MAP
from app.core.database import engine
from app.core.logger import logger
from app.models.schema import UsageLog, User, via_service_account, via_user_id
from app.services import metrics
from app.services.observability import (
    GenerationRecord,
    capture_io_enabled,
    get_langfuse,
    get_request_meta,
    record_generation,
    request_latency_ms,
    request_timing,
)


def _calc_cost_breakdown(
    route: dict[str, Any],
    model_type: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> dict[str, Decimal]:
    """Return the per-component cost: ``{input, output, cache_read_input_tokens,
    cache_creation_input_tokens, total}``.

    Same pricing logic / priority as ``_calc_cost`` (per-model override on
    `route` → per-type → default), but split out so callers (Langfuse
    ``costDetails``) can report the breakdown. ``input`` is the *full* billable
    input cost (uncached portion at full rate + cached portion at the
    discounted rate + cache-write portion at the cache-write rate);
    ``cache_read_input_tokens`` / ``cache_creation_input_tokens`` are those
    portions' costs on their own, for visibility (the latter only when a
    cache-write price applied). ``total`` matches ``_calc_cost`` exactly.
    """
    cached_price = None
    write_price = None
    if route and "input_price_per_1m" in route and "output_price_per_1m" in route:
        prices: dict[str, Any] = route
    else:
        prices = PRICING_MAP.get(model_type, PRICING_MAP.get("_default", {}))
    inp_price = Decimal(str(prices.get("input_price_per_1m", 0.0)))
    out_price = Decimal(str(prices.get("output_price_per_1m", 0.0)))
    if "cached_input_price_per_1m" in prices:
        cached_price = Decimal(str(prices["cached_input_price_per_1m"]))
    if "cache_write_input_price_per_1m" in prices:
        write_price = Decimal(str(prices["cache_write_input_price_per_1m"]))

    # Clamp both portions into the input: cached first, cache writes into
    # what is left — defends against a downstream reporting more cached or
    # written tokens than the prompt held (shouldn't happen, but cheap).
    cached = max(0, min(cached_tokens, input_tokens))
    written = max(0, min(cache_write_tokens, input_tokens - cached))
    cached_cost = Decimal("0")
    write_cost = Decimal("0")
    full_rate = input_tokens
    if cached and cached_price is not None:
        cached_cost = cached * cached_price / 1_000_000
        full_rate -= cached
    if written and write_price is not None:
        write_cost = written * write_price / 1_000_000
        full_rate -= written
    input_cost = full_rate * inp_price / 1_000_000 + cached_cost + write_cost
    output_cost = output_tokens * out_price / 1_000_000
    breakdown = {
        "input": input_cost,
        "output": output_cost,
        "cache_read_input_tokens": cached_cost,
    }
    if written and write_price is not None:
        breakdown["cache_creation_input_tokens"] = write_cost
    breakdown["total"] = input_cost + output_cost
    return breakdown


def _calc_cost(
    route: dict[str, Any],
    model_type: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Decimal:
    """Total request cost. Lookup priority: per-model override on `route` →
    per-type → default. Thin wrapper over ``_calc_cost_breakdown`` (kept for the
    existing callers and its established behaviour).

    `cached_tokens` (a subset of `input_tokens` that hit a prompt cache) is
    billed at `cached_input_price_per_1m` when that price is configured;
    `cache_write_tokens` (another subset of `input_tokens`: the prompt
    written to the cache, which Anthropic models charge extra for) at
    `cache_write_input_price_per_1m`. Without the matching price, or with a
    count of 0, those tokens are charged at the full input price (the vLLM
    path never passes either, so its behaviour is unchanged).
    """
    return _calc_cost_breakdown(
        route, model_type, input_tokens, output_tokens, cached_tokens,
        cache_write_tokens,
    )["total"]


def _log_usage_sync(
    user_id: int,
    username: str,
    model: str,
    model_type: str,
    input_tokens: int,
    output_tokens: int,
    cost: Decimal,
    endpoint: str,
    backend: str = "vllm",
    overflow: bool = False,
    via_user_id: int = 0,
) -> None:
    """Synchronous DB write — meant to run in a thread via run_in_executor."""
    try:
        with Session(engine) as session:
            log = UsageLog(
                user_id=user_id,
                model=model,
                model_type=model_type,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                endpoint=endpoint,
                backend=backend,
                overflow=overflow,
                via_user_id=via_user_id,
            )
            session.add(log)
            session.commit()
            # No post-write "today" SUM: the next request's auth check
            # (deps._check_daily_limit) already enforces the limit, and a
            # second aggregate per request only fed a log line.
        logger.info(
            "Usage | user={} model={} in={} out={} cost=${}",
            username, model, input_tokens, output_tokens, cost,
        )
    except Exception as exc:
        logger.error("Failed to log usage for user={}: {}", username, exc)


def _emit_observation(
    user: User,
    model: str,
    model_type: str,
    input_tokens: int,
    output_tokens: int,
    endpoint: str,
    route: dict[str, Any],
    cached_tokens: int,
    backend: str,
    breakdown: dict[str, Decimal],
    output_payload: Any = None,
    timing: dict[str, Any] | None = None,
    cache_write_tokens: int = 0,
) -> None:
    """Build a GenerationRecord and hand it to Langfuse. No-op when unconfigured."""
    # Short-circuit before building anything when Langfuse is off (cached after
    # the first call) — keeps the unconfigured request path overhead at zero.
    if get_langfuse() is None:
        return
    meta = get_request_meta()
    # `input` already includes the cached and cache-write tokens, so `total`
    # is explicit: without it Langfuse sums every key and counts them twice.
    usage_details = {"input": input_tokens, "output": output_tokens}
    if cached_tokens:
        usage_details["cache_read_input_tokens"] = cached_tokens
    if cache_write_tokens:
        usage_details["cache_creation_input_tokens"] = cache_write_tokens
    usage_details["total"] = input_tokens + output_tokens
    cost_details = {k: float(v) for k, v in breakdown.items()}
    # empty-turn signal only meaningful for chat-like models (embeddings emit 0
    # output tokens legitimately).
    empty_turn = model_type in ("llm", "vlm") and output_tokens <= 1
    # Phase 2: attach request/response content only when explicitly enabled.
    input_payload = None
    out_payload = None
    if capture_io_enabled():
        input_payload = meta.get("input_payload")
        out_payload = output_payload
    # Off the event loop: with LANGFUSE_CAPTURE_IO on, the SDK JSON-encodes
    # the whole prompt synchronously (~35 ms for a 1 MB Claude Code body).
    rec = GenerationRecord(
        username=user.username,
        user_id=str(user.id),
        endpoint=endpoint,
        backend=backend,
        model_alias=model,
        real_model=route.get("real_model") or route.get("deployment") or model,
        model_type=model_type,
        usage=usage_details,
        cost=cost_details,
        output_tokens=output_tokens,
        empty_turn=empty_turn,
        latency_ms=request_latency_ms(),
        timing=timing,
        user_agent=meta.get("user_agent"),
        x_app=meta.get("x_app"),
        x_title=meta.get("x_title"),
        session_id=meta.get("session_id"),
        display_name=getattr(user, "display_name", None),
        service_account=getattr(via_service_account(user), "username", None),
        input_payload=input_payload,
        output_payload=out_payload,
        guardrail=meta.get("guardrail"),
    )
    guardrail = rec.guardrail
    if guardrail is not None and not isinstance(guardrail, dict) and not guardrail.done():
        # Monitor-mode guardrail still judging alongside this request: emit
        # once the verdict is in (bounded by its timeout). Never wait for it:
        # a blocked executor thread would delay the usage-log writes too.
        def _emit_with_verdict(verdict: dict) -> None:
            try:
                _submit_background(record_generation, dataclasses.replace(rec, guardrail=verdict))
            except Exception as exc:
                logger.warning("observability hook failed: {}", exc)

        guardrail.add_done_callback(_emit_with_verdict)
        return
    _submit_background(record_generation, rec)


def _log_error(
    user: User,
    body: Any,
    error: Any,
    status_code: int,
    model: str,
    endpoint: str,
    model_type: str,
) -> None:
    """Record a FAILED request as a Langfuse generation (level=ERROR +
    statusMessage + request_error score).

    Does NOT write `usage_logs` — a failed call is not billable. No-op for
    Langfuse when unconfigured; never raises. Called at every downstream
    error site (connection failure / non-200) in place of inline logging.
    """
    backend = (
        "azure" if endpoint.startswith("/azure")
        else "bedrock" if endpoint.startswith("/aws")
        else "vllm"
    )
    # Prometheus (no-op unless METRICS_ENABLED; never raises).
    metrics.observe_error(endpoint, backend, model, status_code)
    try:
        if get_langfuse() is None:
            return
        meta = get_request_meta()
        record_generation(GenerationRecord(
            username=user.username,
            user_id=str(user.id),
            endpoint=endpoint,
            backend=backend,
            model_alias=model,
            real_model=model,
            model_type=model_type,
            usage={"input": 0, "output": 0},
            cost={"input": 0.0, "output": 0.0, "total": 0.0},
            output_tokens=0,
            error=f"{status_code}: {str(error)[:500]}",
            is_error=True,
            latency_ms=request_latency_ms(),
            timing=request_timing(),
            user_agent=meta.get("user_agent"),
            x_app=meta.get("x_app"),
            x_title=meta.get("x_title"),
            session_id=meta.get("session_id"),
            display_name=getattr(user, "display_name", None),
            service_account=getattr(via_service_account(user), "username", None),
            guardrail=meta.get("guardrail"),
        ))
    except Exception as exc:  # never let the error-hook break error handling
        logger.warning("observability error-hook failed: {}", exc)


_SLOW_REQUEST_WARN_DEFAULT_S = 120.0


def _slow_request_threshold_s() -> float:
    """Seconds after which a completed request is logged as a WARNING.

    ``SLOW_REQUEST_WARN_S`` in the gateway env; default 120. ``0`` (or any
    non-positive / unparsable value) disables the line. Read at call time so
    the knob can be turned without touching code.
    """
    raw = os.getenv("SLOW_REQUEST_WARN_S", "").strip()
    if not raw:
        return _SLOW_REQUEST_WARN_DEFAULT_S
    try:
        return float(raw)
    except ValueError:
        return _SLOW_REQUEST_WARN_DEFAULT_S


def _warn_if_slow(
    user: User, model: str, model_type: str, endpoint: str, backend: str,
    input_tokens: int, output_tokens: int, latency_ms: float | None,
    timing: dict[str, Any] | None = None,
) -> None:
    """Emit one WARNING line for a request that took longer than the threshold.

    The file sink only records WARNING and above (LOG_LEVEL default), and a
    request that merely took a long time to *succeed* used to leave no trace
    there at all — the ``_log_usage`` INFO line never reaches the file and
    ``usage_logs`` stores no duration. Diagnosing "is the cloud backend slow
    today" therefore needed Langfuse or nginx logs. This line makes slow
    requests greppable (``Slow request``) without enabling INFO logging.
    The measured phases (``request_timing``) are appended, so the line also
    says which part was slow.
    """
    if latency_ms is None:
        return
    threshold = _slow_request_threshold_s()
    if threshold <= 0:
        return
    seconds = latency_ms / 1000.0
    if seconds < threshold:
        return
    phases = "".join(f" {k}={v}" for k, v in (timing or {}).items())
    logger.warning(
        "Slow request | user={} model={} type={} endpoint={} backend={} "
        "duration={:.1f}s input_tokens={} output_tokens={} threshold={:.0f}s{}",
        user.username, model, model_type, endpoint, backend,
        seconds, input_tokens, output_tokens, threshold, phases,
    )


def _warn_if_slow_headers(user: User, model: str, endpoint: str, backend: str) -> None:
    """WARNING when a streaming pre-flight took at least ``SLOW_REQUEST_WARN_S``
    to get response headers back from the downstream.

    ``_warn_if_slow`` only fires when a request *finishes*; a stream whose
    downstream is slow to even answer shows nothing until then. This line is
    emitted the moment headers arrive, so a slow backend is visible while it
    is happening, and its ``waited=`` names the phase — everything before the
    first byte of the response — as opposed to a slow generation.
    """
    try:
        latency_ms = request_latency_ms()
        if latency_ms is None:
            return
        threshold = _slow_request_threshold_s()
        if threshold <= 0:
            return
        seconds = latency_ms / 1000.0
        if seconds < threshold:
            return
        logger.warning(
            "Slow response headers | user={} model={} endpoint={} backend={} "
            "waited={:.1f}s threshold={:.0f}s",
            user.username, model, endpoint, backend, seconds, threshold,
        )
    except Exception as exc:  # a log line must never break the request
        logger.warning("slow-headers hook failed: {}", exc)


# Peak overflow (app/services/overflow.py): the unified /v1 dispatch marks a
# request it is about to send to the cloud in place of a congested on-prem
# model, and the usage row carries the mark. A contextvar, like the request
# meta: the cloud proxies know nothing of it, and the stream that bills later
# runs in a copy of this context. Holds the on-prem alias that was asked for.
_overflow_from: contextvars.ContextVar[str | None] = contextvars.ContextVar("overflow_from", default=None)


def mark_overflow(local_alias: str) -> None:
    """This request is served by the cloud for ``local_alias`` (peak overflow)."""
    _overflow_from.set(local_alias)


def clear_overflow() -> None:
    """The overflow attempt was given up: the request is served on-prem after all."""
    _overflow_from.set(None)


def overflow_from() -> str | None:
    return _overflow_from.get()


def _submit_background(fn: Any, *args: Any) -> None:
    """Run ``fn(*args)`` off the event loop, fire-and-forget: the usage-log
    DB write and the Langfuse generation.

    Neither may add latency to the response, so the call is handed to the
    default thread executor and never awaited — the request returns before
    the row lands. It runs in a copy of the caller's context, so contextvars
    read inside it still see this request. With no running loop (sync
    context, scripts) it runs inline instead.

    This is a seam on purpose: tests patch it to run inline, so an assertion
    on `usage_logs` (or a captured generation) right after a request is
    deterministic instead of racing the executor thread. The dispatch itself
    is covered by tests/test_background_dispatch.py.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        fn(*args)
        return
    loop.run_in_executor(None, contextvars.copy_context().run, fn, *args)


def _log_usage(
    user: User,
    model: str,
    model_type: str,
    input_tokens: int,
    output_tokens: int,
    endpoint: str,
    route: dict[str, Any] | None = None,
    cached_tokens: int = 0,
    backend: str = "vllm",
    output_payload: Any = None,
    cache_write_tokens: int = 0,
) -> None:
    """Fire-and-forget usage logging in a background thread.

    If `route` is omitted, looks up MODEL_ROUTING by alias (vLLM default
    behaviour). Azure callers should pass their AZURE_MODELS entry so any
    per-model price override there is honoured at the DB level.

    `cached_tokens` lets the cloud paths bill prompt-cache hits at the
    discounted `cached_input_price_per_1m`; vLLM callers omit it.
    `cache_write_tokens` (part of `input_tokens`, like `cached_tokens`) bills
    prompt-cache writes at `cache_write_input_price_per_1m`; only the
    Bedrock Anthropic paths report them.

    `backend` ("vllm" | "azure") is persisted on the usage_logs row (so cloud
    vs on-prem spend can be split for reporting and the daily cloud cap)
    and tags the Langfuse generation. Emitting the Langfuse generation happens
    here so every billable path is covered from one seam; it is a no-op when
    Langfuse is unconfigured.

    `output_payload` (assistant text/content) is attached to the Langfuse
    generation only when LANGFUSE_CAPTURE_IO is on (Phase 2); input is read
    from the request-meta contextvar set by the forward_* function.
    """
    if route is None:
        route = MODEL_ROUTING.get(model, {})
    breakdown = _calc_cost_breakdown(
        route, model_type, input_tokens, output_tokens, cached_tokens, cache_write_tokens,
    )
    cost = breakdown["total"]

    # Phase durations for Langfuse and the slow-request line. Read here, on
    # the request's own task, for the same reason as the latency below.
    try:
        timing = request_timing(output_tokens)
    except Exception as exc:  # defensive — timing must never break billing
        logger.warning("request timing failed: {}", exc)
        timing = None

    # Observability (no-op when Langfuse unconfigured; never raises).
    try:
        _emit_observation(
            user, model, model_type, input_tokens, output_tokens,
            endpoint, route, cached_tokens, backend, breakdown,
            output_payload=output_payload, timing=timing,
            cache_write_tokens=cache_write_tokens,
        )
    except Exception as exc:  # defensive — must never break logging/billing
        logger.warning("observability hook failed: {}", exc)

    # Slow-request WARNING (file-visible without INFO logging). Must be read
    # here, on the request's own task — the contextvar holding the request
    # start time is not visible from the usage-write executor thread.
    latency_ms = request_latency_ms()
    try:
        _warn_if_slow(
            user, model, model_type, endpoint, backend,
            input_tokens, output_tokens, latency_ms, timing,
        )
    except Exception as exc:  # defensive — a log line must never break billing
        logger.warning("slow-request hook failed: {}", exc)

    # Prometheus (no-op unless METRICS_ENABLED; never raises).
    metrics.observe_request(
        endpoint, backend, model, input_tokens, output_tokens, cached_tokens,
        float(cost), latency_ms, timing, cache_write_tokens=cache_write_tokens,
    )

    _submit_background(
        _log_usage_sync,
        user.id, user.username,
        model, model_type, input_tokens, output_tokens, cost, endpoint,
        backend,
        # Only a cloud row is ever an overflow row: a request that fell back
        # on-prem after a failed attempt is ordinary local usage.
        backend != "vllm" and _overflow_from.get() is not None,
        # The service account that sent this for ``user`` (X-On-Behalf-Of),
        # 0 for a request with the account's own key. The row is ``user``'s.
        via_user_id(user),
    )
