"""
vLLM downstream proxy.

Public forward functions all carry the ``vllm_`` prefix so they're
unambiguous at call sites that also import from ``azure_proxy``:

  - vllm_forward_chat_completions  — OpenAI chat completions (stream + non-stream)
  - vllm_forward_simple_request    — embeddings / rerank / score (non-stream)
  - vllm_forward_responses         — OpenAI Responses API pass-through
  - vllm_forward_messages          — Anthropic Messages translation
  - vllm_forward_count_tokens      — Anthropic count_tokens (forwards to /tokenize)
  - vllm_forward_tokenize          — vLLM-native /tokenize pass-through

All six share ``_resolve_model`` for health-aware type-checked fallback;
billing goes through ``_log_usage`` (decoupled from any specific route map).
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlmodel import Session

from app.core.config import FALLBACK_MAP, MODEL_ROUTING, PRICING_MAP, _check_auto_reload
from app.core.database import engine
from app.core.logger import logger
from app.core.server_state import get_client, is_alive
from app.models.schema import UsageLog, User
from app.services.anthropic_adapter import (
    AnthropicStreamTranslator,
    anthropic_request_io,
    anthropic_to_openai_request,
    empty_turn_warning,
    openai_message_to_anthropic,
    openai_to_anthropic_response,
    summarize_request_shape,
)
from app.services.observability import (
    GenerationRecord,
    StreamingChatOutput,
    capture_io_enabled,
    get_langfuse,
    get_request_meta,
    record_generation,
    request_latency_ms,
    set_io_input,
)


# Streaming reads can stall arbitrarily long between chunks (long prefill,
# reasoning models, queued vLLM batches); let the downstream decide when to
# stop and rely on client disconnect to unwind stuck requests. Non-stream
# paths keep a bounded timeout (_NON_STREAM_TIMEOUT).
_STREAM_TIMEOUT = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)
# Kept just below the nginx `proxy_read_timeout` (300s) so the gateway's own
# httpx timeout fires first and returns a clean 502, rather than nginx
# severing the connection mid-flight and the client seeing a raw 504.
_NON_STREAM_TIMEOUT = 280.0

# Hard ceiling on total downstream silence for a streaming request. The
# per-chunk read timeout is unbounded (reasoning models stall legitimately),
# but a truly dead downstream would otherwise hang the request forever — each
# hung stream permanently holds one shared-pool connection, and enough of
# them accumulated over weeks exhausts the pool (slow requests + health
# probes failing). After this many seconds with no real chunk, the pump gives
# up so the connection is recycled and the caller can surface an error.
# Applies to ALL SSE stream paths (chat completions, responses, Anthropic).
_SSE_MAX_IDLE = 300.0

# How often the pump emits an idle tick while a stream is silent. The
# Anthropic path forwards each tick as an SSE `event: ping` — Claude Code
# treats long gaps without any event as a dead connection, and 10s keeps
# ~33% headroom under the smallest client idle timeout observed (~15s). The
# OpenAI-shape paths (chat/responses) consume the tick only for idle
# accounting and emit nothing on the wire.
_SSE_PING_INTERVAL = 10.0
# Real Anthropic's ping carries `{"type": "ping"}` as the data payload.
# Claude Code parses every SSE event's data as JSON and dispatches on the
# `type` key — an empty `{}` payload looks malformed to that parser and may
# cause the client to drop or stall the stream, defeating the ping's purpose.
_ANTHROPIC_PING_EVENT = 'event: ping\ndata: {"type": "ping"}\n\n'


async def _pump_sse_lines(
    send_coro,
    ping_interval: float = _SSE_PING_INTERVAL,
    max_idle: float = _SSE_MAX_IDLE,
):
    """Run a streaming HTTP request as a background task and yield events.

    Yields tuples:
      ('ping', None)        — `ping_interval` seconds passed without new data
      ('line', str)         — SSE data line from downstream
      ('err', Exception)    — downstream raised, or `max_idle` seconds passed
                              with no real data (treated as a dead downstream)
      ('done', None)        — stream finished cleanly

    Caller does NOT need to close the response — this helper handles aclose.
    """
    queue: asyncio.Queue = asyncio.Queue()
    resp_holder: list = []

    async def reader():
        try:
            resp = await send_coro
            resp_holder.append(resp)
            async for line in resp.aiter_lines():
                await queue.put(("line", line))
        except BaseException as e:
            await queue.put(("err", e))
        finally:
            await queue.put(("done", None))

    task = asyncio.create_task(reader())
    idle = 0.0
    try:
        while True:
            try:
                kind, data = await asyncio.wait_for(queue.get(), timeout=ping_interval)
            except asyncio.TimeoutError:
                idle += ping_interval
                if idle >= max_idle:
                    yield ("err", TimeoutError(
                        f"downstream produced no data for {max_idle:.0f}s"
                    ))
                    return
                yield ("ping", None)
                continue
            idle = 0.0  # real data arrived — reset the idle clock
            yield (kind, data)
            if kind in ("done", "err"):
                return
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for resp in resp_holder:
            try:
                await resp.aclose()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_model(
    model_name: str,
    allowed_types: list[str],
) -> tuple[str, dict[str, Any], str | None]:
    """
    Return (resolved_alias, route_info, fallback_reason).

    Priority:
      1. Exact match + server is alive
      2. Exact match but server is down → fallback to alive server of same type
      3. Unknown model / wrong type     → fallback to alive server of compatible type
      4. No alive server found          → try any server of compatible type (best effort)

    fallback_reason is None when the original model was used directly.
    """
    _check_auto_reload()
    route = MODEL_ROUTING.get(model_name)

    # Exact match with correct type
    if route and route["type"] in allowed_types:
        if is_alive(route["base_url"]):
            return model_name, route, None
        # Server is down — try to find an alive fallback of the same type
        reason = f"model '{model_name}' server is DOWN"
    else:
        reason = f"model '{model_name}' not found or wrong type for {allowed_types}"

    # Configured fallback: check FALLBACK_MAP first
    for at in allowed_types:
        fb_alias = FALLBACK_MAP.get(at)
        if fb_alias and fb_alias != model_name and fb_alias in MODEL_ROUTING:
            fb_route = MODEL_ROUTING[fb_alias]
            if is_alive(fb_route["base_url"]):
                logger.warning("{} - falling back to configured fallback '{}'", reason, fb_alias)
                return fb_alias, fb_route, reason

    # Auto fallback: first alive model with compatible type
    routing_snapshot = dict(MODEL_ROUTING)
    for fb_name, fb_route in routing_snapshot.items():
        if fb_route["type"] in allowed_types and is_alive(fb_route["base_url"]):
            logger.warning("{} - falling back to '{}'", reason, fb_name)
            return fb_name, fb_route, reason

    # No alive server — best effort: use any model with compatible type
    for fb_name, fb_route in routing_snapshot.items():
        if fb_route["type"] in allowed_types:
            logger.warning("{} - no alive server, best-effort fallback to '{}'", reason, fb_name)
            return fb_name, fb_route, reason

    raise HTTPException(
        status_code=400,
        detail=f"No available model for types {allowed_types}.",
    )


def _get_downstream_headers(route: dict[str, Any]) -> dict[str, str]:
    """Build headers for the downstream request, including API key if configured."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    api_key = route.get("api_key", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _error_response(resp) -> JSONResponse:
    """Build a JSONResponse from a non-200 downstream response, handling non-JSON bodies."""
    try:
        content = resp.json()
    except Exception:
        content = {"error": resp.text[:500]}
    return JSONResponse(content=content, status_code=resp.status_code)


