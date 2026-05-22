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

from app.core.config import AZURE_MODELS, _check_auto_reload
from app.core.logger import logger
from app.core.server_state import get_azure_client
from app.models.schema import User
from app.services.anthropic_adapter import (
    AnthropicStreamTranslator,
    anthropic_to_openai_request,
    openai_to_anthropic_response,
)
from app.services.monitor import is_monitored, log_monitor, log_monitor_error
from app.services.responses_adapter import (
    ResponsesToChatStreamTranslator,
    openai_chat_to_responses_request,
    responses_to_openai_chat_response,
)
from app.services.vllm_proxy import (
    _ANTHROPIC_PING_EVENT,
    _NON_STREAM_TIMEOUT,
    _approx_token_count,
    _calc_cost,
    _error_response,
    _log_usage,
    _pump_anthropic_lines,
)


def _resolve_azure(alias: str) -> dict[str, Any]:
    _check_auto_reload()
    entry = AZURE_MODELS.get(alias)
    if not entry:
        raise HTTPException(
            status_code=400,
            detail=f"Azure model '{alias}' not configured.",
        )
    if not entry.get("endpoint") or not entry.get("deployment"):
        raise HTTPException(
            status_code=500,
            detail=f"Azure model '{alias}' missing endpoint or deployment.",
        )
    return entry


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
    try:
        sent_snippet = json.dumps(sent_body, ensure_ascii=False)[:800]
        in_snippet = json.dumps(incoming_body, ensure_ascii=False)[:400]
    except Exception:
        sent_snippet = repr(sent_body)[:800]
        in_snippet = repr(incoming_body)[:400]
    logger.warning(
        "Azure returned {} | endpoint={} model={} resp={} | sent_body={} | incoming_body={}",
        status, endpoint, alias, resp_text[:500], sent_snippet, in_snippet,
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

async def forward_chat_completions(
    request: Request,
    user: User,
) -> StreamingResponse | JSONResponse:
    body = await request.json()
    alias = body.get("model", "")
    entry = _resolve_azure(alias)
    model_type = entry.get("type", "llm")
    deployment = entry["deployment"]

    is_stream = bool(body.get("stream", False))
    responses_body = openai_chat_to_responses_request(body, model=deployment)
    if is_stream:
        responses_body["stream"] = True

    if not _has_input(responses_body):
        try:
            in_snippet = json.dumps(body, ensure_ascii=False)[:600]
        except Exception:
            in_snippet = repr(body)[:600]
        logger.warning(
            "Refusing to forward empty Responses request | endpoint=/azure/v1/chat/completions "
            "model={} incoming_body={}",
            alias, in_snippet,
        )
        raise HTTPException(
            status_code=400,
            detail=(
                "Translated request to Azure has no `input`. "
                "Provide at least one non-system message."
            ),
        )

    target_url = _build_responses_url(entry)
    headers = _build_headers(entry)
    client = get_azure_client()

    monitor_body = {**body, "model": alias}

    if is_stream:
        return await _stream_chat(
            client, target_url, responses_body, headers,
            user, alias, model_type, monitor_body, entry, body,
        )
    return await _non_stream_chat(
        client, target_url, responses_body, headers,
        user, alias, model_type, monitor_body, entry, body,
    )


async def _non_stream_chat(
    client, url: str, body: dict, headers: dict, user: User,
    alias: str, model_type: str, monitor_body: dict, route: dict,
    incoming_body: dict,
) -> JSONResponse:
    try:
        resp = await client.post(url, json=body, headers=headers, timeout=_NON_STREAM_TIMEOUT)
    except Exception as exc:
        logger.error("Azure downstream error: {}", exc)
        log_monitor_error(user.id, monitor_body, str(exc), 502, alias,
                          "/azure/v1/chat/completions", model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        _log_azure_error(incoming_body, body, resp.text, resp.status_code,
                         alias, "/azure/v1/chat/completions")
        log_monitor_error(user.id, monitor_body, resp.text[:500], resp.status_code,
                          alias, "/azure/v1/chat/completions", model_type)
        return _error_response(resp)

    raw = resp.json()
    chat_data = responses_to_openai_chat_response(raw, alias)
    _warn_if_empty_translation(raw, chat_data, alias, "/azure/v1/chat/completions")
    usage = chat_data.get("usage", {})
    input_tk = usage.get("prompt_tokens", 0)
    output_tk = usage.get("completion_tokens", 0)
    cached_tk = _cached_tokens_from_responses(raw.get("usage") or {})
    _log_usage(user, alias, model_type, input_tk, output_tk,
               "/azure/v1/chat/completions", route=route, cached_tokens=cached_tk)
    if is_monitored(user.id):
        cost = float(_calc_cost(route, model_type, input_tk, output_tk, cached_tk))
        log_monitor(user.id, monitor_body, chat_data, alias,
                    "/azure/v1/chat/completions", input_tk, output_tk, cost, model_type)
    return JSONResponse(content=chat_data)


async def _stream_chat(
    client, url: str, body: dict, headers: dict, user: User,
    alias: str, model_type: str, monitor_body: dict, route: dict,
    incoming_body: dict,
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
        log_monitor_error(user.id, monitor_body, str(exc), 502, alias,
                          "/azure/v1/chat/completions", model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        err_bytes = await resp.aread()
        await resp.aclose()
        err_text = err_bytes.decode("utf-8", "replace")
        _log_azure_error(incoming_body, body, err_text, resp.status_code,
                         alias, "/azure/v1/chat/completions")
        log_monitor_error(user.id, monitor_body, err_text[:500], resp.status_code,
                          alias, "/azure/v1/chat/completions", model_type)
        try:
            err_json = json.loads(err_text)
        except Exception:
            err_json = {"error": {"message": err_text[:500]}}
        return JSONResponse(status_code=resp.status_code, content=err_json)

    _monitoring = is_monitored(user.id)
    logger.info("Stream start | user={} model={} endpoint=/azure/v1/chat/completions",
                user.username, alias)

    async def _resp_coro():
        # _pump_anthropic_lines awaits its argument and calls aclose() in
        # its finally block, so wrap the already-opened response in a coro
        # to reuse the pump's lifecycle/ping handling without re-sending.
        return resp

    async def event_generator():
        translator = ResponsesToChatStreamTranslator(alias)
        chunks: list[dict] = []
        try:
            for chunk in translator.start():
                if _monitoring:
                    chunks.append(chunk)
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

            async for kind, data in _pump_anthropic_lines(_resp_coro()):
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
                    if _monitoring:
                        chunks.append(chunk)
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

            for chunk in translator.finish():
                if _monitoring:
                    chunks.append(chunk)
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:
            logger.error("Azure chat stream error: {}", exc)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

        input_tk = translator.input_tokens
        output_tk = translator.output_tokens
        cached_tk = translator.cached_tokens
        if input_tk == 0 and output_tk == 0:
            logger.warning("Azure chat stream for model={} ended with 0 tokens", alias)
        if (
            translator.emitted_text_chars == 0
            and not translator._tool_meta_sent  # noqa: SLF001
        ):
            logger.warning(
                "Empty assistant content from Azure stream | endpoint=/azure/v1/chat/completions "
                "model={} input_tokens={} output_tokens={} reasoning_chars={} finish={}",
                alias, input_tk, output_tk,
                translator.emitted_reasoning_chars, translator.finish_reason,
            )
        _log_usage(user, alias, model_type, input_tk, output_tk,
                   "/azure/v1/chat/completions", route=route, cached_tokens=cached_tk)
        if _monitoring:
            cost = float(_calc_cost(route, model_type, input_tk, output_tk, cached_tk))
            log_monitor(user.id, monitor_body, chunks, alias,
                        "/azure/v1/chat/completions", input_tk, output_tk, cost, model_type)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# ---------------------------------------------------------------------------
# Anthropic Messages API (`/azure/v1/messages`)
# ---------------------------------------------------------------------------

async def forward_messages(
    request: Request,
    user: User,
) -> StreamingResponse | JSONResponse:
    try:
        anthropic_body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    alias = anthropic_body.get("model", "")
    entry = _resolve_azure(alias)
    model_type = entry.get("type", "llm")
    deployment = entry["deployment"]

    openai_body = anthropic_to_openai_request(
        anthropic_body, is_reasoning=bool(entry.get("is_reasoning")),
    )
    is_stream = bool(anthropic_body.get("stream", False))
    responses_body = openai_chat_to_responses_request(openai_body, model=deployment)
    if is_stream:
        responses_body["stream"] = True

    if not _has_input(responses_body):
        try:
            in_snippet = json.dumps(anthropic_body, ensure_ascii=False)[:600]
        except Exception:
            in_snippet = repr(anthropic_body)[:600]
        logger.warning(
            "Refusing to forward empty Responses request | endpoint=/azure/v1/messages "
            "model={} incoming_body={}",
            alias, in_snippet,
        )
        raise HTTPException(
            status_code=400,
            detail=(
                "Translated request to Azure has no `input`. "
                "Provide at least one non-system message."
            ),
        )

    target_url = _build_responses_url(entry)
    headers = _build_headers(entry)
    client = get_azure_client()

    monitor_body = dict(anthropic_body)
    monitor_body["model"] = alias

    if is_stream:
        return await _stream_messages(
            client, target_url, responses_body, headers,
            user, alias, model_type, monitor_body, entry, anthropic_body,
        )
    return await _non_stream_messages(
        client, target_url, responses_body, headers,
        user, alias, model_type, monitor_body, entry, anthropic_body,
    )


async def _non_stream_messages(
    client, url: str, body: dict, headers: dict, user: User,
    alias: str, model_type: str, monitor_body: dict, route: dict,
    incoming_body: dict,
) -> JSONResponse:
    try:
        resp = await client.post(url, json=body, headers=headers, timeout=_NON_STREAM_TIMEOUT)
    except Exception as exc:
        logger.error("Azure messages downstream error: {}", exc)
        log_monitor_error(user.id, monitor_body, str(exc), 502, alias,
                          "/azure/v1/messages", model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        _log_azure_error(incoming_body, body, resp.text, resp.status_code,
                         alias, "/azure/v1/messages")
        log_monitor_error(user.id, monitor_body, resp.text[:500], resp.status_code,
                          alias, "/azure/v1/messages", model_type)
        return _error_response(resp)

    raw = resp.json()
    chat_data = responses_to_openai_chat_response(raw, alias)
    _warn_if_empty_translation(raw, chat_data, alias, "/azure/v1/messages")
    anthropic_data = openai_to_anthropic_response(chat_data, alias)
    input_tk = anthropic_data["usage"]["input_tokens"]
    output_tk = anthropic_data["usage"]["output_tokens"]
    cached_tk = _cached_tokens_from_responses(raw.get("usage") or {})
    _log_usage(user, alias, model_type, input_tk, output_tk,
               "/azure/v1/messages", route=route, cached_tokens=cached_tk)
    if is_monitored(user.id):
        cost = float(_calc_cost(route, model_type, input_tk, output_tk, cached_tk))
        log_monitor(user.id, monitor_body, anthropic_data, alias,
                    "/azure/v1/messages", input_tk, output_tk, cost, model_type)
    return JSONResponse(content=anthropic_data)


async def _stream_messages(
    client, url: str, body: dict, headers: dict, user: User,
    alias: str, model_type: str, monitor_body: dict, route: dict,
    incoming_body: dict,
) -> StreamingResponse | JSONResponse:
    # Pre-flight to surface 4xx before opening the SSE channel — same
    # rationale as _stream_chat.
    req = client.build_request("POST", url, json=body, headers=headers, timeout=None)
    try:
        resp = await client.send(req, stream=True)
    except Exception as exc:
        logger.error("Azure messages stream connect error: {}", exc)
        log_monitor_error(user.id, monitor_body, str(exc), 502, alias,
                          "/azure/v1/messages", model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        err_bytes = await resp.aread()
        await resp.aclose()
        err_text = err_bytes.decode("utf-8", "replace")
        _log_azure_error(incoming_body, body, err_text, resp.status_code,
                         alias, "/azure/v1/messages")
        log_monitor_error(user.id, monitor_body, err_text[:500], resp.status_code,
                          alias, "/azure/v1/messages", model_type)
        try:
            err_json = json.loads(err_text)
        except Exception:
            err_json = {"type": "error", "error": {"type": "api_error", "message": err_text[:500]}}
        return JSONResponse(status_code=resp.status_code, content=err_json)

    _monitoring = is_monitored(user.id)
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
        chunks: list[dict] = []

        def _feed(chat_chunks) -> Iterator:
            for ch in chat_chunks:
                if _monitoring:
                    chunks.append(ch)
                yield from anthropic_xlat.handle_chunk(ch)

        try:
            for event in anthropic_xlat.start():
                yield event
            # Drain the start() chunk(s) through the Anthropic translator so
            # the initial role delta primes its state machine.
            for event in _feed(responses_xlat.start()):
                yield event

            async for kind, data in _pump_anthropic_lines(_resp_coro()):
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

            if anthropic_xlat.stop_reason is None:
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
        _log_usage(user, alias, model_type, input_tk, output_tk,
                   "/azure/v1/messages", route=route, cached_tokens=cached_tk)
        if _monitoring:
            cost = float(_calc_cost(route, model_type, input_tk, output_tk, cached_tk))
            log_monitor(user.id, monitor_body, chunks, alias,
                        "/azure/v1/messages", input_tk, output_tk, cost, model_type)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


async def forward_count_tokens(
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
    _resolve_azure(alias)

    openai_body = anthropic_to_openai_request(anthropic_body)
    approx = _approx_token_count(openai_body.get("messages", []))
    return JSONResponse(content={"input_tokens": int(approx)})
