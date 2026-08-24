"""
Azure OpenAI proxy — Responses API edition.

All Azure LLM/VLM traffic from the gateway goes to the v1 Responses API
endpoint (``{endpoint}/openai/v1/responses``). Clients still speak OpenAI
chat completions (on ``/azure/v1/chat/completions``) or Anthropic Messages
(on ``/azure/v1/messages``); the translation to/from Responses happens
internally via ``responses_adapter``.

Why: newer Azure deployments (gpt-5 series, o-series pro variants) reject
``/chat/completions`` with 400 "operation unsupported". Routing every Azure
call through Responses API keeps a single code path and makes the gateway
forward-compatible with future reasoning-only models.

Key conventions:
  * URL pattern: ``{endpoint}/openai/v1/responses`` (no api-version query)
  * Auth header: ``api-key: <key>`` (resource key) — AAD tokens not supported
  * Body's ``model`` field is set to the Azure *deployment name*; the
    public-facing alias is held separately for logging/cost lookup.
  * Embeddings are not supported via Responses API; ``/azure/v1/embeddings``
    is intentionally not exposed.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.config import AZURE_FALLBACK_MAP, AZURE_MODELS, _check_auto_reload
from app.core.logger import logger
from app.core.server_state import get_azure_client
from app.models.schema import User
from app.services.anthropic_adapter import (
    AnthropicStreamTranslator,
    anthropic_request_io,
    anthropic_to_openai_request,
    openai_message_to_anthropic,
    openai_to_anthropic_response,
)
from app.services.redact import summarize_body
from app.services.responses_adapter import (
    ResponsesToChatStreamTranslator,
    openai_chat_to_responses_request,
    responses_to_openai_chat_response,
)
from app.services.observability import (
    StreamingChatOutput,
    capture_io_enabled,
    set_io_input,
)
from app.services.vllm_proxy import (
    _ANTHROPIC_PING_EVENT,
    _NON_STREAM_TIMEOUT,
    _approx_token_count,
    _error_response,
    _log_error,
    _log_usage,
    _pump_sse_lines,
)


_AZURE_DEFAULT_ALLOWED_TYPES: tuple[str, ...] = ("llm", "vlm")


def _is_usable_entry(entry: dict[str, Any]) -> bool:
    return bool(entry.get("endpoint")) and bool(entry.get("deployment"))


def _resolve_azure(
    alias: str,
    allowed_types: list[str] | tuple[str, ...] | None = None,
) -> tuple[str, dict[str, Any], str | None]:
    """Resolve an Azure alias with type-aware fallback.

    Priority (mirrors vllm_proxy._resolve_model minus the health check —
    Azure deployments are managed, so liveness is reactive at request time
    rather than proactively probed):
      1. Exact alias match whose type is in ``allowed_types`` → use as-is.
      2. ``AZURE_FALLBACK_MAP[type]`` for some type in ``allowed_types``.
      3. First entry in ``AZURE_MODELS`` whose type is in ``allowed_types``.

    Returns ``(resolved_alias, entry, fallback_reason)``. ``fallback_reason``
    is ``None`` when the requested alias was used directly; otherwise it's
    a human-readable string surfaced via the ``X-Model-Fallback`` response
    header (same convention as the vLLM path).

    Raises ``HTTPException(400)`` when nothing is usable.
    """
    _check_auto_reload()
    if allowed_types is None:
        allowed_types = _AZURE_DEFAULT_ALLOWED_TYPES
    allowed = tuple(allowed_types)

    entry = AZURE_MODELS.get(alias)
    if entry and entry.get("type", "llm") in allowed:
        if not _is_usable_entry(entry):
            raise HTTPException(
                status_code=500,
                detail=f"Azure model '{alias}' missing endpoint or deployment.",
            )
        return alias, entry, None

    if entry is None:
        reason = f"Azure model '{alias}' not configured"
    else:
        reason = (
            f"Azure model '{alias}' has type '{entry.get('type')}' "
            f"but endpoint requires one of {list(allowed)}"
        )

    # Configured fallback first
    for at in allowed:
        fb_alias = AZURE_FALLBACK_MAP.get(at)
        if (
            fb_alias and fb_alias != alias and fb_alias in AZURE_MODELS
            and AZURE_MODELS[fb_alias].get("type", "llm") in allowed
            and _is_usable_entry(AZURE_MODELS[fb_alias])
        ):
            logger.warning(
                "{} — falling back to configured Azure fallback '{}'",
                reason, fb_alias,
            )
            return fb_alias, AZURE_MODELS[fb_alias], reason

    # Auto fallback: first compatible Azure entry
    for fb_alias, fb_entry in AZURE_MODELS.items():
        if (
            fb_alias != alias
            and fb_entry.get("type", "llm") in allowed
            and _is_usable_entry(fb_entry)
        ):
            logger.warning("{} — falling back to Azure '{}'", reason, fb_alias)
            return fb_alias, fb_entry, reason

    raise HTTPException(
        status_code=400,
        detail=f"No Azure model available for types {list(allowed)}.",
    )


def _fallback_headers(fallback_reason: str | None) -> dict[str, str]:
    if fallback_reason:
        return {"X-Model-Fallback": fallback_reason}
    return {}


def _build_responses_url(entry: dict[str, Any]) -> str:
    """Azure Responses v1 surface: no deployment in URL, no api-version.

    Defensively strips a trailing ``/openai`` from the configured endpoint
    so operators can paste either the bare host
    (``https://x.cognitiveservices.azure.com``) or the Roo Code-style base
    URL (``https://x.cognitiveservices.azure.com/openai``) without
    producing a doubled ``/openai/openai/...`` path.
    """
    endpoint = entry["endpoint"].rstrip("/")
    if endpoint.endswith("/openai"):
        endpoint = endpoint[: -len("/openai")]
    return f"{endpoint}/openai/v1/responses"


def _build_headers(entry: dict[str, Any]) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "api-key": entry.get("api_key", ""),
    }


def _cached_tokens_from_responses(usage: dict) -> int:
    """Responses API reports prompt-cache hits under
    ``usage.input_tokens_details.cached_tokens``.
    """
    details = usage.get("input_tokens_details") or {}
    return details.get("cached_tokens", 0) or 0


def _has_input(responses_body: dict[str, Any]) -> bool:
    """Azure rejects Responses requests where neither `input` nor
    `previous_response_id` is provided. `input` may be a non-empty string
    or a non-empty list; an empty list still 400s."""
    if responses_body.get("previous_response_id"):
        return True
    inp = responses_body.get("input")
    if isinstance(inp, str):
        return bool(inp)
    if isinstance(inp, list):
        return len(inp) > 0
    return False


_PROBE_PLACEHOLDER_INPUT: list[dict[str, Any]] = [{
    "role": "user",
    "content": [{"type": "input_text", "text": "."}],
}]


def _ensure_input(
    responses_body: dict[str, Any],
    incoming_body: dict[str, Any],
    alias: str,
    endpoint: str,
) -> None:
    """If translation collapsed to empty ``input`` (typical Roo Code probe
    sending only a system message), inject a minimal placeholder so Azure
    returns 200 instead of 400. Logs the original request so operators can
    tell genuine probes from translator regressions.
    """
    if _has_input(responses_body):
        return
    msgs = incoming_body.get("messages") if isinstance(incoming_body, dict) else None
    role_summary = (
        [m.get("role") for m in msgs if isinstance(m, dict)]
        if isinstance(msgs, list) else None
    )
    in_snippet = summarize_body(incoming_body)
    logger.warning(
        "Empty Responses input after translation — injecting probe placeholder | "
        "endpoint={} model={} message_count={} roles={} incoming_shape={}",
        endpoint, alias,
        len(role_summary) if role_summary is not None else None,
        role_summary, in_snippet,
    )
    responses_body["input"] = [dict(item) for item in _PROBE_PLACEHOLDER_INPUT]


def _summarize_input_items(input_items: Any) -> str:
    """Compact summary of a Responses `input` array, used for diagnostics
    on 400s that complain about function-call/output pairing. Emits a
    line-per-item with item type, role, call_id (if any), and any
    call_id-only function_call → function_call_output orphan pairs.
    """
    if not isinstance(input_items, list):
        return f"<input is {type(input_items).__name__}, not a list>"
    rows: list[str] = []
    function_calls: dict[str, int] = {}     # call_id -> position
    function_outputs: set[str] = set()
    for i, item in enumerate(input_items):
        if not isinstance(item, dict):
            rows.append(f"{i}: <non-dict {type(item).__name__}>")
            continue
        itype = item.get("type") or "message"
        role = item.get("role")
        cid = item.get("call_id")
        bits = [f"{i}:{itype}"]
        if role:
            bits.append(f"role={role}")
        if cid:
            bits.append(f"call_id={cid}")
        rows.append(" ".join(bits))
        if itype == "function_call" and cid:
            function_calls[cid] = i
        elif itype == "function_call_output" and cid:
            function_outputs.add(cid)
    orphans = sorted(cid for cid in function_calls if cid not in function_outputs)
    extra = sorted(cid for cid in function_outputs if cid not in function_calls)
    summary = " | ".join(rows)
    notes = []
    if orphans:
        notes.append(f"function_calls without output: {orphans}")
    if extra:
        notes.append(f"function_call_outputs without call: {extra}")
    if notes:
        summary += " || " + " ; ".join(notes)
    return summary


def _log_azure_error(
    incoming_body: dict[str, Any],
    sent_body: dict[str, Any],
    resp_text: str,
    status: int,
    alias: str,
    endpoint: str,
) -> None:
    """Surface Azure 4xx/5xx responses with both halves of the conversation
    so an operator can see what the client asked, what the gateway
    translated to, and how Azure objected."""
    # Shape only — the bodies are the user's prompt. See services/redact.py.
    sent_snippet = summarize_body(sent_body)
    in_snippet = summarize_body(incoming_body)
    input_summary = _summarize_input_items(sent_body.get("input")) if isinstance(sent_body, dict) else ""
    logger.warning(
        "Azure returned {} | endpoint={} model={} resp={} | input_summary={} | "
        "sent_shape={} | incoming_shape={}",
        status, endpoint, alias, resp_text[:1000], input_summary,
        sent_snippet, in_snippet,
    )


def _warn_if_empty_translation(
    raw: dict[str, Any],
    chat_data: dict[str, Any],
    alias: str,
    endpoint: str,
) -> None:
    """Emit a WARNING when the Responses->chat-completions translation
    yielded an empty assistant message (no text, no tool calls). Clients
    surface this as "no assistant message"; the log line lets operators
    see what Azure actually returned so the adapter can be adjusted.

    Suppressed when there's any text content, any tool calls, or any
    reasoning content — those are valid non-empty outputs (Roo Code's
    "no assistant message" message specifically means content == "").
    """
    msg = (chat_data.get("choices") or [{}])[0].get("message") or {}
    if msg.get("content") or msg.get("tool_calls"):
        return
    output_types = [
        it.get("type") for it in (raw.get("output") or []) if isinstance(it, dict)
    ]
    try:
        snippet = json.dumps(raw, ensure_ascii=False)[:800]
    except Exception:
        snippet = repr(raw)[:800]
    logger.warning(
        "Empty assistant content from Azure | endpoint={} model={} "
        "status={} output_types={} usage={} raw_head={}",
        endpoint,
        alias,
        raw.get("status"),
        output_types,
        raw.get("usage"),
        snippet,
    )


def _parse_responses_sse_event(data_str: str) -> dict[str, Any] | None:
    """Parse a Responses API SSE `data:` payload into a dict, or None
    on parse failure / sentinel."""
    if not data_str or data_str == "[DONE]":
        return None
    try:
        return json.loads(data_str)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Chat Completions (`/azure/v1/chat/completions`)
# ---------------------------------------------------------------------------

async def azure_forward_chat_completions(
    request: Request,
    user: User,
) -> StreamingResponse | JSONResponse:
    body = await request.json()
    requested_alias = body.get("model", "")
    resolved_alias, entry, fallback_reason = _resolve_azure(
        requested_alias, allowed_types=["llm", "vlm"],
    )
    model_type = entry.get("type", "llm")
    deployment = entry["deployment"]

    is_stream = bool(body.get("stream", False))
    responses_body = openai_chat_to_responses_request(body, model=deployment)
    if is_stream:
        responses_body["stream"] = True

    _ensure_input(responses_body, body, resolved_alias, "/azure/v1/chat/completions")

    # Phase 2: capture the (OpenAI-shaped) request messages as Langfuse input.
    if capture_io_enabled():
        set_io_input(body.get("messages"))

    target_url = _build_responses_url(entry)
    headers = _build_headers(entry)
    client = get_azure_client()

    # Bill the resolved alias (what we actually used) but show the original
    # in monitor logs so operators see what the client asked for.
    monitor_body = {**body, "model": resolved_alias}
    extra_headers = _fallback_headers(fallback_reason)

    if is_stream:
        return await _stream_chat(
            client, target_url, responses_body, headers,
            user, resolved_alias, model_type, monitor_body, entry, body,
            extra_headers,
        )
    return await _non_stream_chat(
        client, target_url, responses_body, headers,
        user, resolved_alias, model_type, monitor_body, entry, body,
        extra_headers,
    )


async def _non_stream_chat(
    client, url: str, body: dict, headers: dict, user: User,
    alias: str, model_type: str, monitor_body: dict, route: dict,
    incoming_body: dict, extra_headers: dict[str, str] | None = None,
) -> JSONResponse:
    try:
        resp = await client.post(url, json=body, headers=headers, timeout=_NON_STREAM_TIMEOUT)
    except Exception as exc:
        logger.error("Azure downstream error: {}", exc)
        _log_error(user, monitor_body, str(exc), 502, alias,
                          "/azure/v1/chat/completions", model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        _log_azure_error(incoming_body, body, resp.text, resp.status_code,
                         alias, "/azure/v1/chat/completions")
        _log_error(user, monitor_body, resp.text[:500], resp.status_code,
                          alias, "/azure/v1/chat/completions", model_type)
        return _error_response(resp)

    raw = resp.json()
    chat_data = responses_to_openai_chat_response(raw, alias)
    _warn_if_empty_translation(raw, chat_data, alias, "/azure/v1/chat/completions")
    usage = chat_data.get("usage", {})
    input_tk = usage.get("prompt_tokens", 0)
    output_tk = usage.get("completion_tokens", 0)
    cached_tk = _cached_tokens_from_responses(raw.get("usage") or {})
    obs_output = (chat_data.get("choices") or [{}])[0].get("message") if capture_io_enabled() else None
    _log_usage(user, alias, model_type, input_tk, output_tk,
               "/azure/v1/chat/completions", route=route, cached_tokens=cached_tk, backend="azure", output_payload=obs_output)
    return JSONResponse(content=chat_data, headers=extra_headers or None)


async def _stream_chat(
    client, url: str, body: dict, headers: dict, user: User,
    alias: str, model_type: str, monitor_body: dict, route: dict,
    incoming_body: dict, extra_headers: dict[str, str] | None = None,
) -> StreamingResponse | JSONResponse:
    # Pre-flight: open the stream and check status BEFORE handing it to the
    # SSE pump. Without this an Azure 4xx is returned as a JSON error body
    # whose lines don't begin with `data: ` and get silently dropped, making
    # the client see an empty-but-successful stream.
    req = client.build_request("POST", url, json=body, headers=headers, timeout=None)
    try:
        resp = await client.send(req, stream=True)
    except Exception as exc:
        logger.error("Azure stream connect error: {}", exc)
        _log_error(user, monitor_body, str(exc), 502, alias,
                          "/azure/v1/chat/completions", model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        err_bytes = await resp.aread()
        await resp.aclose()
        err_text = err_bytes.decode("utf-8", "replace")
        _log_azure_error(incoming_body, body, err_text, resp.status_code,
                         alias, "/azure/v1/chat/completions")
        _log_error(user, monitor_body, err_text[:500], resp.status_code,
                          alias, "/azure/v1/chat/completions", model_type)
        try:
            err_json = json.loads(err_text)
        except Exception:
            err_json = {"error": {"message": err_text[:500]}}
        return JSONResponse(status_code=resp.status_code, content=err_json)

    _capture_io = capture_io_enabled()
    logger.info("Stream start | user={} model={} endpoint=/azure/v1/chat/completions",
                user.username, alias)

    async def _resp_coro():
        # _pump_sse_lines awaits its argument and calls aclose() in
        # its finally block, so wrap the already-opened response in a coro
        # to reuse the pump's lifecycle/ping handling without re-sending.
        return resp

    async def event_generator():
        translator = ResponsesToChatStreamTranslator(alias)
        output_acc = StreamingChatOutput()  # Phase 2: assistant turn for Langfuse output
        try:
            for chunk in translator.start():
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

            async for kind, data in _pump_sse_lines(_resp_coro()):
                if kind == "ping":
                    # vLLM-style ping isn't part of the chat completions SSE
                    # contract; emit an SSE comment so proxies don't strip it.
                    yield ": ping\n\n"
                    continue
                if kind == "err":
                    raise data
                if kind == "done":
                    break
                line = data
                if not line or not line.startswith("data: "):
                    continue
                event = _parse_responses_sse_event(line[6:])
                if event is None:
                    continue
                for chunk in translator.handle_event(event):
                    if _capture_io:
                        output_acc.add_delta((chunk.get("choices") or [{}])[0].get("delta") or {})
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

            for chunk in translator.finish():
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            az_err = translator.derive_error_message()
            if az_err:
                # Azure emitted response.failed / response.error mid-stream.
                # Surface it on the SSE so the client doesn't think the
                # turn completed normally with empty content.
                yield (
                    "data: "
                    + json.dumps({"error": {"message": f"Azure: {az_err}", "type": "api_error"}})
                    + "\n\n"
                )
            yield "data: [DONE]\n\n"
        except Exception as exc:
            logger.error("Azure chat stream error: {}", exc)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

        input_tk = translator.input_tokens
        output_tk = translator.output_tokens
        cached_tk = translator.cached_tokens
        err_msg = translator.derive_error_message()
        if input_tk == 0 and output_tk == 0:
            logger.warning("Azure chat stream for model={} ended with 0 tokens", alias)
        if (
            translator.emitted_text_chars == 0
            and not translator._tool_meta_sent  # noqa: SLF001
        ):
            logger.warning(
                "Empty assistant content from Azure stream | endpoint=/azure/v1/chat/completions "
                "model={} input_tokens={} output_tokens={} reasoning_chars={} finish={} "
                "event_types={} error={} sent_input_summary={}",
                alias, input_tk, output_tk,
                translator.emitted_reasoning_chars, translator.finish_reason,
                translator.event_type_counts, err_msg,
                _summarize_input_items(body.get("input")),
            )
        obs_output = output_acc.as_message() if _capture_io else None
        _log_usage(user, alias, model_type, input_tk, output_tk,
                   "/azure/v1/chat/completions", route=route, cached_tokens=cached_tk, backend="azure", output_payload=obs_output)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            **(extra_headers or {}),
        },
    )


# ---------------------------------------------------------------------------
# Anthropic Messages API (`/azure/v1/messages`)
# ---------------------------------------------------------------------------

async def azure_forward_messages(
    request: Request,
    user: User,
) -> StreamingResponse | JSONResponse:
    try:
        anthropic_body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    requested_alias = anthropic_body.get("model", "")
    resolved_alias, entry, fallback_reason = _resolve_azure(
        requested_alias, allowed_types=["llm", "vlm"],
    )
    model_type = entry.get("type", "llm")
    deployment = entry["deployment"]

    openai_body = anthropic_to_openai_request(
        anthropic_body, is_reasoning=bool(entry.get("is_reasoning")),
    )
    is_stream = bool(anthropic_body.get("stream", False))
    responses_body = openai_chat_to_responses_request(openai_body, model=deployment)
    if is_stream:
        responses_body["stream"] = True

    _ensure_input(responses_body, anthropic_body, resolved_alias, "/azure/v1/messages")

    # Phase 2: capture the ORIGINAL Anthropic request as Langfuse input so the
    # trace stays in Anthropic shape (not the internal OpenAI pivot).
    if capture_io_enabled():
        set_io_input(anthropic_request_io(anthropic_body))

    target_url = _build_responses_url(entry)
    headers = _build_headers(entry)
    client = get_azure_client()

    monitor_body = dict(anthropic_body)
    monitor_body["model"] = resolved_alias
    extra_headers = _fallback_headers(fallback_reason)

    if is_stream:
        return await _stream_messages(
            client, target_url, responses_body, headers,
            user, resolved_alias, model_type, monitor_body, entry, anthropic_body,
            extra_headers,
        )
    return await _non_stream_messages(
        client, target_url, responses_body, headers,
        user, resolved_alias, model_type, monitor_body, entry, anthropic_body,
        extra_headers,
    )


async def _non_stream_messages(
    client, url: str, body: dict, headers: dict, user: User,
    alias: str, model_type: str, monitor_body: dict, route: dict,
    incoming_body: dict, extra_headers: dict[str, str] | None = None,
) -> JSONResponse:
    try:
        resp = await client.post(url, json=body, headers=headers, timeout=_NON_STREAM_TIMEOUT)
    except Exception as exc:
        logger.error("Azure messages downstream error: {}", exc)
        _log_error(user, monitor_body, str(exc), 502, alias,
                          "/azure/v1/messages", model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        _log_azure_error(incoming_body, body, resp.text, resp.status_code,
                         alias, "/azure/v1/messages")
        _log_error(user, monitor_body, resp.text[:500], resp.status_code,
                          alias, "/azure/v1/messages", model_type)
        return _error_response(resp)

    raw = resp.json()
    chat_data = responses_to_openai_chat_response(raw, alias)
    _warn_if_empty_translation(raw, chat_data, alias, "/azure/v1/messages")
    anthropic_data = openai_to_anthropic_response(chat_data, alias)
    input_tk = anthropic_data["usage"]["input_tokens"]
    output_tk = anthropic_data["usage"]["output_tokens"]
    cached_tk = _cached_tokens_from_responses(raw.get("usage") or {})
    obs_output = (
        {"role": "assistant", "content": anthropic_data["content"]}
        if capture_io_enabled() else None
    )
    _log_usage(user, alias, model_type, input_tk, output_tk,
               "/azure/v1/messages", route=route, cached_tokens=cached_tk, backend="azure", output_payload=obs_output)
    return JSONResponse(content=anthropic_data, headers=extra_headers or None)


async def _stream_messages(
    client, url: str, body: dict, headers: dict, user: User,
    alias: str, model_type: str, monitor_body: dict, route: dict,
    incoming_body: dict, extra_headers: dict[str, str] | None = None,
) -> StreamingResponse | JSONResponse:
    # Pre-flight to surface 4xx before opening the SSE channel — same
    # rationale as _stream_chat.
    req = client.build_request("POST", url, json=body, headers=headers, timeout=None)
    try:
        resp = await client.send(req, stream=True)
    except Exception as exc:
        logger.error("Azure messages stream connect error: {}", exc)
        _log_error(user, monitor_body, str(exc), 502, alias,
                          "/azure/v1/messages", model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        err_bytes = await resp.aread()
        await resp.aclose()
        err_text = err_bytes.decode("utf-8", "replace")
        _log_azure_error(incoming_body, body, err_text, resp.status_code,
                         alias, "/azure/v1/messages")
        _log_error(user, monitor_body, err_text[:500], resp.status_code,
                          alias, "/azure/v1/messages", model_type)
        try:
            err_json = json.loads(err_text)
        except Exception:
            err_json = {"type": "error", "error": {"type": "api_error", "message": err_text[:500]}}
        return JSONResponse(status_code=resp.status_code, content=err_json)

    _capture_io = capture_io_enabled()
    logger.info("Stream start | user={} model={} endpoint=/azure/v1/messages",
                user.username, alias)

    async def _resp_coro():
        return resp

    async def event_generator():
        # Two-stage translator chain: Responses SSE -> synthetic OpenAI chat
        # chunks -> Anthropic SSE events. Reuses the existing Anthropic
        # translator so the public Anthropic surface stays unchanged.
        responses_xlat = ResponsesToChatStreamTranslator(alias)
        anthropic_xlat = AnthropicStreamTranslator(alias)
        output_acc = StreamingChatOutput()  # Phase 2: assistant turn for Langfuse output

        def _feed(chat_chunks) -> Iterator:
            for ch in chat_chunks:
                if _capture_io:
                    output_acc.add_delta((ch.get("choices") or [{}])[0].get("delta") or {})
                yield from anthropic_xlat.handle_chunk(ch)

        try:
            for event in anthropic_xlat.start():
                yield event
            # Drain the start() chunk(s) through the Anthropic translator so
            # the initial role delta primes its state machine.
            for event in _feed(responses_xlat.start()):
                yield event

            async for kind, data in _pump_sse_lines(_resp_coro()):
                if kind == "ping":
                    yield _ANTHROPIC_PING_EVENT
                    continue
                if kind == "err":
                    raise data
                if kind == "done":
                    break
                line = data
                if not line or not line.startswith("data: "):
                    continue
                event_data = _parse_responses_sse_event(line[6:])
                if event_data is None:
                    continue
                resp_chunks = list(responses_xlat.handle_event(event_data))
                if resp_chunks:
                    for event in _feed(resp_chunks):
                        yield event

            # Final chunk carries usage + finish_reason for the Anthropic
            # translator to compute message_delta + message_stop.
            for event in _feed(responses_xlat.finish()):
                yield event

            az_err = responses_xlat.derive_error_message()
            if az_err:
                # Azure emitted response.failed / response.error mid-stream.
                # Translate it into an Anthropic-shape error so the client
                # sees an actual failure instead of an empty completion.
                # error_kind picks the right Anthropic error_type so
                # Claude Code retries overload but bails on invalid_request
                # instead of looping fruitlessly.
                error_kind = responses_xlat.derive_error_kind()
                logger.warning(
                    "Azure messages stream surfaced error | model={} error={} kind={}",
                    alias, az_err, error_kind,
                )
                for event in anthropic_xlat.fail(f"Azure: {az_err}", error_type=error_kind):
                    yield event
            elif anthropic_xlat.stop_reason is None:
                logger.warning(
                    "Azure messages stream ended without finish_reason | model={} — truncated",
                    alias,
                )
                for event in anthropic_xlat.fail(
                    "Downstream stream ended prematurely; the response may be incomplete."
                ):
                    yield event
            else:
                for event in anthropic_xlat.finish():
                    yield event
        except Exception as exc:
            logger.error("Azure messages stream error: {}", exc)
            err_payload = json.dumps({"type": "error", "error": {"type": "api_error", "message": str(exc)}})
            yield f"event: error\ndata: {err_payload}\n\n"

        input_tk = anthropic_xlat.input_tokens or responses_xlat.input_tokens
        output_tk = anthropic_xlat.output_tokens or responses_xlat.output_tokens
        cached_tk = responses_xlat.cached_tokens
        err_msg = responses_xlat.derive_error_message()
        if (
            responses_xlat.emitted_text_chars == 0
            and not responses_xlat._tool_meta_sent  # noqa: SLF001
        ):
            logger.warning(
                "Empty assistant content from Azure stream | endpoint=/azure/v1/messages "
                "model={} input_tokens={} output_tokens={} reasoning_chars={} finish={} "
                "event_types={} error={} sent_input_summary={}",
                alias, input_tk, output_tk,
                responses_xlat.emitted_reasoning_chars, responses_xlat.finish_reason,
                responses_xlat.event_type_counts, err_msg,
                _summarize_input_items(body.get("input")),
            )
        obs_output = (
            openai_message_to_anthropic(output_acc.as_message(), alias)
            if _capture_io else None
        )
        _log_usage(user, alias, model_type, input_tk, output_tk,
                   "/azure/v1/messages", route=route, cached_tokens=cached_tk, backend="azure", output_payload=obs_output)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            **(extra_headers or {}),
        },
    )


async def azure_forward_count_tokens(
    request: Request,
    user: User,
) -> JSONResponse:
    """Anthropic /v1/messages/count_tokens for Azure deployments.

    Azure has no tokenize endpoint, so we return a chars/4 estimate.
    Auth/daily-limit checks still apply; not billed.
    """
    try:
        anthropic_body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    alias = anthropic_body.get("model", "")
    _, _entry, fallback_reason = _resolve_azure(alias, allowed_types=["llm", "vlm"])

    openai_body = anthropic_to_openai_request(anthropic_body)
    approx = _approx_token_count(openai_body.get("messages", []))
    return JSONResponse(
        content={"input_tokens": int(approx)},
        headers=_fallback_headers(fallback_reason) or None,
    )


# ---------------------------------------------------------------------------
# Responses API pass-through (`/azure/v1/responses`)
# ---------------------------------------------------------------------------
#
# This is intentionally separate from the chat completions and messages
# paths above. Those translate from OpenAI / Anthropic shapes into the
# Responses API shape; here the client already speaks Responses API and
# we only rewrite `body.model` from the gateway alias to the Azure
# deployment name. Useful when the client wants Responses-specific
# features (previous_response_id, store, reasoning items in input, etc.)
# that don't survive the chat completions translation.
#
# No relationship to vllm_proxy.vllm_forward_responses — that one is for
# vLLM's internal LAN downstream and carries vLLM-specific concerns
# (real_model alias swap, fallback headers, alive checks). The Azure path
# needs none of those, so the code is duplicated rather than abstracted
# into a generic helper.


async def azure_forward_responses(
    request: Request,
    user: User,
) -> StreamingResponse | JSONResponse:
    """Pure pass-through to Azure ``/openai/v1/responses``.

    Mutates only ``body.model`` (alias -> deployment). The client is
    responsible for sending Responses-shape input/instructions/etc.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    requested_alias = body.get("model", "")
    resolved_alias, entry, fallback_reason = _resolve_azure(
        requested_alias, allowed_types=["llm", "vlm"],
    )
    model_type = entry.get("type", "llm")
    deployment = entry["deployment"]

    # Phase 2: capture the Responses-shape request input as Langfuse input.
    if capture_io_enabled():
        set_io_input(body.get("input") or body.get("messages") or body)

    # Only mutation: alias → deployment name. Everything else is up to the
    # client. Sampling-param stripping that the chat completions path does
    # is deliberately NOT applied here: a client speaking Responses API
    # natively presumably knows which params its target model accepts.
    body["model"] = deployment

    is_stream = bool(body.get("stream", False))
    target_url = _build_responses_url(entry)
    headers = _build_headers(entry)
    client = get_azure_client()

    monitor_body = {**body, "model": resolved_alias}
    extra_headers = _fallback_headers(fallback_reason)

    if is_stream:
        return await _stream_responses(
            client, target_url, body, headers,
            user, resolved_alias, model_type, monitor_body, entry,
            extra_headers,
        )
    return await _non_stream_responses(
        client, target_url, body, headers,
        user, resolved_alias, model_type, monitor_body, entry,
        extra_headers,
    )


async def _non_stream_responses(
    client, url: str, body: dict, headers: dict, user: User,
    alias: str, model_type: str, monitor_body: dict, route: dict,
    extra_headers: dict[str, str] | None = None,
) -> JSONResponse:
    try:
        resp = await client.post(url, json=body, headers=headers, timeout=_NON_STREAM_TIMEOUT)
    except Exception as exc:
        logger.error("Azure responses downstream error: {}", exc)
        _log_error(user, monitor_body, str(exc), 502, alias,
                          "/azure/v1/responses", model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        _log_azure_error(monitor_body, body, resp.text, resp.status_code,
                         alias, "/azure/v1/responses")
        _log_error(user, monitor_body, resp.text[:500], resp.status_code,
                          alias, "/azure/v1/responses", model_type)
        return _error_response(resp)

    data = resp.json()
    usage = data.get("usage") or {}
    input_tk = usage.get("input_tokens", 0) or 0
    output_tk = usage.get("output_tokens", 0) or 0
    cached_tk = _cached_tokens_from_responses(usage)
    obs_output = data.get("output") or data if capture_io_enabled() else None
    _log_usage(user, alias, model_type, input_tk, output_tk,
               "/azure/v1/responses", route=route, cached_tokens=cached_tk, backend="azure", output_payload=obs_output)
    return JSONResponse(content=data, headers=extra_headers or None)


async def _stream_responses(
    client, url: str, body: dict, headers: dict, user: User,
    alias: str, model_type: str, monitor_body: dict, route: dict,
    extra_headers: dict[str, str] | None = None,
) -> StreamingResponse | JSONResponse:
    # Pre-flight to surface 4xx before opening the SSE channel — same
    # rationale as _stream_chat / _stream_messages.
    req = client.build_request("POST", url, json=body, headers=headers, timeout=None)
    try:
        resp = await client.send(req, stream=True)
    except Exception as exc:
        logger.error("Azure responses stream connect error: {}", exc)
        _log_error(user, monitor_body, str(exc), 502, alias,
                          "/azure/v1/responses", model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        err_bytes = await resp.aread()
        await resp.aclose()
        err_text = err_bytes.decode("utf-8", "replace")
        _log_azure_error(monitor_body, body, err_text, resp.status_code,
                         alias, "/azure/v1/responses")
        _log_error(user, monitor_body, err_text[:500], resp.status_code,
                          alias, "/azure/v1/responses", model_type)
        try:
            err_json = json.loads(err_text)
        except Exception:
            err_json = {"error": {"message": err_text[:500]}}
        return JSONResponse(status_code=resp.status_code, content=err_json)

    _capture_io = capture_io_enabled()
    logger.info("Stream start | user={} model={} endpoint=/azure/v1/responses",
                user.username, alias)

    async def _resp_coro():
        return resp

    async def event_generator():
        # Pure SSE pass-through: rebuild each event line-for-line. We
        # additionally sniff `response.completed` events to extract usage
        # for billing without altering the bytes the client sees.
        input_tk = 0
        output_tk = 0
        cached_tk = 0
        output_acc = StreamingChatOutput()  # Phase 2: streamed Responses text
        responses_output = None  # Phase 2: full Responses output[] from the terminal event
        try:
            async for kind, data in _pump_sse_lines(_resp_coro()):
                if kind == "ping":
                    # Keep proxies / curl-less clients aware the connection
                    # is alive while Azure thinks. SSE comments are spec.
                    yield ": ping\n\n"
                    continue
                if kind == "err":
                    raise data
                if kind == "done":
                    break
                line = data
                # Re-emit the raw line. aiter_lines strips the trailing
                # newline so we add it back; the blank line between events
                # also arrives as an empty string and becomes a "\n".
                yield (line or "") + "\n"
                if not line or not line.startswith("data: "):
                    continue
                payload = line[6:]
                if not payload or payload == "[DONE]":
                    continue
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if _capture_io and event.get("type") == "response.output_text.delta":
                    output_acc.add_delta({"content": event.get("delta")})
                if event.get("type") == "response.completed":
                    resp_data = event.get("response") or {}
                    if _capture_io and isinstance(resp_data.get("output"), list):
                        responses_output = resp_data["output"]
                    u = resp_data.get("usage") or {}
                    input_tk = u.get("input_tokens", input_tk) or input_tk
                    output_tk = u.get("output_tokens", output_tk) or output_tk
                    cached_tk = _cached_tokens_from_responses(u) or cached_tk
        except Exception as exc:
            logger.error("Azure responses stream error: {}", exc)
            yield (
                "data: "
                + json.dumps({"error": {"message": str(exc), "type": "api_error"}})
                + "\n\n"
            )

        if input_tk == 0 and output_tk == 0:
            logger.warning("Azure responses stream for model={} ended with 0 tokens", alias)
        obs_output = None
        if _capture_io:
            obs_output = responses_output if responses_output is not None else output_acc.as_message()
        _log_usage(user, alias, model_type, input_tk, output_tk,
                   "/azure/v1/responses", route=route, cached_tokens=cached_tk, backend="azure", output_payload=obs_output)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            **(extra_headers or {}),
        },
    )