def _calc_cost_breakdown(
    route: dict[str, Any],
    model_type: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
) -> dict[str, Decimal]:
    """Return the per-component cost: ``{input, output, cache_read_input_tokens, total}``.

    Same pricing logic / priority as ``_calc_cost`` (per-model override on
    `route` → per-type → default), but split out so callers (Langfuse
    ``costDetails``) can report the breakdown. ``input`` is the *full* billable
    input cost (uncached portion at full rate + cached portion at the
    discounted rate); ``cache_read_input_tokens`` is the cached portion's cost
    on its own, for visibility. ``total`` matches ``_calc_cost`` exactly.
    """
    cached_price = None
    if route and "input_price_per_1m" in route and "output_price_per_1m" in route:
        inp_price = Decimal(str(route["input_price_per_1m"]))
        out_price = Decimal(str(route["output_price_per_1m"]))
        if "cached_input_price_per_1m" in route:
            cached_price = Decimal(str(route["cached_input_price_per_1m"]))
    else:
        pricing = PRICING_MAP.get(model_type, PRICING_MAP.get("_default", {}))
        inp_price = Decimal(str(pricing.get("input_price_per_1m", 0.0)))
        out_price = Decimal(str(pricing.get("output_price_per_1m", 0.0)))
        if "cached_input_price_per_1m" in pricing:
            cached_price = Decimal(str(pricing["cached_input_price_per_1m"]))

    # Clamp cached_tokens into [0, input_tokens] — defends against a
    # downstream reporting cached > prompt (shouldn't happen, but cheap).
    cached = max(0, min(cached_tokens, input_tokens))
    if cached and cached_price is not None:
        uncached = input_tokens - cached
        cached_cost = cached * cached_price / 1_000_000
        input_cost = (uncached * inp_price + cached * cached_price) / 1_000_000
    else:
        cached_cost = Decimal("0")
        input_cost = input_tokens * inp_price / 1_000_000
    output_cost = output_tokens * out_price / 1_000_000
    return {
        "input": input_cost,
        "output": output_cost,
        "cache_read_input_tokens": cached_cost,
        "total": input_cost + output_cost,
    }


def _calc_cost(
    route: dict[str, Any],
    model_type: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
) -> Decimal:
    """Total request cost. Lookup priority: per-model override on `route` →
    per-type → default. Thin wrapper over ``_calc_cost_breakdown`` (kept for the
    existing callers and its established behaviour).

    `cached_tokens` (a subset of `input_tokens` that hit a prompt cache) is
    billed at `cached_input_price_per_1m` when that price is configured —
    used by the Azure path. When no cached price is set, or `cached_tokens` is
    0, all input tokens are charged at the full input price (the vLLM path
    never passes `cached_tokens`, so its behaviour is unchanged).
    """
    return _calc_cost_breakdown(
        route, model_type, input_tokens, output_tokens, cached_tokens
    )["total"]


