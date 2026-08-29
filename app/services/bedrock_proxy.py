"""
AWS Bedrock proxy — Converse API edition.

All Bedrock LLM/VLM traffic from the gateway goes to the Converse API
(``{endpoint}/model/{modelId}/converse`` and ``/converse-stream``). Clients
still speak OpenAI chat completions (on ``/aws/v1/chat/completions``) or
Anthropic Messages (on ``/aws/v1/messages``); the translation to/from
Converse happens internally via ``converse_adapter``.

Why Converse: it is the one API surface every Bedrock model family shares
(Anthropic, Nova, Llama, Mistral, DeepSeek, ...), so a mixed-family model
list keeps a single code path — the same decision the Azure proxy made by
collapsing everything onto the Responses API.

Key conventions:
  * URL pattern: ``https://bedrock-runtime.{region}.amazonaws.com/model/
    {modelId}/converse[-stream]`` — ``modelId`` is URL-quoted (it contains
    ``.`` and ``:``); a per-entry ``endpoint`` override replaces the host
    (VPC endpoints, gov regions).
  * Auth: ``Authorization: Bearer <api_key>`` using a long-term Bedrock API
    key. Isolated in ``_build_headers`` so IAM/SigV4 signing can be added
    later without touching the forwarding logic.
  * Streaming is NOT SSE — ConverseStream returns binary
    ``application/vnd.amazon.eventstream`` frames, decoded by
    ``aws_eventstream.EventStreamDecoder`` in ``_pump_bedrock_events``
    (which also provides the same ping-heartbeat / max-idle behavior as the
    SSE pump used for vLLM and Azure).
  * No health check — Bedrock is a managed service; ``_resolve_bedrock``
    fallback is reactive to "alias not configured / wrong type" only.
  * Embeddings are not supported via Converse; there is intentionally no
    ``/aws/v1/embeddings`` endpoint.
"""

from __future__ import annotations

import json
from typing import Any, Iterator
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.config import BEDROCK_FALLBACK_MAP, BEDROCK_MODELS, _check_auto_reload
from app.core.logger import logger
from app.core.server_state import get_bedrock_client
from app.models.schema import User
from app.services.anthropic_adapter import (
    AnthropicStreamTranslator,
    anthropic_request_io,
    anthropic_to_openai_request,
    openai_message_to_anthropic,
    openai_to_anthropic_response,
)
from app.services.aws_eventstream import EventStreamDecoder, EventStreamError
from app.services.converse_adapter import (
    ConverseToChatStreamTranslator,
    cached_tokens_from_converse,
    converse_to_openai_chat_response,
    openai_chat_to_converse_request,
)
from app.services.observability import (
    StreamingChatOutput,
    capture_io_enabled,
    set_io_input,
)
from app.services.reasoning_effort import apply_to_openai_body
from app.services.redact import summarize_body
from app.services.vllm_proxy import (
    _SSE_MAX_IDLE,
    _ANTHROPIC_PING_EVENT,
    _SSE_PING_INTERVAL,
    _NON_STREAM_TIMEOUT,
    _approx_token_count,
    _error_response,
    _log_error,
    _log_usage,
)

import asyncio

_BEDROCK_DEFAULT_ALLOWED_TYPES: tuple[str, ...] = ("llm", "vlm")


def _is_usable_entry(entry: dict[str, Any]) -> bool:
    return bool(entry.get("model_id")) and bool(
        entry.get("region") or entry.get("endpoint")
    )


