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
    anthropic_to_openai_request,
    openai_to_anthropic_response,
)
from app.services.monitor import is_monitored, log_monitor, log_monitor_error


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
# but a truly dead downstream would otherwise hang the request — and its ping
# heartbeat — forever. After this many seconds with no real chunk, the pump
# gives up so the caller can surface an error instead of spinning.
_ANTHROPIC_MAX_IDLE = 300.0

# How often to send an Anthropic SSE `event: ping` while a stream is silent.
# Claude Code treats long gaps without any event as a dead connection. Keep
# this well below the smallest client idle timeout we expect to see (~15s
# observed for some Claude Code builds) — at 10s we get ~33% headroom for
# network jitter, buffer flush, and process scheduling.
_ANTHROPIC_PING_INTERVAL = 10.0
# Real Anthropic's ping carries `{"type": "ping"}` as the data payload.
# Claude Code parses every SSE event's data as JSON and dispatches on the
# `type` key — an empty `{}` payload looks malformed to that parser and may
# cause the client to drop or stall the stream, defeating the ping's purpose.
_ANTHROPIC_PING_EVENT = 'event: ping\ndata: {"type": "ping"}\n\n'


async def _pump_anthropic_lines(
    send_coro,
    ping_interval: float = _ANTHROPIC_PING_INTERVAL,
    max_idle: float = _ANTHROPIC_MAX_IDLE,
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


def _calc_cost(
    route: dict[str, Any],
    model_type: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
) -> Decimal:
    """Cost lookup priority: per-model override on `route` → per-type → default.

    The caller passes the route dict it already resolved (a MODEL_ROUTING entry
    for vLLM, an AZURE_MODELS entry for Azure, ...). This function stays
    agnostic to which downstream backend produced the route.

    `cached_tokens` (a subset of `input_tokens` that hit a prompt cache) is
    billed at `cached_input_price_per_1m` when that price is configured —
    used by the Azure path, where Microsoft really does bill cached prompt
    tokens at a discount. When no cached price is set, or `cached_tokens` is
    0, all input tokens are charged at the full input price (the vLLM path
    never passes `cached_tokens`, so its behaviour is unchanged).
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
        billable_input = uncached * inp_price + cached * cached_price
    else:
        billable_input = input_tokens * inp_price
    return (billable_input + output_tokens * out_price) / 1_000_000


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


def _log_usage(
    user: User,
    model: str,
    model_type: str,
    input_tokens: int,
    output_tokens: int,
    endpoint: str,
    route: dict[str, Any] | None = None,
    cached_tokens: int = 0,
) -> None:
    """Fire-and-forget usage logging in a background thread.

    If `route` is omitted, looks up MODEL_ROUTING by alias (vLLM default
    behaviour). Azure callers should pass their AZURE_MODELS entry so any
    per-model price override there is honoured at the DB level.

    `cached_tokens` lets the Azure path bill prompt-cache hits at the
    discounted `cached_input_price_per_1m`; vLLM callers omit it.
    """
    import asyncio

    if route is None:
        route = MODEL_ROUTING.get(model, {})
    cost = _calc_cost(route, model_type, input_tokens, output_tokens, cached_tokens)
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(
            None,
            _log_usage_sync,
            user.id, user.username, user.daily_limit_usd,
            model, model_type, input_tokens, output_tokens, cost, endpoint,
        )
    except RuntimeError:
        # No running loop (e.g. during testing) — run synchronously
        _log_usage_sync(
            user.id, user.username, user.daily_limit_usd,  # type: ignore[arg-type]
            model, model_type, input_tokens, output_tokens, cost, endpoint,
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
        log_monitor_error(user.id, monitor_body or body, str(exc), 502, model, "/v1/chat/completions", model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        log_monitor_error(user.id, monitor_body or body, resp.text[:500], resp.status_code, model, "/v1/chat/completions", model_type)
        return _error_response(resp)

    data = resp.json()
    usage = data.get("usage", {})
    input_tk = usage.get("prompt_tokens", 0)
    output_tk = usage.get("completion_tokens", 0)
    _log_usage(user, model, model_type, input_tk, output_tk, "/v1/chat/completions", route=route)
    if is_monitored(user.id):
        cost = float(_calc_cost(route or {}, model_type, input_tk, output_tk))
        log_monitor(user.id, monitor_body or body, data, model, "/v1/chat/completions", input_tk, output_tk, cost, model_type)
    return JSONResponse(content=data, headers=extra_headers)


async def _stream_chat(
    client, url: str, body: dict, headers: dict, user: User, model: str, model_type: str,
    extra_headers: dict[str, str] | None = None, monitor_body: dict | None = None,
    route: dict[str, Any] | None = None,
) -> StreamingResponse:
    req = client.build_request("POST", url, json=body, headers=headers, timeout=_STREAM_TIMEOUT)
    _monitoring = is_monitored(user.id)

    async def event_generator():
        input_tokens = 0
        output_tokens = 0
        resp = None
        chunks: list[dict] = [] if _monitoring else []
        try:
            resp = await client.send(req, stream=True)
            async for line in resp.aiter_lines():
                if not line:
                    yield "\n"
                    continue

                yield f"{line}\n\n"

                # Parse usage from SSE data lines
                if line.startswith("data: ") and line != "data: [DONE]":
                    try:
                        chunk = json.loads(line[6:])
                        if _monitoring:
                            chunks.append(chunk)
                        usage = chunk.get("usage")
                        if usage:
                            input_tokens = usage.get("prompt_tokens", input_tokens)
                            output_tokens = usage.get("completion_tokens", output_tokens)
                    except (json.JSONDecodeError, KeyError):
                        pass
        except Exception as exc:
            logger.error("Stream error: {}: {}", type(exc).__name__, exc)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        finally:
            if resp is not None:
                try:
                    await resp.aclose()
                except Exception:
                    pass

        if input_tokens == 0 and output_tokens == 0:
            logger.warning("Stream for model={} ended with 0 tokens — downstream may not report usage", model)
        _log_usage(user, model, model_type, input_tokens, output_tokens, "/v1/chat/completions", route=route)
        if _monitoring:
            cost = float(_calc_cost(route or {}, model_type, input_tokens, output_tokens))
            log_monitor(user.id, monitor_body or body, chunks, model, "/v1/chat/completions", input_tokens, output_tokens, cost, model_type)

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
        log_monitor_error(user.id, body, str(exc), 502, resolved_alias, endpoint_label, model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        log_monitor_error(user.id, body, resp.text[:500], resp.status_code, resolved_alias, endpoint_label, model_type)
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
    if is_monitored(user.id):
        monitor_body = body.copy()
        monitor_body["model"] = resolved_alias
        cost = float(_calc_cost(route, model_type, prompt_tokens, completion_tokens))
        log_monitor(user.id, monitor_body, data, resolved_alias, endpoint_label, prompt_tokens, completion_tokens, cost, model_type)
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
        log_monitor_error(user.id, json.loads(raw_body), str(exc), 502, model, path_suffix, model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        log_monitor_error(user.id, json.loads(raw_body), resp.text[:500], resp.status_code, model, path_suffix, model_type)
        return _error_response(resp)

    data = resp.json()
    usage = data.get("usage", {})
    input_tk = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    output_tk = usage.get("output_tokens", usage.get("completion_tokens", 0))
    details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached_tk = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
    _log_usage(user, model, model_type, input_tk, output_tk, path_suffix, route=route, cached_tokens=cached_tk)
    if is_monitored(user.id):
        cost = float(_calc_cost(route or {}, model_type, input_tk, output_tk, cached_tk))
        log_monitor(user.id, json.loads(raw_body), data, model, path_suffix, input_tk, output_tk, cost, model_type)
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

    if is_stream:
        return await _stream_messages(
            client, target_url, openai_body, downstream_headers,
            user, resolved_alias, model_type, extra_headers, monitor_body, route,
        )
    return await _non_stream_messages(
        client, target_url, openai_body, downstream_headers,
        user, resolved_alias, model_type, extra_headers, monitor_body, route,
    )


async def _non_stream_messages(
    client, url: str, body: dict, headers: dict, user: User,
    model_alias: str, model_type: str,
    extra_headers: dict[str, str] | None = None,
    monitor_body: dict | None = None,
    route: dict[str, Any] | None = None,
) -> JSONResponse:
    try:
        resp = await client.post(url, json=body, headers=headers, timeout=_NON_STREAM_TIMEOUT)
    except Exception as exc:
        logger.error("Downstream error: {}: {}", type(exc).__name__, exc)
        log_monitor_error(user.id, monitor_body or body, str(exc), 502, model_alias, "/v1/messages", model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        log_monitor_error(user.id, monitor_body or body, resp.text[:500], resp.status_code, model_alias, "/v1/messages", model_type)
        return _error_response(resp)

    openai_data = resp.json()
    anthropic_data = openai_to_anthropic_response(openai_data, model_alias)

    input_tk = anthropic_data["usage"]["input_tokens"]
    output_tk = anthropic_data["usage"]["output_tokens"]
    _log_usage(user, model_alias, model_type, input_tk, output_tk, "/v1/messages", route=route)
    if is_monitored(user.id):
        cost = float(_calc_cost(route or {}, model_type, input_tk, output_tk))
        log_monitor(user.id, monitor_body or body, anthropic_data, model_alias, "/v1/messages", input_tk, output_tk, cost, model_type)

    return JSONResponse(content=anthropic_data, headers=extra_headers)


async def _stream_messages(
    client, url: str, body: dict, headers: dict, user: User,
    model_alias: str, model_type: str,
    extra_headers: dict[str, str] | None = None,
    monitor_body: dict | None = None,
    route: dict[str, Any] | None = None,
) -> StreamingResponse:
    req = client.build_request("POST", url, json=body, headers=headers, timeout=_STREAM_TIMEOUT)
    _monitoring = is_monitored(user.id)

    logger.info("Stream start | user={} model={} endpoint=/v1/messages", user.username, model_alias)

    async def event_generator():
        translator = AnthropicStreamTranslator(model_alias)
        chunks: list[dict] = []
        try:
            for event in translator.start():
                yield event

            async for kind, data in _pump_anthropic_lines(client.send(req, stream=True)):
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
                if _monitoring:
                    chunks.append(chunk)
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
        if input_tokens == 0 and output_tokens == 0:
            logger.warning("Anthropic stream for model={} ended with 0 tokens", model_alias)
        _log_usage(user, model_alias, model_type, input_tokens, output_tokens, "/v1/messages", route=route)
        if _monitoring:
            cost = float(_calc_cost(route or {}, model_type, input_tokens, output_tokens))
            log_monitor(user.id, monitor_body or body, chunks, model_alias, "/v1/messages", input_tokens, output_tokens, cost, model_type)

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
    _monitoring = is_monitored(user.id)

    async def event_generator():
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        resp = None
        chunks: list[dict] = []
        try:
            resp = await client.send(req, stream=True)
            async for line in resp.aiter_lines():
                if not line:
                    yield "\n"
                    continue

                yield f"{line}\n\n"

                if line.startswith("data: ") and line != "data: [DONE]":
                    try:
                        chunk = json.loads(line[6:])
                        if _monitoring:
                            chunks.append(chunk)
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
        except Exception as exc:
            logger.error("Stream error: {}: {}", type(exc).__name__, exc)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        finally:
            if resp is not None:
                try:
                    await resp.aclose()
                except Exception:
                    pass

        if input_tokens == 0 and output_tokens == 0:
            logger.warning("Stream for model={} ended with 0 tokens — downstream may not report usage", model)
        _log_usage(user, model, model_type, input_tokens, output_tokens, path_suffix, route=route, cached_tokens=cached_tokens)
        if _monitoring:
            cost = float(_calc_cost(route or {}, model_type, input_tokens, output_tokens, cached_tokens))
            log_monitor(user.id, json.loads(raw_body), chunks, model, path_suffix, input_tokens, output_tokens, cost, model_type)

    resp_headers = {"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}
    if extra_headers:
        resp_headers.update(extra_headers)
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=resp_headers,
    )