def _log_usage_sync(
    user_id: int,
    username: str,
    daily_limit: float,
    model: str,
    model_type: str,
    input_tokens: int,
    output_tokens: int,
    cost: Decimal,
    endpoint: str,
    backend: str = "vllm",
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
            )
            session.add(log)
            session.commit()

            # Post-write daily limit check (soft protection for TOCTOU race)
            from sqlmodel import func, select as sel
            from app.core.timeutil import local_day_start_utc
            today_start = local_day_start_utc()
            stmt = (
                sel(func.coalesce(func.sum(UsageLog.cost_usd), 0))
                .where(UsageLog.user_id == user_id)
                .where(UsageLog.created_at >= today_start)
            )
            today_cost = float(session.exec(stmt).one())
            if today_cost > daily_limit:
                logger.warning(
                    "Daily limit exceeded after request | user={} limit=${} actual=${}",
                    username, daily_limit, today_cost,
                )
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
) -> None:
    """Build a GenerationRecord and hand it to Langfuse. No-op when unconfigured."""
    # Short-circuit before building anything when Langfuse is off (cached after
    # the first call) — keeps the unconfigured request path overhead at zero.
    if get_langfuse() is None:
        return
    meta = get_request_meta()
    usage_details = {"input": input_tokens, "output": output_tokens}
    if cached_tokens:
        usage_details["cache_read_input_tokens"] = cached_tokens
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
    record_generation(GenerationRecord(
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
        user_agent=meta.get("user_agent"),
        x_app=meta.get("x_app"),
        session_id=meta.get("session_id"),
        display_name=getattr(user, "display_name", None),
        input_payload=input_payload,
        output_payload=out_payload,
    ))


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
    try:
        if get_langfuse() is None:
            return
        meta = get_request_meta()
        record_generation(GenerationRecord(
            username=user.username,
            user_id=str(user.id),
            endpoint=endpoint,
            backend=(
                "azure" if endpoint.startswith("/azure")
                else "bedrock" if endpoint.startswith("/aws")
                else "vllm"
            ),
            model_alias=model,
            real_model=model,
            model_type=model_type,
            usage={"input": 0, "output": 0},
            cost={"input": 0.0, "output": 0.0, "total": 0.0},
            output_tokens=0,
            error=f"{status_code}: {str(error)[:500]}",
            is_error=True,
            latency_ms=request_latency_ms(),
            user_agent=meta.get("user_agent"),
            x_app=meta.get("x_app"),
            session_id=meta.get("session_id"),
            display_name=getattr(user, "display_name", None),
        ))
    except Exception as exc:  # never let the error-hook break error handling
        logger.warning("observability error-hook failed: {}", exc)


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
) -> None:
    """Fire-and-forget usage logging in a background thread.

    If `route` is omitted, looks up MODEL_ROUTING by alias (vLLM default
    behaviour). Azure callers should pass their AZURE_MODELS entry so any
    per-model price override there is honoured at the DB level.

    `cached_tokens` lets the Azure path bill prompt-cache hits at the
    discounted `cached_input_price_per_1m`; vLLM callers omit it.

    `backend` ("vllm" | "azure") is persisted on the usage_logs row (so cloud
    vs on-prem spend can be split for reporting and the Azure daily sub-limit)
    and tags the Langfuse generation. Emitting the Langfuse generation happens
    here so every billable path is covered from one seam; it is a no-op when
    Langfuse is unconfigured.

    `output_payload` (assistant text/content) is attached to the Langfuse
    generation only when LANGFUSE_CAPTURE_IO is on (Phase 2); input is read
    from the request-meta contextvar set by the forward_* function.
    """
    import asyncio

    if route is None:
        route = MODEL_ROUTING.get(model, {})
    breakdown = _calc_cost_breakdown(route, model_type, input_tokens, output_tokens, cached_tokens)
    cost = breakdown["total"]

    # Observability (no-op when Langfuse unconfigured; never raises).
    try:
        _emit_observation(
            user, model, model_type, input_tokens, output_tokens,
            endpoint, route, cached_tokens, backend, breakdown,
            output_payload=output_payload,
        )
    except Exception as exc:  # defensive — must never break logging/billing
        logger.warning("observability hook failed: {}", exc)

    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(
            None,
            _log_usage_sync,
            user.id, user.username, user.daily_limit_usd,
            model, model_type, input_tokens, output_tokens, cost, endpoint,
            backend,
        )
    except RuntimeError:
        # No running loop (e.g. during testing) — run synchronously
        _log_usage_sync(
            user.id, user.username, user.daily_limit_usd,  # type: ignore[arg-type]
            model, model_type, input_tokens, output_tokens, cost, endpoint,
            backend,
        )


# ---------------------------------------------------------------------------
# 1. vllm_forward_chat_completions - Standard Chat Completion (stream + non-stream)
# ---------------------------------------------------------------------------

