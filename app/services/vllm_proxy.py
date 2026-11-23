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
  - vllm_forward_render            — vLLM-native /chat/completions/render pass-through
  - vllm_forward_systemone         — System One typed decisions (/v1/systemone)

All eight share ``_resolve_model`` for health-aware type-checked fallback.
What this backend has in common with Azure and Bedrock lives elsewhere:
billing and observability in ``billing.py`` (``_log_usage``, ``_log_error``),
timeouts, the SSE pump and the native Anthropic relay in ``downstream.py``.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.config import FALLBACK_MAP, MODEL_ROUTING, _check_auto_reload
from app.core.logger import logger
from app.core.server_state import get_client, is_alive, mark_down
from app.models.schema import User
from app.services.anthropic_adapter import (
    AnthropicStreamTranslator,
    _anthropic_content_chars,
    _approx_token_count,
    anthropic_request_io,
    anthropic_to_openai_request,
    empty_turn_warning,
    normalize_anthropic_messages,
    sanitize_native_messages_body,
    openai_message_to_anthropic,
    openai_to_anthropic_response,
    summarize_request_shape,
)
from app.services import model_fallback
from app.services.billing import _log_error, _log_usage, _warn_if_slow_headers
from app.services.downstream import (
    _ANTHROPIC_PING_EVENT,
    _NON_STREAM_TIMEOUT,
    _STREAM_TIMEOUT,
    _error_response,
    _pump_sse_lines,
    _relay_native_anthropic_sse,
)
from app.services.observability import (
    StreamingChatOutput,
    capture_io_enabled,
    set_io_input,
)
from app.services.reasoning_effort import apply_to_openai_body
from app.services.redact import downstream_error_detail


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

    picked = model_fallback.pick(model_name, allowed_types, MODEL_ROUTING, FALLBACK_MAP, is_alive)
    if picked:
        fb_name, fb_route, how = picked
        if how == "configured":
            logger.warning("{} - falling back to configured fallback '{}'", reason, fb_name)
        elif how == "auto":
            logger.warning("{} - falling back to '{}'", reason, fb_name)
        else:
            logger.warning("{} - no alive server, best-effort fallback to '{}'", reason, fb_name)
        return fb_name, fb_route, reason

    raise HTTPException(
        status_code=400,
        detail=f"No available model for types {allowed_types}.",
    )


def peek_route(model_name: str, allowed_types: list[str]) -> tuple[str, dict[str, Any]] | None:
    """The alias and route ``_resolve_model`` would serve ``model_name``
    from, without its log lines; None where it would refuse. For code that
    must know the on-prem server before the request is forwarded (peak
    overflow, ``v1_api._overflow_plan``): the same maps, the same liveness
    view and the same fallback rule, so the two cannot disagree."""
    _check_auto_reload()
    route = MODEL_ROUTING.get(model_name)
    if route and route["type"] in allowed_types and is_alive(route["base_url"]):
        return model_name, route
    picked = model_fallback.pick(model_name, allowed_types, MODEL_ROUTING, FALLBACK_MAP, is_alive)
    return (picked[0], picked[1]) if picked else None