def _resolve_bedrock(
    alias: str,
    allowed_types: list[str] | tuple[str, ...] | None = None,
) -> tuple[str, dict[str, Any], str | None]:
    """Resolve a Bedrock alias with type-aware fallback.

    Priority (mirrors azure_proxy._resolve_azure — no health check, managed
    service):
      1. Exact alias match whose type is in ``allowed_types`` → use as-is.
      2. ``BEDROCK_FALLBACK_MAP[type]`` for some type in ``allowed_types``.
      3. First entry in ``BEDROCK_MODELS`` whose type is in ``allowed_types``.

    Returns ``(resolved_alias, entry, fallback_reason)``; ``fallback_reason``
    is surfaced via the ``X-Model-Fallback`` response header.

    Raises ``HTTPException(400)`` when nothing is usable.
    """
    _check_auto_reload()
    if allowed_types is None:
        allowed_types = _BEDROCK_DEFAULT_ALLOWED_TYPES
    allowed = tuple(allowed_types)

    entry = BEDROCK_MODELS.get(alias)
    if entry and entry.get("type", "llm") in allowed:
        if not _is_usable_entry(entry):
            raise HTTPException(
                status_code=500,
                detail=f"Bedrock model '{alias}' missing model_id or region/endpoint.",
            )
        return alias, entry, None

    if entry is None:
        reason = f"Bedrock model '{alias}' not configured"
    else:
        reason = (
            f"Bedrock model '{alias}' has type '{entry.get('type')}' "
            f"but endpoint requires one of {list(allowed)}"
        )

    # Configured fallback first
    for at in allowed:
        fb_alias = BEDROCK_FALLBACK_MAP.get(at)
        if (
            fb_alias and fb_alias != alias and fb_alias in BEDROCK_MODELS
            and BEDROCK_MODELS[fb_alias].get("type", "llm") in allowed
            and _is_usable_entry(BEDROCK_MODELS[fb_alias])
        ):
            logger.warning(
                "{} — falling back to configured Bedrock fallback '{}'",
                reason, fb_alias,
            )
            return fb_alias, BEDROCK_MODELS[fb_alias], reason

    # Auto fallback: first compatible Bedrock entry
    for fb_alias, fb_entry in BEDROCK_MODELS.items():
        if (
            fb_alias != alias
            and fb_entry.get("type", "llm") in allowed
            and _is_usable_entry(fb_entry)
        ):
            logger.warning("{} — falling back to Bedrock '{}'", reason, fb_alias)
            return fb_alias, fb_entry, reason

    raise HTTPException(
        status_code=400,
        detail=f"No Bedrock model available for types {list(allowed)}.",
    )


def _fallback_headers(fallback_reason: str | None) -> dict[str, str]:
    if fallback_reason:
        return {"X-Model-Fallback": fallback_reason}
    return {}


def _build_converse_url(entry: dict[str, Any], stream: bool) -> str:
    endpoint = (entry.get("endpoint") or "").rstrip("/")
    if not endpoint:
        endpoint = f"https://bedrock-runtime.{entry.get('region', 'us-east-1')}.amazonaws.com"
    # model_id contains '.' and ':' (and '/' for ARNs) — quote everything.
    model_id = quote(entry.get("model_id", ""), safe="")
    op = "converse-stream" if stream else "converse"
    return f"{endpoint}/model/{model_id}/{op}"


def _build_headers(entry: dict[str, Any]) -> dict[str, str]:
    """Bedrock auth: long-term API key as a Bearer token.

    This is the single seam for downstream auth — an IAM/SigV4 variant would
    replace the header set here (and would need the request body for
    signing, so it would move to a sign(entry, body) shape).
    """
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {entry.get('api_key', '')}",
    }


def _log_bedrock_error(
    incoming_body: dict[str, Any],
    sent_body: dict[str, Any],
    resp_text: str,
    status: int,
    alias: str,
    endpoint: str,
) -> None:
    """Surface Bedrock 4xx/5xx with both halves of the conversation (what the
    client asked / what the gateway translated to / how Bedrock objected)."""
    # Shape only — the bodies are the user's prompt. See services/redact.py.
    logger.warning(
        "Bedrock returned {} | endpoint={} model={} resp={} | sent_shape={} | incoming_shape={}",
        status, endpoint, alias, resp_text[:1000],
        summarize_body(sent_body), summarize_body(incoming_body),
    )