def _fallback_headers(fallback_reason: str | None) -> dict[str, str]:
    """Return extra response headers when a fallback occurred."""
    if fallback_reason:
        return {"X-Model-Fallback": fallback_reason}
    return {}


def _tokenize_url(base_url: str) -> str:
    """vLLM exposes /tokenize at the server root, not under /v1, but the
    configured base_url usually ends in /v1 (so chat/completions etc. resolve
    correctly). Strip a trailing /v1 before appending /tokenize.
    """
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return f"{root}/tokenize"


async def vllm_forward_chat_completions(
    request: Request,
    user: User,
    allowed_types: list[str],
) -> StreamingResponse | JSONResponse:
    body = await request.json()
    model_name = body.get("model", "")
    resolved_alias, route, fallback_reason = _resolve_model(model_name, allowed_types)

    # Swap to real_model for the downstream server
    real_model = route["real_model"]
    body["model"] = real_model
    base_url = route["base_url"]
    model_type = route["type"]
    downstream_headers = _get_downstream_headers(route)
    is_stream = body.get("stream", False)
    extra_headers = _fallback_headers(fallback_reason)

    # Always request usage in streaming mode so we can capture token counts
    if is_stream:
        opts = body.get("stream_options") or {}
        opts["include_usage"] = True
        body["stream_options"] = opts

    client = get_client()
    target_url = f"{base_url}/chat/completions"

    # Keep a copy of user-facing request body for monitoring (before real_model swap)
    monitor_body = body.copy()
    monitor_body["model"] = resolved_alias

    # Phase 2: capture the (already OpenAI-shaped) messages as Langfuse input.
    if capture_io_enabled():
        set_io_input(body.get("messages"))

    if is_stream:
        return await _stream_chat(
            client, target_url, body, downstream_headers, user, resolved_alias, model_type,
            extra_headers, monitor_body, route,
        )
    else:
        return await _non_stream_chat(
            client, target_url, body, downstream_headers, user, resolved_alias, model_type,
            extra_headers, monitor_body, route,
        )