def _get_downstream_headers(route: dict[str, Any]) -> dict[str, str]:
    """Build headers for the downstream request, including API key if configured."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    api_key = route.get("api_key", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


# ---------------------------------------------------------------------------
# 1. vllm_forward_chat_completions - Standard Chat Completion (stream + non-stream)
# ---------------------------------------------------------------------------

def _fallback_headers(fallback_reason: str | None) -> dict[str, str]:
    """Return extra response headers when a fallback occurred."""
    if fallback_reason:
        return {"X-Model-Fallback": fallback_reason}
    return {}


# ---------------------------------------------------------------------------
# Request-time failover
#
# The health cache is refreshed every 30s, so a server that just died would
# otherwise 502 every request until the next probe. Instead, each downstream
# call gets ONE failover retry against a different alive server of the same
# type when:
#   - the CONNECTION fails (request never sent — always safe to retry); the
#     dead server is additionally marked DOWN in the health cache so
#     subsequent requests skip it immediately, or
#   - the response is 502/503/504 (LB "backend gone" / overloaded — no
#     partial output exists); the server is NOT marked down for these, it
#     answered, it just can't serve right now.
# Streams only fail over BEFORE the response is handed to the SSE pump —
# never mid-stream. Other statuses (4xx, 500) surface as-is: they are
# request- or model-specific and would fail identically elsewhere.
# ---------------------------------------------------------------------------

_FAILOVER_STATUSES = frozenset({502, 503, 504})
_CONNECT_FAILURES = (httpx.ConnectError, httpx.ConnectTimeout)


def _alternative_route(
    model_type: str, exclude_base_url: str
) -> tuple[str, dict[str, Any]] | None:
    """An alive same-type route on a DIFFERENT server, preferring the
    configured fallback alias. None when no distinct candidate exists."""
    fb_alias = FALLBACK_MAP.get(model_type)
    if fb_alias:
        r = MODEL_ROUTING.get(fb_alias)
        if (
            r and r["type"] == model_type
            and r["base_url"] != exclude_base_url and is_alive(r["base_url"])
        ):
            return fb_alias, r
    for alias, r in MODEL_ROUTING.items():
        if (
            r["type"] == model_type
            and r["base_url"] != exclude_base_url and is_alive(r["base_url"])
        ):
            return alias, r
    return None


def _failover_route(
    model_type: str, failed_route: dict[str, Any], why: str, *, mark: bool
) -> tuple[str, dict[str, Any], str] | None:
    """Handle a failed attempt: optionally mark the server DOWN, then pick
    an alternative. Returns (alias, route, note) or None."""
    if mark:
        mark_down(failed_route["base_url"])
        logger.warning("Marked DOWN after {}: {}", why, failed_route["base_url"])
    alt = _alternative_route(model_type, failed_route["base_url"])
    if alt is None:
        logger.warning(
            "No failover candidate after {} on {}", why, failed_route["base_url"],
        )
        return None
    alias, route = alt
    logger.warning(
        "Failover: {} on {} — retrying on {} (alias={})",
        why, failed_route["base_url"], route["base_url"], alias,
    )
    return alias, route, f"failover: {why}"


async def _post_with_failover(
    client, path: str, body: dict, alias: str, model_type: str,
    route: dict[str, Any], timeout: float = _NON_STREAM_TIMEOUT,
    url_for: Callable[[dict[str, Any]], str] | None = None,
):
    """POST ``{route.base_url}{path}`` with a single failover retry.

    Mutates ``body["model"]`` to each attempt's real_model. Returns
    ``(resp, alias, route, note)`` — alias/route reflect the server that
    actually answered; ``note`` is set when failover happened. Re-raises the
    connect exception when the final attempt fails at the connection level.

    ``url_for``, when given, builds each attempt's URL from its route
    instead — for a downstream whose endpoint doesn't hang off ``base_url``.
    """
    note: str | None = None
    while True:
        body["model"] = route["real_model"]
        url = url_for(route) if url_for else f"{route['base_url']}{path}"
        try:
            resp = await client.post(
                url, json=body, headers=_get_downstream_headers(route), timeout=timeout,
            )
        except _CONNECT_FAILURES as exc:
            alt = (
                _failover_route(
                    model_type, route, f"connect failure ({type(exc).__name__})", mark=True,
                )
                if note is None else None
            )
            if alt is None:
                raise
            alias, route, note = alt
            continue
        if note is None and resp.status_code in _FAILOVER_STATUSES:
            alt = _failover_route(
                model_type, route, f"HTTP {resp.status_code}", mark=False,
            )
            if alt is not None:
                alias, route, note = alt
                continue
        return resp, alias, route, note


async def _open_stream_with_failover(
    client, path: str, body: dict, alias: str, model_type: str,
    route: dict[str, Any],
):
    """Open a streaming connection with a single failover retry (pre-flight
    only — nothing has been sent to the client yet). Same contract as
    ``_post_with_failover``; non-failover statuses are returned as-is for
    the caller's existing pre-flight/pump handling."""
    note: str | None = None
    while True:
        body["model"] = route["real_model"]
        req = client.build_request(
            "POST", f"{route['base_url']}{path}", json=body,
            headers=_get_downstream_headers(route), timeout=_STREAM_TIMEOUT,
        )
        try:
            resp = await client.send(req, stream=True)
        except _CONNECT_FAILURES as exc:
            alt = (
                _failover_route(
                    model_type, route, f"connect failure ({type(exc).__name__})", mark=True,
                )
                if note is None else None
            )
            if alt is None:
                raise
            alias, route, note = alt
            continue
        if note is None and resp.status_code in _FAILOVER_STATUSES:
            alt = _failover_route(
                model_type, route, f"HTTP {resp.status_code}", mark=False,
            )
            if alt is not None:
                try:
                    await resp.aclose()
                except Exception:
                    pass
                alias, route, note = alt
                continue
        return resp, alias, route, note


async def _opened(resp):
    """Adapt an already-opened streaming response to _pump_sse_lines's
    send-coroutine interface."""
    return resp


def _merge_failover_header(
    extra_headers: dict[str, str] | None, note: str | None
) -> dict[str, str] | None:
    if not note:
        return extra_headers
    merged = dict(extra_headers or {})
    # A resolve-time fallback reason (if any) is superseded by the
    # request-time failover note — the latter names the server that served.
    merged["X-Model-Fallback"] = (
        f"{merged['X-Model-Fallback']}; {note}" if merged.get("X-Model-Fallback") else note
    )
    return merged


def _server_root(base_url: str) -> str:
    """The downstream's server root.

    vLLM serves /tokenize and /detokenize at the root, not under /v1, but the
    configured base_url usually ends in /v1 (so chat/completions etc. resolve
    correctly). Strip a trailing /v1 so root-level endpoints can be appended.
    """
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return root


def _tokenize_url(base_url: str) -> str:
    return f"{_server_root(base_url)}/tokenize"


def _detokenize_url(base_url: str) -> str:
    return f"{_server_root(base_url)}/detokenize"