async def _pump_bedrock_events(
    send_coro,
    ping_interval: float = _SSE_PING_INTERVAL,
    max_idle: float = _SSE_MAX_IDLE,
):
    """Run a ConverseStream request as a background task and yield events.

    The binary-framing sibling of ``vllm_proxy._pump_sse_lines``:
    reads raw bytes, feeds them through ``EventStreamDecoder``, and yields

      ('ping', None)                    — ping_interval s of downstream silence
      ('event', (event_type, payload))  — decoded event; payload is the parsed
                                          JSON dict, event_type is the
                                          :event-type header (or the
                                          :exception-type for exceptions)
      ('err', Exception)                — downstream raised / frame corrupt /
                                          max_idle exceeded
      ('done', None)                    — stream finished cleanly

    Caller does NOT need to close the response.
    """
    queue: asyncio.Queue = asyncio.Queue()
    resp_holder: list = []

    async def reader():
        try:
            resp = await send_coro
            resp_holder.append(resp)
            decoder = EventStreamDecoder()
            async for chunk in resp.aiter_bytes():
                for msg in decoder.feed(chunk):
                    if msg.message_type == "exception":
                        etype = msg.exception_type or "exception"
                    else:
                        etype = msg.event_type
                    try:
                        payload = json.loads(msg.payload) if msg.payload else {}
                    except json.JSONDecodeError:
                        payload = {}
                    await queue.put(("event", (etype, payload)))
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
            idle = 0.0
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
# Chat Completions (`/aws/v1/chat/completions`)
# ---------------------------------------------------------------------------