async def _non_stream_chat(
    client, url: str, body: dict, headers: dict, user: User, model: str, model_type: str,
    extra_headers: dict[str, str] | None = None, monitor_body: dict | None = None,
    route: dict[str, Any] | None = None,
) -> JSONResponse:
    try:
        resp = await client.post(url, json=body, headers=headers, timeout=_NON_STREAM_TIMEOUT)
    except Exception as exc:
        logger.error("Downstream error: {}: {}", type(exc).__name__, exc)
        _log_error(user, monitor_body or body, str(exc), 502, model, "/v1/chat/completions", model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        _log_error(user, monitor_body or body, resp.text[:500], resp.status_code, model, "/v1/chat/completions", model_type)
        return _error_response(resp)

    data = resp.json()
    usage = data.get("usage", {})
    input_tk = usage.get("prompt_tokens", 0)
    output_tk = usage.get("completion_tokens", 0)
    obs_output = (data.get("choices") or [{}])[0].get("message") if capture_io_enabled() else None
    _log_usage(user, model, model_type, input_tk, output_tk, "/v1/chat/completions", route=route, output_payload=obs_output)
    return JSONResponse(content=data, headers=extra_headers)


async def _stream_chat(
    client, url: str, body: dict, headers: dict, user: User, model: str, model_type: str,
    extra_headers: dict[str, str] | None = None, monitor_body: dict | None = None,
    route: dict[str, Any] | None = None,
) -> StreamingResponse:
    req = client.build_request("POST", url, json=body, headers=headers, timeout=_STREAM_TIMEOUT)
    _capture_io = capture_io_enabled()

    async def event_generator():
        input_tokens = 0
        output_tokens = 0
        output_acc = StreamingChatOutput()  # Phase 2: assistant turn for Langfuse output
        # The pump owns the response lifecycle (aclose) and enforces the
        # max-idle ceiling so a dead downstream can't hold a pool connection
        # forever. Idle ticks are consumed silently — OpenAI-shape clients
        # get no heartbeat on the wire.
        async for kind, data in _pump_sse_lines(client.send(req, stream=True)):
            if kind == "ping":
                continue
            if kind == "done":
                break
            if kind == "err":
                logger.error("Stream error: {}: {}", type(data).__name__, data)
                yield f"data: {json.dumps({'error': str(data)})}\n\n"
                break

            line = data
            if not line:
                yield "\n"
                continue

            yield f"{line}\n\n"

            # Parse usage from SSE data lines
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    chunk = json.loads(line[6:])
                    if _capture_io:
                        output_acc.add_delta((chunk.get("choices") or [{}])[0].get("delta") or {})
                    usage = chunk.get("usage")
                    if usage:
                        input_tokens = usage.get("prompt_tokens", input_tokens)
                        output_tokens = usage.get("completion_tokens", output_tokens)
                except (json.JSONDecodeError, KeyError):
                    pass

        if input_tokens == 0 and output_tokens == 0:
            logger.warning("Stream for model={} ended with 0 tokens — downstream may not report usage", model)
        obs_output = output_acc.as_message() if _capture_io else None
        _log_usage(user, model, model_type, input_tokens, output_tokens, "/v1/chat/completions", route=route, output_payload=obs_output)

    resp_headers = {"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}
    if extra_headers:
        resp_headers.update(extra_headers)
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=resp_headers,
    )


# ---------------------------------------------------------------------------
# 2. vllm_forward_simple_request - Embeddings & Rerankers (non-streaming)
# ---------------------------------------------------------------------------

async def vllm_forward_simple_request(
    request: Request,
    user: User,
    allowed_types: list[str],
    path_suffix: str,
    endpoint_label: str,
) -> JSONResponse:
    body = await request.json()
    model_name = body.get("model", "")
    resolved_alias, route, fallback_reason = _resolve_model(model_name, allowed_types)

    real_model = route["real_model"]
    body["model"] = real_model
    base_url = route["base_url"]
    model_type = route["type"]
    downstream_headers = _get_downstream_headers(route)
    extra_headers = _fallback_headers(fallback_reason)

    client = get_client()
    target_url = f"{base_url}{path_suffix}"

    try:
        resp = await client.post(target_url, json=body, headers=downstream_headers, timeout=_NON_STREAM_TIMEOUT)
    except Exception as exc:
        logger.error("Downstream error: {}: {}", type(exc).__name__, exc)
        _log_error(user, body, str(exc), 502, resolved_alias, endpoint_label, model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        _log_error(user, body, resp.text[:500], resp.status_code, resolved_alias, endpoint_label, model_type)
        return _error_response(resp)

    data = resp.json()
    usage = data.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    total_tokens = usage.get("total_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)

    # Rerankers sometimes only report total_tokens
    if prompt_tokens == 0 and total_tokens > 0:
        prompt_tokens = total_tokens

    _log_usage(user, resolved_alias, model_type, prompt_tokens, completion_tokens, endpoint_label)
    return JSONResponse(content=data, headers=extra_headers)


# ---------------------------------------------------------------------------
# 3. vllm_forward_responses - Pure pass-through (e.g. /responses)
# ---------------------------------------------------------------------------

async def vllm_forward_responses(
    request: Request,
    user: User,
    allowed_types: list[str],
    path_suffix: str,
) -> StreamingResponse | JSONResponse:
    """
    Forward the raw request body to `base_url + path_suffix`.
    Only mutate the model field (alias -> real_model). Supports SSE streaming.
    """
    raw_body = await request.body()
    try:
        body_json = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    model_name = body_json.get("model", "")
    resolved_alias, route, fallback_reason = _resolve_model(model_name, allowed_types)
    base_url = route["base_url"]
    model_type = route["type"]
    downstream_headers = _get_downstream_headers(route)
    extra_headers = _fallback_headers(fallback_reason)

    # Phase 2: capture the request input (Responses API `input`, or messages).
    if capture_io_enabled():
        set_io_input(body_json.get("input") or body_json.get("messages") or body_json)

    # Always replace alias with real_model for downstream
    real_model = route["real_model"]
    body_json["model"] = real_model
    raw_body = json.dumps(body_json).encode()

    client = get_client()
    target_url = f"{base_url}{path_suffix}"
    is_stream = body_json.get("stream", False)

    if is_stream:
        return await _passthrough_stream(
            client, target_url, raw_body, downstream_headers,
            user, resolved_alias, model_type, path_suffix, extra_headers, route,
        )
    else:
        return await _passthrough_non_stream(
            client, target_url, raw_body, downstream_headers,
            user, resolved_alias, model_type, path_suffix, extra_headers, route,
        )


async def _passthrough_non_stream(
    client, url: str, raw_body: bytes, headers: dict,
    user: User, model: str, model_type: str, path_suffix: str,
    extra_headers: dict[str, str] | None = None,
    route: dict[str, Any] | None = None,
) -> JSONResponse:
    try:
        resp = await client.post(url, content=raw_body, headers=headers, timeout=_NON_STREAM_TIMEOUT)
    except Exception as exc:
        logger.error("Downstream error: {}: {}", type(exc).__name__, exc)
        _log_error(user, json.loads(raw_body), str(exc), 502, model, path_suffix, model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        _log_error(user, json.loads(raw_body), resp.text[:500], resp.status_code, model, path_suffix, model_type)
        return _error_response(resp)

    data = resp.json()
    usage = data.get("usage", {})
    input_tk = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    output_tk = usage.get("output_tokens", usage.get("completion_tokens", 0))
    details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached_tk = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
    obs_output = data.get("output") or data if capture_io_enabled() else None
    _log_usage(user, model, model_type, input_tk, output_tk, path_suffix, route=route, cached_tokens=cached_tk, output_payload=obs_output)
    return JSONResponse(content=data, headers=extra_headers)


# ---------------------------------------------------------------------------
# 4. vllm_forward_messages - Anthropic /v1/messages compatibility
# ---------------------------------------------------------------------------

async def vllm_forward_messages(
    request: Request,
    user: User,
    allowed_types: list[str],
) -> StreamingResponse | JSONResponse:
    """Accept an Anthropic Messages API request, translate to OpenAI chat
    completions, forward to the downstream server, then translate the response
    back to Anthropic format."""
    try:
        anthropic_body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    model_name = anthropic_body.get("model", "")
    resolved_alias, route, fallback_reason = _resolve_model(model_name, allowed_types)

    real_model = route["real_model"]
    base_url = route["base_url"]
    model_type = route["type"]
    downstream_headers = _get_downstream_headers(route)
    extra_headers = _fallback_headers(fallback_reason)

    openai_body = anthropic_to_openai_request(
        anthropic_body, is_reasoning=bool(route.get("is_reasoning")),
    )
    openai_body["model"] = real_model
    is_stream = bool(anthropic_body.get("stream", False))

    # Phase 2: stash the ORIGINAL Anthropic request as the Langfuse input so the
    # trace of an Anthropic endpoint is a faithful Anthropic record (not the
    # internal OpenAI pivot). No-op unless LANGFUSE_CAPTURE_IO is on.
    if capture_io_enabled():
        set_io_input(anthropic_request_io(anthropic_body))

    if is_stream:
        # Always include usage in the stream so we can report it back
        opts = openai_body.get("stream_options") or {}
        opts["include_usage"] = True
        openai_body["stream_options"] = opts
        openai_body["stream"] = True

    client = get_client()
    target_url = f"{base_url}/chat/completions"

    monitor_body = dict(anthropic_body)
    monitor_body["model"] = resolved_alias

    # Compact request-shape summary for the empty-turn diagnostic (see
    # _stream_messages / _non_stream_messages). Computed from the original
    # Anthropic body so thinking blocks are still visible.
    req_shape = summarize_request_shape(anthropic_body)

    if is_stream:
        return await _stream_messages(
            client, target_url, openai_body, downstream_headers,
            user, resolved_alias, model_type, extra_headers, monitor_body, route,
            req_shape,
        )
    return await _non_stream_messages(
        client, target_url, openai_body, downstream_headers,
        user, resolved_alias, model_type, extra_headers, monitor_body, route,
        req_shape,
    )


async def _non_stream_messages(
    client, url: str, body: dict, headers: dict, user: User,
    model_alias: str, model_type: str,
    extra_headers: dict[str, str] | None = None,
    monitor_body: dict | None = None,
    route: dict[str, Any] | None = None,
    req_shape: str = "",
) -> JSONResponse:
    try:
        resp = await client.post(url, json=body, headers=headers, timeout=_NON_STREAM_TIMEOUT)
    except Exception as exc:
        logger.error("Downstream error: {}: {}", type(exc).__name__, exc)
        _log_error(user, monitor_body or body, str(exc), 502, model_alias, "/v1/messages", model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        _log_error(user, monitor_body or body, resp.text[:500], resp.status_code, model_alias, "/v1/messages", model_type)
        return _error_response(resp)

    openai_data = resp.json()
    anthropic_data = openai_to_anthropic_response(openai_data, model_alias)

    input_tk = anthropic_data["usage"]["input_tokens"]
    output_tk = anthropic_data["usage"]["output_tokens"]
    obs_output = None
    if capture_io_enabled():
        # Record the Anthropic-shape assistant message (role + content blocks)
        # so the trace matches the Anthropic surface, not the OpenAI pivot.
        obs_output = {"role": "assistant", "content": anthropic_data["content"]}

    # Empty / near-empty turn diagnostic (silent stop in Claude Code).
    text_chars = sum(
        len(b.get("text", "")) for b in anthropic_data.get("content", [])
        if isinstance(b, dict) and b.get("type") == "text"
    )
    thinking_chars = sum(
        len(b.get("thinking", "")) for b in anthropic_data.get("content", [])
        if isinstance(b, dict) and b.get("type") == "thinking"
    )
    diag = empty_turn_warning(
        model_alias, input_tk, output_tk, anthropic_data.get("stop_reason"),
        text_chars, thinking_chars, req_shape,
    )
    if diag:
        logger.warning(diag)

    _log_usage(user, model_alias, model_type, input_tk, output_tk, "/v1/messages", route=route, output_payload=obs_output)

    return JSONResponse(content=anthropic_data, headers=extra_headers)


async def _stream_messages(
    client, url: str, body: dict, headers: dict, user: User,
    model_alias: str, model_type: str,
    extra_headers: dict[str, str] | None = None,
    monitor_body: dict | None = None,
    route: dict[str, Any] | None = None,
    req_shape: str = "",
) -> StreamingResponse:
    req = client.build_request("POST", url, json=body, headers=headers, timeout=_STREAM_TIMEOUT)
    _capture_io = capture_io_enabled()

    logger.info("Stream start | user={} model={} endpoint=/v1/messages", user.username, model_alias)

    async def event_generator():
        translator = AnthropicStreamTranslator(model_alias)
        output_acc = StreamingChatOutput()  # Phase 2: assistant turn for Langfuse output
        try:
            for event in translator.start():
                yield event

            async for kind, data in _pump_sse_lines(client.send(req, stream=True)):
                if kind == "ping":
                    yield _ANTHROPIC_PING_EVENT
                    continue
                if kind == "err":
                    raise data
                if kind == "done":
                    break
                # kind == "line"
                line = data
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if _capture_io:
                    output_acc.add_delta((chunk.get("choices") or [{}])[0].get("delta") or {})
                for event in translator.handle_chunk(chunk):
                    yield event

            # A clean end-of-stream WITHOUT a finish_reason means the
            # downstream connection dropped mid-generation. Emitting a normal
            # message_stop would tell Claude Code the turn completed (it then
            # silently accepts a truncated answer) — surface an error instead.
            if translator.stop_reason is None:
                logger.warning(
                    "Anthropic stream ended without finish_reason | model={} — truncated",
                    model_alias,
                )
                for event in translator.fail(
                    "Downstream stream ended prematurely; the response may be incomplete."
                ):
                    yield event
            else:
                for event in translator.finish():
                    yield event
        except Exception as exc:
            logger.error("Stream error: {}: {}", type(exc).__name__, exc)
            err_payload = json.dumps({"type": "error", "error": {"type": "api_error", "message": str(exc)}})
            yield f"event: error\ndata: {err_payload}\n\n"

        input_tokens = translator.input_tokens
        output_tokens = translator.output_tokens
        # Empty / near-empty turn diagnostic: a clean finish with out<=1 is
        # the "silent stop" Claude Code shows. text/thinking chars tell a
        # truly-empty turn from a thinking-only one; req_shape shows whether
        # the request history was carrying empty / thinking assistant turns.
        diag = empty_turn_warning(
            model_alias, input_tokens, output_tokens, translator.stop_reason,
            translator.text_chars, translator.thinking_chars, req_shape,
        )
        if diag:
            logger.warning(diag)
        obs_output = (
            openai_message_to_anthropic(output_acc.as_message(), model_alias)
            if _capture_io else None
        )
        _log_usage(user, model_alias, model_type, input_tokens, output_tokens, "/v1/messages", route=route, output_payload=obs_output)

    resp_headers = {"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}
    if extra_headers:
        resp_headers.update(extra_headers)
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=resp_headers,
    )


# ---------------------------------------------------------------------------
# 5. vllm_forward_count_tokens - Anthropic /v1/messages/count_tokens
# ---------------------------------------------------------------------------

async def vllm_forward_count_tokens(
    request: Request,
    user: User,
    allowed_types: list[str],
) -> JSONResponse:
    """Anthropic-compatible token counting endpoint.

    Translates the Anthropic-format request to OpenAI chat format and forwards
    to the downstream vLLM ``/tokenize`` endpoint, which accepts a chat-style
    ``messages`` payload and returns ``{"count": N, ...}``. The result is
    repackaged as ``{"input_tokens": N}`` for Anthropic SDK compatibility.

    No usage is recorded — this is a metadata query, not a billable inference
    call. Auth and daily-limit checks still apply via ``get_current_user``.
    """
    try:
        anthropic_body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    model_name = anthropic_body.get("model", "")
    resolved_alias, route, fallback_reason = _resolve_model(model_name, allowed_types)

    real_model = route["real_model"]
    base_url = route["base_url"]
    downstream_headers = _get_downstream_headers(route)
    extra_headers = _fallback_headers(fallback_reason)

    openai_body = anthropic_to_openai_request(anthropic_body)
    # Build a /tokenize payload — vLLM accepts chat-style messages directly
    tokenize_body: dict[str, Any] = {
        "model": real_model,
        "messages": openai_body.get("messages", []),
        "add_generation_prompt": True,
    }
    if openai_body.get("tools"):
        tokenize_body["tools"] = openai_body["tools"]

    client = get_client()
    target_url = _tokenize_url(base_url)

    try:
        resp = await client.post(
            target_url, json=tokenize_body, headers=downstream_headers, timeout=30.0,
        )
    except Exception as exc:
        logger.error("count_tokens downstream error: {}: {}", type(exc).__name__, exc)
        # Fallback: rough estimate from flattened text length
        approx = _approx_token_count(openai_body.get("messages", []))
        return JSONResponse(
            content={"input_tokens": approx},
            headers=extra_headers,
        )

    if resp.status_code != 200:
        logger.warning(
            "count_tokens downstream returned {} for model={} — falling back to estimate",
            resp.status_code, resolved_alias,
        )
        approx = _approx_token_count(openai_body.get("messages", []))
        return JSONResponse(
            content={"input_tokens": approx},
            headers=extra_headers,
        )

    try:
        data = resp.json()
    except Exception:
        approx = _approx_token_count(openai_body.get("messages", []))
        return JSONResponse(
            content={"input_tokens": approx},
            headers=extra_headers,
        )

    # vLLM /tokenize returns {"count": N, "max_model_len": ..., "tokens": [...]}
    count = data.get("count")
    if count is None and isinstance(data.get("tokens"), list):
        count = len(data["tokens"])
    if count is None:
        count = _approx_token_count(openai_body.get("messages", []))

    return JSONResponse(
        content={"input_tokens": int(count)},
        headers=extra_headers,
    )


# ---------------------------------------------------------------------------
# 6. vllm_forward_tokenize - vLLM-native /tokenize pass-through
# ---------------------------------------------------------------------------

async def vllm_forward_tokenize(
    request: Request,
    user: User,
    allowed_types: list[str],
) -> JSONResponse:
    """Pass-through to the downstream vLLM ``/tokenize`` endpoint.

    Accepts vLLM's native payload (``{"model", "prompt"}`` or
    ``{"model", "messages", ...}``) and returns the raw vLLM response
    (``{"count", "max_model_len", "tokens"}``). Only the ``model`` field is
    rewritten (alias → ``real_model``); everything else is forwarded verbatim.

    Not billed — tokenization is a metadata query, not an inference call —
    so no row is written to ``usage_logs``. Auth and daily-limit checks still
    apply via ``get_current_user``.
    """
    try:
        body_json = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    model_name = body_json.get("model", "")
    _resolved_alias, route, fallback_reason = _resolve_model(model_name, allowed_types)

    body_json["model"] = route["real_model"]
    base_url = route["base_url"]
    downstream_headers = _get_downstream_headers(route)
    extra_headers = _fallback_headers(fallback_reason)

    client = get_client()
    target_url = _tokenize_url(base_url)

    try:
        resp = await client.post(
            target_url, json=body_json, headers=downstream_headers, timeout=30.0,
        )
    except Exception as exc:
        logger.error("tokenize downstream error: {}: {}", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        return _error_response(resp)

    try:
        data = resp.json()
    except Exception:
        return JSONResponse(
            content={"error": "Downstream returned non-JSON response"},
            status_code=502,
        )

    return JSONResponse(content=data, headers=extra_headers)


def _approx_token_count(messages: list[dict[str, Any]]) -> int:
    """Rough fallback token estimate (~4 chars per token) used only when the
    downstream tokenizer is unavailable."""
    total_chars = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total_chars += len(part.get("text", ""))
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            total_chars += len(fn.get("name", "")) + len(fn.get("arguments", ""))
    return max(1, total_chars // 4)


async def _passthrough_stream(
    client, url: str, raw_body: bytes, headers: dict,
    user: User, model: str, model_type: str, path_suffix: str,
    extra_headers: dict[str, str] | None = None,
    route: dict[str, Any] | None = None,
) -> StreamingResponse:
    req = client.build_request("POST", url, content=raw_body, headers=headers, timeout=_STREAM_TIMEOUT)
    _capture_io = capture_io_enabled()

    async def event_generator():
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        output_acc = StreamingChatOutput()  # Phase 2: chat-shape assistant turn
        responses_output = None  # Phase 2: full Responses output[] from the terminal event
        # Pump owns aclose + the max-idle ceiling (see _stream_chat).
        async for kind, data in _pump_sse_lines(client.send(req, stream=True)):
            if kind == "ping":
                continue
            if kind == "done":
                break
            if kind == "err":
                logger.error("Stream error: {}: {}", type(data).__name__, data)
                yield f"data: {json.dumps({'error': str(data)})}\n\n"
                break

            line = data
            if not line:
                yield "\n"
                continue

            yield f"{line}\n\n"

            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    chunk = json.loads(line[6:])
                    if _capture_io:
                        # Prefer the terminal event's full output[] (incl.
                        # function_call items); otherwise accumulate
                        # chat-completions shape (delta.content / tool_calls)
                        # or Responses text (response.output_text.delta).
                        _resp = chunk.get("response")
                        if isinstance(_resp, dict) and isinstance(_resp.get("output"), list):
                            responses_output = _resp["output"]
                        elif chunk.get("type") == "response.output_text.delta":
                            output_acc.add_delta({"content": chunk.get("delta")})
                        else:
                            output_acc.add_delta((chunk.get("choices") or [{}])[0].get("delta") or {})
                    # Chat completions shape: usage at top level on the
                    # terminal chunk (when stream_options.include_usage=true).
                    usage = chunk.get("usage")
                    # Responses API shape: usage is nested inside the
                    # `response` object on the `response.completed` event
                    # (and on `response.incomplete` / `response.failed`).
                    # Roo Code's "OpenAI" provider hits /v1/responses
                    # without enabling stream_options, so this is the only
                    # path that ever reports tokens for it.
                    if not usage and isinstance(chunk.get("response"), dict):
                        usage = chunk["response"].get("usage")
                    if usage:
                        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", input_tokens))
                        output_tokens = usage.get("output_tokens", usage.get("completion_tokens", output_tokens))
                        details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
                        if isinstance(details, dict):
                            cached_tokens = details.get("cached_tokens", cached_tokens) or cached_tokens
                except (json.JSONDecodeError, KeyError):
                    pass

        if input_tokens == 0 and output_tokens == 0:
            logger.warning("Stream for model={} ended with 0 tokens — downstream may not report usage", model)
        obs_output = None
        if _capture_io:
            obs_output = responses_output if responses_output is not None else output_acc.as_message()
        _log_usage(user, model, model_type, input_tokens, output_tokens, path_suffix, route=route, cached_tokens=cached_tokens, output_payload=obs_output)

    resp_headers = {"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}
    if extra_headers:
        resp_headers.update(extra_headers)
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=resp_headers,
    )