def _align_reasoning_fields(messages: Any) -> None:
    """Mirror `reasoning` <-> `reasoning_content` on assistant messages, in place.

    The two names are dialects of the same field: DeepSeek (and vLLM < 0.20)
    read `reasoning_content`, vLLM >= 0.20 reads only `reasoning` on incoming
    messages and silently drops the other (vllm-project/vllm#38488). Clients
    speak whichever dialect they grew up with, so the gateway aligns them —
    whichever name is present gets copied to the missing one, and downstream /
    client each pick the name they know. No-op when neither field is present.
    """
    if not isinstance(messages, list):
        return
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        reasoning = msg.get("reasoning")
        reasoning_content = msg.get("reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content and not reasoning:
            msg["reasoning"] = reasoning_content
        elif isinstance(reasoning, str) and reasoning and not reasoning_content:
            msg["reasoning_content"] = reasoning


def _align_reasoning_in_response(data: dict) -> None:
    """Same aliasing for a non-stream chat completions response, in place.

    vLLM currently dual-emits both names but its docs mark `reasoning_content`
    for eventual removal; aliasing here keeps DeepSeek-convention clients
    working regardless of what the downstream emits.
    """
    for choice in data.get("choices") or []:
        message = choice.get("message") if isinstance(choice, dict) else None
        if isinstance(message, dict):
            _align_reasoning_fields([message])


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
    # Absorb the reasoning / reasoning_content dialect split so neither the
    # client's nor the downstream's version matters (see _align_reasoning_fields).
    _align_reasoning_fields(body.get("messages"))
    # Keep a client's `reasoning_effort` inside what this model accepts —
    # a no-op unless the route declares `reasoning_efforts`.
    apply_to_openai_body(body, route, resolved_alias, "/v1/chat/completions")
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
        resp, model, route, failover_note = await _post_with_failover(
            client, "/chat/completions", body, model, model_type, route,
        )
    except Exception as exc:
        logger.error("Downstream error: {}: {}", type(exc).__name__, exc)
        _log_error(user, monitor_body or body, exc, 502, model, "/v1/chat/completions", model_type)
        raise HTTPException(status_code=502, detail=downstream_error_detail(exc))
    extra_headers = _merge_failover_header(extra_headers, failover_note)

    if resp.status_code != 200:
        _log_error(user, monitor_body or body, resp.text[:500], resp.status_code, model, "/v1/chat/completions", model_type)
        return _error_response(resp)

    data = resp.json()
    _align_reasoning_in_response(data)
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
    try:
        opened_resp, model, route, failover_note = await _open_stream_with_failover(
            client, "/chat/completions", body, model, model_type, route,
        )
    except Exception as exc:
        logger.error("Downstream error: {}: {}", type(exc).__name__, exc)
        _log_error(user, monitor_body or body, exc, 502, model, "/v1/chat/completions", model_type)
        raise HTTPException(status_code=502, detail=downstream_error_detail(exc))
    extra_headers = _merge_failover_header(extra_headers, failover_note)
    _warn_if_slow_headers(user, model, "/v1/chat/completions", "vllm")
    _capture_io = capture_io_enabled()

    async def event_generator():
        input_tokens = 0
        output_tokens = 0
        output_acc = StreamingChatOutput()  # Phase 2: assistant turn for Langfuse output
        # The pump owns the response lifecycle (aclose) and enforces the
        # max-idle ceiling so a dead downstream can't hold a pool connection
        # forever. Idle ticks are consumed silently — OpenAI-shape clients
        # get no heartbeat on the wire.
        async for kind, data in _pump_sse_lines(_opened(opened_resp)):
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
    model_type = route["type"]
    extra_headers = _fallback_headers(fallback_reason)

    client = get_client()

    try:
        resp, resolved_alias, route, failover_note = await _post_with_failover(
            client, path_suffix, body, resolved_alias, model_type, route,
        )
    except Exception as exc:
        logger.error("Downstream error: {}: {}", type(exc).__name__, exc)
        _log_error(user, body, exc, 502, resolved_alias, endpoint_label, model_type)
        raise HTTPException(status_code=502, detail=downstream_error_detail(exc))
    extra_headers = _merge_failover_header(extra_headers, failover_note)

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
    model_type = route["type"]
    downstream_headers = _get_downstream_headers(route)
    extra_headers = _fallback_headers(fallback_reason)

    # Phase 2: capture the request input (Responses API `input`, or messages).
    if capture_io_enabled():
        set_io_input(body_json.get("input") or body_json.get("messages") or body_json)

    # Always replace alias with real_model for downstream
    real_model = route["real_model"]
    body_json["model"] = real_model

    client = get_client()
    is_stream = body_json.get("stream", False)

    if is_stream:
        return await _passthrough_stream(
            client, body_json, downstream_headers,
            user, resolved_alias, model_type, path_suffix, extra_headers, route,
        )
    else:
        return await _passthrough_non_stream(
            client, body_json, downstream_headers,
            user, resolved_alias, model_type, path_suffix, extra_headers, route,
        )


async def _passthrough_non_stream(
    client, body_json: dict, headers: dict,
    user: User, model: str, model_type: str, path_suffix: str,
    extra_headers: dict[str, str] | None = None,
    route: dict[str, Any] | None = None,
) -> JSONResponse:
    try:
        resp, model, route, failover_note = await _post_with_failover(
            client, path_suffix, body_json, model, model_type, route,
        )
    except Exception as exc:
        logger.error("Downstream error: {}: {}", type(exc).__name__, exc)
        _log_error(user, body_json, exc, 502, model, path_suffix, model_type)
        raise HTTPException(status_code=502, detail=downstream_error_detail(exc))
    extra_headers = _merge_failover_header(extra_headers, failover_note)

    if resp.status_code != 200:
        _log_error(user, body_json, resp.text[:500], resp.status_code, model, path_suffix, model_type)
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

async def _forward_messages_native(
    client, anthropic_body: dict, user: User,
    model_alias: str, model_type: str, route: dict[str, Any],
    extra_headers: dict[str, str], req_shape: str,
) -> StreamingResponse | JSONResponse | None:
    """Forward an Anthropic request as-is to a vLLM native /v1/messages
    endpoint (``native_messages = true`` on the route).

    Returns None when the downstream answers 404/405 — it doesn't actually
    serve the native endpoint (older vLLM) — so the caller falls back to the
    translation path. The body is passed through except for ``model``
    (alias → real_model down, real_model → alias on the way back up) and the
    system-role normalization (a no-op for schema-clean requests — see
    ``normalize_anthropic_messages``).
    """
    native_body, dropped = sanitize_native_messages_body(
        normalize_anthropic_messages(anthropic_body)
    )
    native_body = dict(native_body)
    if dropped:
        logger.warning(
            "Native /v1/messages: dropped unsupported field(s) {} | user={} model={}",
            dropped, user.username, model_alias,
        )
    native_body["model"] = route["real_model"]
    url = f"{route['base_url']}/messages"
    headers = _get_downstream_headers(route)
    is_stream = bool(anthropic_body.get("stream", False))
    _capture_io = capture_io_enabled()

    if not is_stream:
        try:
            resp = await client.post(url, json=native_body, headers=headers, timeout=_NON_STREAM_TIMEOUT)
        except _CONNECT_FAILURES as exc:
            # Server unreachable — mark it DOWN and let the caller fall back
            # to the translation path, whose own failover picks an alive
            # server (the native endpoint of a *different* server may not
            # exist, so failing over inside the native path isn't safe).
            mark_down(route["base_url"])
            logger.warning(
                "Native /v1/messages connect failure on {} ({}) — marked DOWN, "
                "falling back to translation", route["base_url"], type(exc).__name__,
            )
            return None
        except Exception as exc:
            logger.error("Downstream error: {}: {}", type(exc).__name__, exc)
            _log_error(user, anthropic_body, exc, 502, model_alias, "/v1/messages", model_type)
            raise HTTPException(status_code=502, detail=downstream_error_detail(exc))
        if resp.status_code in (404, 405):
            return None
        if resp.status_code != 200:
            _log_error(user, anthropic_body, resp.text[:500], resp.status_code, model_alias, "/v1/messages", model_type)
            return _error_response(resp)

        data = resp.json()
        data["model"] = model_alias
        usage = data.get("usage") or {}
        input_tk = usage.get("input_tokens", 0) or 0
        output_tk = usage.get("output_tokens", 0) or 0
        cached_tk = usage.get("cache_read_input_tokens", 0) or 0
        obs_output = (
            {"role": "assistant", "content": data.get("content", [])}
            if _capture_io else None
        )
        text_chars, thinking_chars = _anthropic_content_chars(data.get("content", []))
        diag = empty_turn_warning(
            model_alias, input_tk, output_tk, data.get("stop_reason"),
            text_chars, thinking_chars, req_shape,
        )
        if diag:
            logger.warning(diag)
        _log_usage(
            user, model_alias, model_type, input_tk, output_tk, "/v1/messages",
            route=route, cached_tokens=cached_tk, output_payload=obs_output,
        )
        return JSONResponse(content=data, headers=extra_headers)

    # --- streaming ---
    req = client.build_request("POST", url, json=native_body, headers=headers, timeout=_STREAM_TIMEOUT)
    logger.info("Stream start (native) | user={} model={} endpoint=/v1/messages", user.username, model_alias)
    try:
        resp = await client.send(req, stream=True)
    except _CONNECT_FAILURES as exc:
        # Same contract as the non-stream branch above: mark DOWN, fall back
        # to translation (nothing has been sent to the client yet).
        mark_down(route["base_url"])
        logger.warning(
            "Native /v1/messages connect failure on {} ({}) — marked DOWN, "
            "falling back to translation", route["base_url"], type(exc).__name__,
        )
        return None
    except Exception as exc:
        logger.error("Downstream error: {}: {}", type(exc).__name__, exc)
        _log_error(user, anthropic_body, exc, 502, model_alias, "/v1/messages", model_type)
        raise HTTPException(status_code=502, detail=downstream_error_detail(exc))
    _warn_if_slow_headers(user, model_alias, "/v1/messages", "vllm")

    if resp.status_code in (404, 405):
        await resp.aclose()
        return None
    if resp.status_code != 200:
        # Pre-flight: a 4xx/5xx here is a plain JSON body, not an SSE stream.
        body_bytes = await resp.aread()
        await resp.aclose()
        _log_error(user, anthropic_body, body_bytes[:500], resp.status_code, model_alias, "/v1/messages", model_type)
        try:
            content = json.loads(body_bytes)
        except Exception:
            content = {"error": body_bytes.decode(errors="replace")[:500]}
        return JSONResponse(content=content, status_code=resp.status_code)

    async def _resp_coro():
        return resp

    def _bill(usage: dict[str, int], obs_output: Any) -> None:
        _log_usage(
            user, model_alias, model_type,
            usage.get("input_tokens", 0), usage.get("output_tokens", 0), "/v1/messages",
            route=route, cached_tokens=usage.get("cache_read_input_tokens", 0),
            output_payload=obs_output,
        )

    resp_headers = {"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}
    resp_headers.update(extra_headers)
    return StreamingResponse(
        _relay_native_anthropic_sse(_resp_coro(), model_alias, req_shape, _capture_io, _bill),
        media_type="text/event-stream",
        headers=resp_headers,
    )


async def vllm_forward_messages(
    request: Request,
    user: User,
    allowed_types: list[str],
) -> StreamingResponse | JSONResponse:
    """Accept an Anthropic Messages API request, translate to OpenAI chat
    completions, forward to the downstream server, then translate the response
    back to Anthropic format.

    Routes with ``native_messages = true`` skip the translation entirely and
    pass the Anthropic body straight to the downstream vLLM's native
    /v1/messages endpoint (vLLM >= 0.11.1), falling back to translation when
    the downstream answers 404/405."""
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

    # Phase 2: stash the ORIGINAL Anthropic request as the Langfuse input
    # (shared by both the native and translated paths below).
    if capture_io_enabled():
        set_io_input(anthropic_request_io(anthropic_body))

    req_shape = summarize_request_shape(anthropic_body)

    if route.get("native_messages"):
        result = await _forward_messages_native(
            get_client(), anthropic_body, user,
            resolved_alias, model_type, route, extra_headers, req_shape,
        )
        if result is not None:
            return result
        logger.warning(
            "Downstream {} native /v1/messages unavailable (404/405 — vLLM < 0.11.1? — "
            "or connect failure) — falling back to translation", base_url,
        )

    openai_body = anthropic_to_openai_request(
        anthropic_body, is_reasoning=bool(route.get("is_reasoning")),
    )
    apply_to_openai_body(openai_body, route, resolved_alias, "/v1/messages")
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
        resp, model_alias, route, failover_note = await _post_with_failover(
            client, "/chat/completions", body, model_alias, model_type, route,
        )
    except Exception as exc:
        logger.error("Downstream error: {}: {}", type(exc).__name__, exc)
        _log_error(user, monitor_body or body, exc, 502, model_alias, "/v1/messages", model_type)
        raise HTTPException(status_code=502, detail=downstream_error_detail(exc))
    extra_headers = _merge_failover_header(extra_headers, failover_note)

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
    logger.info("Stream start | user={} model={} endpoint=/v1/messages", user.username, model_alias)

    try:
        opened_resp, model_alias, route, failover_note = await _open_stream_with_failover(
            client, "/chat/completions", body, model_alias, model_type, route,
        )
    except Exception as exc:
        logger.error("Downstream error: {}: {}", type(exc).__name__, exc)
        _log_error(user, monitor_body or body, exc, 502, model_alias, "/v1/messages", model_type)
        raise HTTPException(status_code=502, detail=downstream_error_detail(exc))
    extra_headers = _merge_failover_header(extra_headers, failover_note)
    _warn_if_slow_headers(user, model_alias, "/v1/messages", "vllm")
    _capture_io = capture_io_enabled()

    async def event_generator():
        translator = AnthropicStreamTranslator(model_alias)
        output_acc = StreamingChatOutput()  # Phase 2: assistant turn for Langfuse output
        try:
            for event in translator.start():
                yield event

            async for kind, data in _pump_sse_lines(_opened(opened_resp)):
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
            err_payload = json.dumps({"type": "error", "error": {"type": "api_error", "message": downstream_error_detail(exc)}})
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

    # Native path: vLLM >= 0.11.1 serves /v1/messages/count_tokens directly —
    # counts with the exact chat template the native /v1/messages call will
    # use. Any failure (older vLLM, network) falls through to the /tokenize
    # translation below, which has its own chars/4 last resort.
    if route.get("native_messages"):
        native_body, _dropped = sanitize_native_messages_body(
            normalize_anthropic_messages(anthropic_body)
        )
        native_body = dict(native_body)
        native_body["model"] = real_model
        try:
            resp = await get_client().post(
                f"{base_url}/messages/count_tokens",
                json=native_body, headers=downstream_headers, timeout=30.0,
            )
            if resp.status_code == 200:
                count = resp.json().get("input_tokens")
                if isinstance(count, (int, float)) and not isinstance(count, bool):
                    return JSONResponse(
                        content={"input_tokens": int(count)},
                        headers=extra_headers,
                    )
        except Exception as exc:
            logger.warning(
                "native count_tokens failed for {}: {} — falling back to /tokenize",
                resolved_alias, exc,
            )

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
    # Same dialect alignment as chat completions — a chat-style tokenize
    # payload renders through the same template, so reasoning history must
    # arrive under the name the downstream reads for counts to match.
    _align_reasoning_fields(body_json.get("messages"))
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
        raise HTTPException(status_code=502, detail=downstream_error_detail(exc))

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


# ---------------------------------------------------------------------------
# 7. vllm_forward_render - vLLM-native /chat/completions/render pass-through
# ---------------------------------------------------------------------------

_RENDER_TIMEOUT = 60.0


# Keys the gateway adds to the render response when ?decode=true. Namespaced
# away from anything vLLM returns today, and from the `prompt`/`text` field
# upstream may eventually add itself (vllm-project/vllm#39819).
_DECODED_KEY = "decoded_prompt"
_DECODE_ERROR_KEY = "decode_error"


def _render_token_ids(data: dict) -> list[int] | None:
    """The rendered token ids out of a render response, or None if the
    downstream didn't return any in a shape we recognise."""
    for key in ("token_ids", "prompt_token_ids"):
        value = data.get(key)
        if isinstance(value, list) and all(isinstance(t, int) for t in value):
            return value
    return None


async def _decode_render(data: dict, route: dict[str, Any]) -> None:
    """Add the detokenized prompt to a render response, in place.

    The render endpoint returns token **ids** only, which answers "what did
    the model see" in a form no human reads (vllm-project/vllm#39819 tracks
    fixing that upstream). The gateway closes the gap by default: one extra
    call to the same server's ``/detokenize`` — the same server that rendered,
    so the tokenizer is guaranteed to match — and the text lands under
    ``decoded_prompt``. ``?decode=false`` skips it for a pure pass-through.

    Best-effort by construction: this is a debug convenience layered on top of
    a pass-through, so a failure must not cost the caller the render they
    already have. Any failure leaves the render body intact and records why
    under ``decode_error`` — silently omitting the text would leave the caller
    unable to tell "the decode failed" from "there was nothing to decode".
    """
    token_ids = _render_token_ids(data)
    if token_ids is None:
        data[_DECODE_ERROR_KEY] = "no token_ids in the downstream render response"
        return
    if _DECODED_KEY in data:
        # Downstream already returned it (a newer vLLM closing #39819) —
        # leave its own answer alone.
        return

    try:
        resp = await get_client().post(
            _detokenize_url(route["base_url"]),
            json={"model": route["real_model"], "tokens": token_ids},
            headers=_get_downstream_headers(route),
            timeout=30.0,
        )
    except Exception as exc:
        logger.warning(
            "render decode failed: {}: {} — returning render without text",
            type(exc).__name__, exc,
        )
        data[_DECODE_ERROR_KEY] = f"detokenize request failed: {type(exc).__name__}"
        return

    if resp.status_code != 200:
        logger.warning(
            "render decode failed: /detokenize returned {} — returning render "
            "without text", resp.status_code,
        )
        data[_DECODE_ERROR_KEY] = f"detokenize returned HTTP {resp.status_code}"
        return

    try:
        prompt = resp.json()["prompt"]
    except Exception:
        data[_DECODE_ERROR_KEY] = "detokenize returned an unexpected body"
        return

    data[_DECODED_KEY] = prompt


async def vllm_forward_render(
    request: Request,
    user: User,
    allowed_types: list[str],
    decode: bool = True,
) -> JSONResponse:
    """Pass-through to the downstream vLLM ``/chat/completions/render`` endpoint.

    vLLM can render a chat completions request without running it (the
    endpoint arrived with the disaggregated-serving render server,
    vllm-project/vllm#36166, so it is absent from older downstreams — they
    answer 404 and the gateway propagates it). The response is a single
    engine-level request object: the rendered ``token_ids`` (a list of token
    **IDs**, not decoded text — vllm-project/vllm#39819 tracks adding the
    prompt string), the resolved ``sampling_params``, and the rest of what
    the engine would have been handed. Exposing it here lets developers see
    exactly what the gateway sends downstream and how the model's chat
    template turns it into a prompt — the usual answer to "why did the model
    see something different from what I sent".

    Deliberately **vLLM-only**: the request body is forwarded verbatim to the
    on-prem server (only ``model`` is rewritten, alias → ``real_model``, and
    the serving alias is put back on the response). There is no Azure or
    Bedrock dispatch — those are managed APIs with no rendering surface to
    expose — so a cloud alias posted here falls back through ``_resolve_model``
    to a vLLM route like any other unknown alias, same as ``/v1/tokenize``.

    The reasoning-dialect alignment ``/v1/chat/completions`` applies is applied
    here too, so what gets rendered is what would actually be generated from.

    By default the token ids are run back through the same server's
    ``/detokenize`` and the resulting text is added as ``decoded_prompt`` —
    see ``_decode_render``. This is the one place the endpoint stops being a
    pure pass-through; ``decode=False`` returns exactly what vLLM sent.

    Not billed — rendering runs the tokenizer and chat template, not the
    model — so no row is written to ``usage_logs``. Auth and daily-limit
    checks still apply via ``get_current_user``. A downstream too old to serve
    the endpoint answers 404, which is propagated as-is.
    """
    try:
        body_json = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")
    if not isinstance(body_json, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    model_name = body_json.get("model", "")
    resolved_alias, route, fallback_reason = _resolve_model(model_name, allowed_types)
    model_type = route["type"]
    extra_headers = _fallback_headers(fallback_reason)

    # Same dialect alignment as chat completions — render must reflect the
    # body the generating call would send, not the one the client typed.
    _align_reasoning_fields(body_json.get("messages"))
    apply_to_openai_body(
        body_json, route, resolved_alias, "/v1/chat/completions/render",
    )

    client = get_client()
    try:
        resp, resolved_alias, route, failover_note = await _post_with_failover(
            client, "/chat/completions/render", body_json, resolved_alias,
            model_type, route, timeout=_RENDER_TIMEOUT,
        )
    except Exception as exc:
        logger.error("render downstream error: {}: {}", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail=downstream_error_detail(exc))
    extra_headers = _merge_failover_header(extra_headers, failover_note)

    if resp.status_code != 200:
        return _error_response(resp)

    try:
        data = resp.json()
    except Exception:
        return JSONResponse(
            content={"error": "Downstream returned non-JSON response"},
            status_code=502,
        )

    # Put the user-facing alias back on the echoed request so the response
    # never leaks the downstream's real model name (same contract as every
    # other surface).
    if isinstance(data, dict) and data.get("model") == route["real_model"]:
        data["model"] = resolved_alias

    if decode and isinstance(data, dict):
        await _decode_render(data, route)

    return JSONResponse(content=data, headers=extra_headers)


# ---------------------------------------------------------------------------
# 8. vllm_forward_systemone - System One typed decisions (/v1/systemone)
# ---------------------------------------------------------------------------

# A ``[models.systemone.*]`` downstream speaks TypeSafe's System One wire
# format at ``/v1/systemone`` under its server root — e.g. Mapika's decider
# (``decider/serve.py``). It is not vLLM, so its ``base_url`` may be the bare
# server root (``http://host:8000``) or end in ``/v1`` like a vLLM route;
# ``_systemone_url`` accepts both. It rides the shared on-prem machinery
# otherwise: the health loop (which probes decider's own ``/health`` —
# ``health._probe_systemone``), failover, billing and observability.
_SYSTEMONE_ENDPOINT = "/v1/systemone"


def _systemone_url(route: dict[str, Any]) -> str:
    """``{server root}/v1/systemone`` for a route, whether its ``base_url``
    is the server root or ends in ``/v1``."""
    return f"{_server_root(route['base_url'])}/v1/systemone"


def _default_alias(allowed_types: list[str]) -> str:
    """The alias that serves a request naming no model.

    ``model`` is optional in the System One wire format, so leaving it out is
    a legitimate request, not a typo. It resolves the way ``_resolve_model``
    ranks candidates — the ``[fallback]`` entry for the type, else the first
    configured alias of the type — but as an exact match, so the request
    isn't logged as a WARNING-level fallback with an ``X-Model-Fallback``
    header on every call. A default whose server is down still falls back
    normally. ``""`` when no route of these types exists (``_resolve_model``
    then answers its usual 400).
    """
    for model_type in allowed_types:
        fb_alias = FALLBACK_MAP.get(model_type)
        if fb_alias and MODEL_ROUTING.get(fb_alias, {}).get("type") == model_type:
            return fb_alias
    for alias, route in dict(MODEL_ROUTING).items():
        if route["type"] in allowed_types:
            return alias
    return ""


def _usage_count(usage: dict, key: str) -> int:
    """A non-negative token count from a downstream ``usage`` block; 0 when
    absent or malformed, so a third-party server's odd usage can't turn an
    answered request into a 500 at the billing step."""
    try:
        return max(0, int(usage.get(key) or 0))
    except (TypeError, ValueError, OverflowError):  # OverflowError: JSON Infinity
        return 0


async def vllm_forward_systemone(
    request: Request,
    user: User,
    allowed_types: list[str],
) -> JSONResponse:
    """Pass-through to a System One downstream's ``/v1/systemone`` (see
    ``_systemone_url``: the route's ``base_url`` may omit ``/v1``).

    A System One model answers typed questions about a ``state`` in a single
    forward pass — each question a ``choice``, a ``score`` or a ``noul``
    (yes/no) — and returns calibrated probabilities under ``answers`` rather
    than generated text. The body is forwarded verbatim (``state``,
    ``questions``, ``independent``, ``layout``, ...); only ``model`` is
    rewritten (alias → ``real_model``), and the response's ``model`` is set
    to the serving alias. A request without ``model`` is served by the
    type's default model (see ``_default_alias``). This is the path the
    TypeSafe SDK calls (``POST {TYPESAFE_BASE_URL}/v1/systemone``).

    On-prem only: there is no Azure/Bedrock dispatch (neither offers a
    System One surface), so an unknown or cloud alias falls back through
    ``_resolve_model`` to a systemone route like any other unknown alias.

    Billed through ``_log_usage`` like embeddings/rerank. The downstream
    reports Anthropic-style ``usage``, and decider's schema-cache layout
    reports the cached question prefix as ``cached_tokens`` *next to*
    ``input_tokens`` (which then counts only the uncached state tokens), not
    inside it. The billed input is therefore their sum, with
    ``cached_tokens`` as its cached portion: the model entry's
    ``cached_input_price_per_1m`` discounts it when set, and otherwise every
    token bills at the input price — the same default as every other
    backend. ``output_tokens`` is 0, since nothing is generated.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    requested = body.get("model")
    model_name = requested if isinstance(requested, str) else ""
    if not model_name:
        model_name = _default_alias(allowed_types)
    resolved_alias, route, fallback_reason = _resolve_model(model_name, allowed_types)
    model_type = route["type"]
    extra_headers = _fallback_headers(fallback_reason)

    # Phase 2: capture the request in the endpoint's own shape.
    if capture_io_enabled():
        set_io_input({"state": body.get("state"), "questions": body.get("questions")})

    client = get_client()
    try:
        resp, resolved_alias, route, failover_note = await _post_with_failover(
            client, "/systemone", body, resolved_alias, model_type, route,
            url_for=_systemone_url,
        )
    except Exception as exc:
        logger.error("systemone downstream error: {}: {}", type(exc).__name__, exc)
        _log_error(user, body, exc, 502, resolved_alias, _SYSTEMONE_ENDPOINT, model_type)
        raise HTTPException(status_code=502, detail=downstream_error_detail(exc))
    extra_headers = _merge_failover_header(extra_headers, failover_note)

    # 413 (request too large), 422 (invalid questions) and 503 (queue full —
    # already failed over to another server when one exists) are the
    # downstream's to explain; pass them through unbilled.
    if resp.status_code != 200:
        _log_error(
            user, body, resp.text[:500], resp.status_code, resolved_alias,
            _SYSTEMONE_ENDPOINT, model_type,
        )
        return _error_response(resp)

    try:
        data = resp.json()
    except Exception:
        _log_error(
            user, body, "downstream returned non-JSON response", 502,
            resolved_alias, _SYSTEMONE_ENDPOINT, model_type,
        )
        return JSONResponse(
            content={"error": "Downstream returned non-JSON response"},
            status_code=502,
        )
    if not isinstance(data, dict):
        _log_error(
            user, body, "downstream returned a non-object JSON response", 502,
            resolved_alias, _SYSTEMONE_ENDPOINT, model_type,
        )
        return JSONResponse(
            content={"error": "Downstream returned a non-object JSON response"},
            status_code=502,
        )

    # decider echoes its own name ("decider" by default); callers know the
    # model by its alias. Set unconditionally: the TypeSafe SDK requires a
    # string `model` on every response.
    data["model"] = resolved_alias

    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    input_tk = _usage_count(usage, "input_tokens")
    cached_tk = _usage_count(usage, "cached_tokens")
    output_tk = _usage_count(usage, "output_tokens")
    if input_tk + cached_tk == 0:
        logger.warning(
            "systemone response reported no input tokens — billed as 0 | "
            "user={} model={} (does the downstream return usage?)",
            user.username, resolved_alias,
        )
    obs_output = data.get("answers") if capture_io_enabled() else None
    _log_usage(
        user, resolved_alias, model_type, input_tk + cached_tk, output_tk,
        _SYSTEMONE_ENDPOINT, route=route, cached_tokens=cached_tk,
        output_payload=obs_output,
    )
    return JSONResponse(content=data, headers=extra_headers)


async def _passthrough_stream(
    client, body_json: dict, headers: dict,
    user: User, model: str, model_type: str, path_suffix: str,
    extra_headers: dict[str, str] | None = None,
    route: dict[str, Any] | None = None,
) -> StreamingResponse:
    try:
        opened_resp, model, route, failover_note = await _open_stream_with_failover(
            client, path_suffix, body_json, model, model_type, route,
        )
    except Exception as exc:
        logger.error("Downstream error: {}: {}", type(exc).__name__, exc)
        _log_error(user, body_json, exc, 502, model, path_suffix, model_type)
        raise HTTPException(status_code=502, detail=downstream_error_detail(exc))
    extra_headers = _merge_failover_header(extra_headers, failover_note)
    _warn_if_slow_headers(user, model, path_suffix, "vllm")
    _capture_io = capture_io_enabled()

    async def event_generator():
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        output_acc = StreamingChatOutput()  # Phase 2: chat-shape assistant turn
        responses_output = None  # Phase 2: full Responses output[] from the terminal event
        # Pump owns aclose + the max-idle ceiling (see _stream_chat).
        async for kind, data in _pump_sse_lines(_opened(opened_resp)):
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