async def bedrock_forward_chat_completions(
    request: Request,
    user: User,
) -> StreamingResponse | JSONResponse:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    requested_alias = body.get("model", "")
    resolved_alias, entry, fallback_reason = _resolve_bedrock(
        requested_alias, allowed_types=["llm", "vlm"],
    )
    model_type = entry.get("type", "llm")

    is_stream = bool(body.get("stream", False))
    # Reconcile the requested effort with what this model accepts — a no-op
    # unless the entry declares `reasoning_efforts`.
    apply_to_openai_body(body, entry, resolved_alias, "/aws/v1/chat/completions")
    converse_body = openai_chat_to_converse_request(body, model_id=entry.get("model_id", ""))

    if capture_io_enabled():
        set_io_input(body.get("messages"))

    target_url = _build_converse_url(entry, stream=is_stream)
    headers = _build_headers(entry)
    client = get_bedrock_client()

    monitor_body = {**body, "model": resolved_alias}
    extra_headers = _fallback_headers(fallback_reason)

    if is_stream:
        return await _stream_chat(
            client, target_url, converse_body, headers,
            user, resolved_alias, model_type, monitor_body, entry, body,
            extra_headers,
        )
    return await _non_stream_chat(
        client, target_url, converse_body, headers,
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
        logger.error("Bedrock downstream error: {}", exc)
        _log_error(user, monitor_body, str(exc), 502, alias,
                          "/aws/v1/chat/completions", model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        _log_bedrock_error(incoming_body, body, resp.text, resp.status_code,
                           alias, "/aws/v1/chat/completions")
        _log_error(user, monitor_body, resp.text[:500], resp.status_code,
                          alias, "/aws/v1/chat/completions", model_type)
        return _error_response(resp)

    raw = resp.json()
    chat_data = converse_to_openai_chat_response(raw, alias)
    usage = chat_data.get("usage", {})
    input_tk = usage.get("prompt_tokens", 0)
    output_tk = usage.get("completion_tokens", 0)
    cached_tk = cached_tokens_from_converse(raw.get("usage") or {})
    obs_output = (chat_data.get("choices") or [{}])[0].get("message") if capture_io_enabled() else None
    _log_usage(user, alias, model_type, input_tk, output_tk,
               "/aws/v1/chat/completions", route=route, cached_tokens=cached_tk,
               backend="bedrock", output_payload=obs_output)
    return JSONResponse(content=chat_data, headers=extra_headers or None)


async def _stream_chat(
    client, url: str, body: dict, headers: dict, user: User,
    alias: str, model_type: str, monitor_body: dict, route: dict,
    incoming_body: dict, extra_headers: dict[str, str] | None = None,
) -> StreamingResponse | JSONResponse:
    # Pre-flight: open the stream and check status BEFORE handing it to the
    # event pump — a Bedrock 4xx arrives as a plain JSON body, not an
    # event-stream frame, and would otherwise be dropped silently.
    req = client.build_request("POST", url, json=body, headers=headers, timeout=None)
    try:
        resp = await client.send(req, stream=True)
    except Exception as exc:
        logger.error("Bedrock stream connect error: {}", exc)
        _log_error(user, monitor_body, str(exc), 502, alias,
                          "/aws/v1/chat/completions", model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        err_bytes = await resp.aread()
        await resp.aclose()
        err_text = err_bytes.decode("utf-8", "replace")
        _log_bedrock_error(incoming_body, body, err_text, resp.status_code,
                           alias, "/aws/v1/chat/completions")
        _log_error(user, monitor_body, err_text[:500], resp.status_code,
                          alias, "/aws/v1/chat/completions", model_type)
        try:
            err_json = json.loads(err_text)
        except Exception:
            err_json = {"error": {"message": err_text[:500]}}
        return JSONResponse(status_code=resp.status_code, content=err_json)

    _capture_io = capture_io_enabled()
    logger.info("Stream start | user={} model={} endpoint=/aws/v1/chat/completions",
                user.username, alias)

    async def _resp_coro():
        return resp

    async def event_generator():
        translator = ConverseToChatStreamTranslator(alias)
        output_acc = StreamingChatOutput()
        try:
            for chunk in translator.start():
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

            async for kind, data in _pump_bedrock_events(_resp_coro()):
                if kind == "ping":
                    yield ": ping\n\n"
                    continue
                if kind == "err":
                    raise data
                if kind == "done":
                    break
                etype, payload = data
                for chunk in translator.handle_event(etype, payload):
                    if _capture_io:
                        output_acc.add_delta((chunk.get("choices") or [{}])[0].get("delta") or {})
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

            for chunk in translator.finish():
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            br_err = translator.derive_error_message()
            if br_err:
                yield (
                    "data: "
                    + json.dumps({"error": {"message": f"Bedrock: {br_err}", "type": "api_error"}})
                    + "\n\n"
                )
            yield "data: [DONE]\n\n"
        except EventStreamError as exc:
            logger.error("Bedrock chat stream frame error: {}", exc)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        except Exception as exc:
            logger.error("Bedrock chat stream error: {}", exc)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

        input_tk = translator.input_tokens
        output_tk = translator.output_tokens
        cached_tk = translator.cached_tokens
        if input_tk == 0 and output_tk == 0:
            logger.warning("Bedrock chat stream for model={} ended with 0 tokens", alias)
        if translator.emitted_text_chars == 0 and not translator._tool_meta_sent:  # noqa: SLF001
            logger.warning(
                "Empty assistant content from Bedrock stream | endpoint=/aws/v1/chat/completions "
                "model={} input_tokens={} output_tokens={} reasoning_chars={} finish={} "
                "event_types={} error={}",
                alias, input_tk, output_tk,
                translator.emitted_reasoning_chars, translator.finish_reason,
                translator.event_type_counts, translator.derive_error_message(),
            )
        obs_output = output_acc.as_message() if _capture_io else None
        _log_usage(user, alias, model_type, input_tk, output_tk,
                   "/aws/v1/chat/completions", route=route, cached_tokens=cached_tk,
                   backend="bedrock", output_payload=obs_output)

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
# Anthropic Messages API (`/aws/v1/messages`)
# ---------------------------------------------------------------------------

async def bedrock_forward_messages(
    request: Request,
    user: User,
) -> StreamingResponse | JSONResponse:
    try:
        anthropic_body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    requested_alias = anthropic_body.get("model", "")
    resolved_alias, entry, fallback_reason = _resolve_bedrock(
        requested_alias, allowed_types=["llm", "vlm"],
    )
    model_type = entry.get("type", "llm")

    openai_body = anthropic_to_openai_request(
        anthropic_body, is_reasoning=bool(entry.get("is_reasoning")),
    )
    apply_to_openai_body(openai_body, entry, resolved_alias, "/aws/v1/messages")
    is_stream = bool(anthropic_body.get("stream", False))
    converse_body = openai_chat_to_converse_request(
        openai_body, model_id=entry.get("model_id", ""),
    )

    # Phase 2: capture the ORIGINAL Anthropic request as Langfuse input so
    # the trace stays in Anthropic shape (not the internal OpenAI pivot).
    if capture_io_enabled():
        set_io_input(anthropic_request_io(anthropic_body))

    target_url = _build_converse_url(entry, stream=is_stream)
    headers = _build_headers(entry)
    client = get_bedrock_client()

    monitor_body = dict(anthropic_body)
    monitor_body["model"] = resolved_alias
    extra_headers = _fallback_headers(fallback_reason)

    if is_stream:
        return await _stream_messages(
            client, target_url, converse_body, headers,
            user, resolved_alias, model_type, monitor_body, entry, anthropic_body,
            extra_headers,
        )
    return await _non_stream_messages(
        client, target_url, converse_body, headers,
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
        logger.error("Bedrock messages downstream error: {}", exc)
        _log_error(user, monitor_body, str(exc), 502, alias,
                          "/aws/v1/messages", model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        _log_bedrock_error(incoming_body, body, resp.text, resp.status_code,
                           alias, "/aws/v1/messages")
        _log_error(user, monitor_body, resp.text[:500], resp.status_code,
                          alias, "/aws/v1/messages", model_type)
        return _error_response(resp)

    raw = resp.json()
    chat_data = converse_to_openai_chat_response(raw, alias)
    anthropic_data = openai_to_anthropic_response(chat_data, alias)
    input_tk = anthropic_data["usage"]["input_tokens"]
    output_tk = anthropic_data["usage"]["output_tokens"]
    cached_tk = cached_tokens_from_converse(raw.get("usage") or {})
    obs_output = (
        {"role": "assistant", "content": anthropic_data["content"]}
        if capture_io_enabled() else None
    )
    _log_usage(user, alias, model_type, input_tk, output_tk,
               "/aws/v1/messages", route=route, cached_tokens=cached_tk,
               backend="bedrock", output_payload=obs_output)
    return JSONResponse(content=anthropic_data, headers=extra_headers or None)


async def _stream_messages(
    client, url: str, body: dict, headers: dict, user: User,
    alias: str, model_type: str, monitor_body: dict, route: dict,
    incoming_body: dict, extra_headers: dict[str, str] | None = None,
) -> StreamingResponse | JSONResponse:
    # Pre-flight to surface 4xx before opening the stream — same rationale
    # as _stream_chat.
    req = client.build_request("POST", url, json=body, headers=headers, timeout=None)
    try:
        resp = await client.send(req, stream=True)
    except Exception as exc:
        logger.error("Bedrock messages stream connect error: {}", exc)
        _log_error(user, monitor_body, str(exc), 502, alias,
                          "/aws/v1/messages", model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        err_bytes = await resp.aread()
        await resp.aclose()
        err_text = err_bytes.decode("utf-8", "replace")
        _log_bedrock_error(incoming_body, body, err_text, resp.status_code,
                           alias, "/aws/v1/messages")
        _log_error(user, monitor_body, err_text[:500], resp.status_code,
                          alias, "/aws/v1/messages", model_type)
        try:
            err_json = json.loads(err_text)
        except Exception:
            err_json = {"type": "error", "error": {"type": "api_error", "message": err_text[:500]}}
        return JSONResponse(status_code=resp.status_code, content=err_json)

    _capture_io = capture_io_enabled()
    logger.info("Stream start | user={} model={} endpoint=/aws/v1/messages",
                user.username, alias)

    async def _resp_coro():
        return resp

    async def event_generator():
        # Two-stage translator chain: ConverseStream events -> synthetic
        # OpenAI chat chunks -> Anthropic SSE events. Reuses the existing
        # Anthropic translator so the public Anthropic surface stays
        # identical across vLLM / Azure / Bedrock.
        converse_xlat = ConverseToChatStreamTranslator(alias)
        anthropic_xlat = AnthropicStreamTranslator(alias)
        output_acc = StreamingChatOutput()

        def _feed(chat_chunks) -> Iterator:
            for ch in chat_chunks:
                if _capture_io:
                    output_acc.add_delta((ch.get("choices") or [{}])[0].get("delta") or {})
                yield from anthropic_xlat.handle_chunk(ch)

        try:
            for event in anthropic_xlat.start():
                yield event
            for event in _feed(converse_xlat.start()):
                yield event

            async for kind, data in _pump_bedrock_events(_resp_coro()):
                if kind == "ping":
                    yield _ANTHROPIC_PING_EVENT
                    continue
                if kind == "err":
                    raise data
                if kind == "done":
                    break
                etype, payload = data
                chat_chunks = list(converse_xlat.handle_event(etype, payload))
                if chat_chunks:
                    for event in _feed(chat_chunks):
                        yield event

            # Final chunk carries usage + finish_reason for the Anthropic
            # translator to compute message_delta + message_stop.
            for event in _feed(converse_xlat.finish()):
                yield event

            br_err = converse_xlat.derive_error_message()
            if br_err:
                error_kind = converse_xlat.derive_error_kind()
                logger.warning(
                    "Bedrock messages stream surfaced error | model={} error={} kind={}",
                    alias, br_err, error_kind,
                )
                for event in anthropic_xlat.fail(f"Bedrock: {br_err}", error_type=error_kind):
                    yield event
            elif anthropic_xlat.stop_reason is None:
                logger.warning(
                    "Bedrock messages stream ended without stopReason | model={} — truncated",
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
            logger.error("Bedrock messages stream error: {}", exc)
            err_payload = json.dumps({"type": "error", "error": {"type": "api_error", "message": str(exc)}})
            yield f"event: error\ndata: {err_payload}\n\n"

        input_tk = anthropic_xlat.input_tokens or converse_xlat.input_tokens
        output_tk = anthropic_xlat.output_tokens or converse_xlat.output_tokens
        cached_tk = converse_xlat.cached_tokens
        if converse_xlat.emitted_text_chars == 0 and not converse_xlat._tool_meta_sent:  # noqa: SLF001
            logger.warning(
                "Empty assistant content from Bedrock stream | endpoint=/aws/v1/messages "
                "model={} input_tokens={} output_tokens={} reasoning_chars={} finish={} "
                "event_types={} error={}",
                alias, input_tk, output_tk,
                converse_xlat.emitted_reasoning_chars, converse_xlat.finish_reason,
                converse_xlat.event_type_counts, converse_xlat.derive_error_message(),
            )
        obs_output = (
            openai_message_to_anthropic(output_acc.as_message(), alias)
            if _capture_io else None
        )
        _log_usage(user, alias, model_type, input_tk, output_tk,
                   "/aws/v1/messages", route=route, cached_tokens=cached_tk,
                   backend="bedrock", output_payload=obs_output)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            **(extra_headers or {}),
        },
    )


async def bedrock_forward_count_tokens(
    request: Request,
    user: User,
) -> JSONResponse:
    """Anthropic /v1/messages/count_tokens for Bedrock models.

    Bedrock has no tokenize endpoint, so this returns a chars/4 estimate
    (same convention as the Azure path). Auth/daily-limit checks still
    apply; not billed.
    """
    try:
        anthropic_body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    alias = anthropic_body.get("model", "")
    _, _entry, fallback_reason = _resolve_bedrock(alias, allowed_types=["llm", "vlm"])

    openai_body = anthropic_to_openai_request(anthropic_body)
    approx = _approx_token_count(openai_body.get("messages", []))
    return JSONResponse(
        content={"input_tokens": int(approx)},
        headers=_fallback_headers(fallback_reason) or None,
    )
