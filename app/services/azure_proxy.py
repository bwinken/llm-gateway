"""
Azure OpenAI proxy.

Translates OpenAI-style requests on `/azure/v1/*` to Azure OpenAI's URL
and header conventions, then streams the (already OpenAI-shaped) response
back to the client unchanged.

Key differences from vLLM/OpenAI handled here:
  * URL pattern: {endpoint}/openai/deployments/{deployment}/{path}?api-version=...
  * Auth header: `api-key: <key>` instead of `Authorization: Bearer <key>`
  * The `model` field in the request body is replaced by the deployment in
    the URL — Azure ignores body.model when the URL targets a deployment.
"""

from __future__ import annotations

import json
from typing import Any

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
    """Return the azure model entry for `alias`, or raise 400."""
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


def _build_url(entry: dict[str, Any], path: str) -> str:
    """Build the Azure URL for a given API path (e.g. 'chat/completions')."""
    endpoint = entry["endpoint"].rstrip("/")
    deployment = entry["deployment"]
    api_version = entry.get("api_version") or "2024-08-01-preview"
    return f"{endpoint}/openai/deployments/{deployment}/{path}?api-version={api_version}"


def _build_headers(entry: dict[str, Any]) -> dict[str, str]:
    """Azure auth uses `api-key` header (not Bearer)."""
    return {
        "Content-Type": "application/json",
        "api-key": entry.get("api_key", ""),
    }


def _cached_tokens(usage: dict) -> int:
    """Extract prompt-cache hit count from an OpenAI-shape usage object.

    Azure (and OpenAI) report cache hits under
    `usage.prompt_tokens_details.cached_tokens`; cached_tokens is a subset
    of prompt_tokens. Returns 0 when caching wasn't used or isn't reported.
    """
    details = usage.get("prompt_tokens_details") or {}
    return details.get("cached_tokens", 0) or 0


# ---------------------------------------------------------------------------
# Chat Completions
# ---------------------------------------------------------------------------

async def forward_chat_completions(
    request: Request,
    user: User,
) -> StreamingResponse | JSONResponse:
    body = await request.json()
    alias = body.get("model", "")
    entry = _resolve_azure(alias)
    model_type = entry.get("type", "llm")

    # Azure routes by deployment in the URL; the body.model field is ignored
    # but we strip it to keep the payload tidy.
    body.pop("model", None)

    is_stream = body.get("stream", False)
    if is_stream:
        opts = body.get("stream_options") or {}
        opts["include_usage"] = True
        body["stream_options"] = opts

    target_url = _build_url(entry, "chat/completions")
    headers = _build_headers(entry)
    client = get_azure_client()

    monitor_body = {**body, "model": alias}

    if is_stream:
        return await _stream_chat(
            client, target_url, body, headers, user, alias, model_type, monitor_body, entry,
        )
    return await _non_stream_chat(
        client, target_url, body, headers, user, alias, model_type, monitor_body, entry,
    )


async def _non_stream_chat(
    client, url: str, body: dict, headers: dict, user: User,
    model: str, model_type: str, monitor_body: dict, route: dict,
) -> JSONResponse:
    try:
        resp = await client.post(url, json=body, headers=headers, timeout=_NON_STREAM_TIMEOUT)
    except Exception as exc:
        logger.error("Azure downstream error: {}", exc)
        log_monitor_error(user.id, monitor_body, str(exc), 502, model, "/azure/v1/chat/completions", model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        log_monitor_error(user.id, monitor_body, resp.text[:500], resp.status_code, model, "/azure/v1/chat/completions", model_type)
        return _error_response(resp)

    data = resp.json()
    usage = data.get("usage", {})
    input_tk = usage.get("prompt_tokens", 0)
    output_tk = usage.get("completion_tokens", 0)
    cached_tk = _cached_tokens(usage)
    _log_usage(user, model, model_type, input_tk, output_tk, "/azure/v1/chat/completions", route=route, cached_tokens=cached_tk)
    if is_monitored(user.id):
        cost = float(_calc_cost(route, model_type, input_tk, output_tk, cached_tk))
        log_monitor(user.id, monitor_body, data, model, "/azure/v1/chat/completions", input_tk, output_tk, cost, model_type)
    return JSONResponse(content=data)


async def _stream_chat(
    client, url: str, body: dict, headers: dict, user: User,
    model: str, model_type: str, monitor_body: dict, route: dict,
) -> StreamingResponse:
    req = client.build_request("POST", url, json=body, headers=headers, timeout=None)
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
                        usage = chunk.get("usage")
                        if usage:
                            input_tokens = usage.get("prompt_tokens", input_tokens)
                            output_tokens = usage.get("completion_tokens", output_tokens)
                            cached_tokens = _cached_tokens(usage) or cached_tokens
                    except (json.JSONDecodeError, KeyError):
                        pass
        except Exception as exc:
            logger.error("Azure stream error: {}", exc)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        finally:
            if resp is not None:
                try:
                    await resp.aclose()
                except Exception:
                    pass

        if input_tokens == 0 and output_tokens == 0:
            logger.warning("Azure stream for model={} ended with 0 tokens", model)
        _log_usage(user, model, model_type, input_tokens, output_tokens, "/azure/v1/chat/completions", route=route, cached_tokens=cached_tokens)
        if _monitoring:
            cost = float(_calc_cost(route, model_type, input_tokens, output_tokens, cached_tokens))
            log_monitor(user.id, monitor_body, chunks, model, "/azure/v1/chat/completions", input_tokens, output_tokens, cost, model_type)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

async def forward_embeddings(
    request: Request,
    user: User,
) -> JSONResponse:
    body = await request.json()
    alias = body.get("model", "")
    entry = _resolve_azure(alias)
    model_type = entry.get("type", "embedding")
    body.pop("model", None)

    target_url = _build_url(entry, "embeddings")
    headers = _build_headers(entry)
    client = get_azure_client()

    monitor_body = {**body, "model": alias}

    try:
        resp = await client.post(target_url, json=body, headers=headers, timeout=_NON_STREAM_TIMEOUT)
    except Exception as exc:
        logger.error("Azure downstream error: {}", exc)
        log_monitor_error(user.id, monitor_body, str(exc), 502, alias, "/azure/v1/embeddings", model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        log_monitor_error(user.id, monitor_body, resp.text[:500], resp.status_code, alias, "/azure/v1/embeddings", model_type)
        return _error_response(resp)

    data = resp.json()
    usage = data.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    total_tokens = usage.get("total_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    if prompt_tokens == 0 and total_tokens > 0:
        prompt_tokens = total_tokens

    _log_usage(user, alias, model_type, prompt_tokens, completion_tokens, "/azure/v1/embeddings", route=entry)
    if is_monitored(user.id):
        cost = float(_calc_cost(entry, model_type, prompt_tokens, completion_tokens))
        log_monitor(user.id, monitor_body, data, alias, "/azure/v1/embeddings", prompt_tokens, completion_tokens, cost, model_type)
    return JSONResponse(content=data)


# ---------------------------------------------------------------------------
# Anthropic Messages API (translates Anthropic format ↔ OpenAI)
# ---------------------------------------------------------------------------

async def forward_messages(
    request: Request,
    user: User,
) -> StreamingResponse | JSONResponse:
    """Anthropic /v1/messages → translate → Azure chat/completions → translate back."""
    try:
        anthropic_body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    alias = anthropic_body.get("model", "")
    entry = _resolve_azure(alias)
    model_type = entry.get("type", "llm")

    openai_body = anthropic_to_openai_request(
        anthropic_body, is_reasoning=bool(entry.get("is_reasoning")),
    )
    # Azure ignores body.model when URL targets a deployment; remove it for tidiness.
    openai_body.pop("model", None)
    is_stream = bool(anthropic_body.get("stream", False))

    if is_stream:
        opts = openai_body.get("stream_options") or {}
        opts["include_usage"] = True
        openai_body["stream_options"] = opts
        openai_body["stream"] = True

    target_url = _build_url(entry, "chat/completions")
    headers = _build_headers(entry)
    client = get_azure_client()

    monitor_body = dict(anthropic_body)
    monitor_body["model"] = alias

    if is_stream:
        return await _stream_messages(
            client, target_url, openai_body, headers, user, alias, model_type, monitor_body, entry,
        )
    return await _non_stream_messages(
        client, target_url, openai_body, headers, user, alias, model_type, monitor_body, entry,
    )


async def _non_stream_messages(
    client, url: str, body: dict, headers: dict, user: User,
    alias: str, model_type: str, monitor_body: dict, route: dict,
) -> JSONResponse:
    try:
        resp = await client.post(url, json=body, headers=headers, timeout=_NON_STREAM_TIMEOUT)
    except Exception as exc:
        logger.error("Azure messages downstream error: {}", exc)
        log_monitor_error(user.id, monitor_body, str(exc), 502, alias, "/azure/v1/messages", model_type)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        log_monitor_error(user.id, monitor_body, resp.text[:500], resp.status_code, alias, "/azure/v1/messages", model_type)
        return _error_response(resp)

    openai_data = resp.json()
    anthropic_data = openai_to_anthropic_response(openai_data, alias)
    input_tk = anthropic_data["usage"]["input_tokens"]
    output_tk = anthropic_data["usage"]["output_tokens"]
    cached_tk = _cached_tokens(openai_data.get("usage", {}))
    _log_usage(user, alias, model_type, input_tk, output_tk, "/azure/v1/messages", route=route, cached_tokens=cached_tk)
    if is_monitored(user.id):
        cost = float(_calc_cost(route, model_type, input_tk, output_tk, cached_tk))
        log_monitor(user.id, monitor_body, anthropic_data, alias, "/azure/v1/messages", input_tk, output_tk, cost, model_type)
    return JSONResponse(content=anthropic_data)


async def _stream_messages(
    client, url: str, body: dict, headers: dict, user: User,
    alias: str, model_type: str, monitor_body: dict, route: dict,
) -> StreamingResponse:
    req = client.build_request("POST", url, json=body, headers=headers, timeout=None)
    _monitoring = is_monitored(user.id)
    logger.info("Stream start | user={} model={} endpoint=/azure/v1/messages", user.username, alias)

    async def event_generator():
        translator = AnthropicStreamTranslator(alias)
        chunks: list[dict] = []
        cached_tk = 0
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
                chunk_usage = chunk.get("usage")
                if chunk_usage:
                    cached_tk = _cached_tokens(chunk_usage) or cached_tk
                for event in translator.handle_chunk(chunk):
                    yield event

            # No finish_reason on a clean end → downstream dropped mid-stream.
            # Report an error rather than a normal message_stop so the client
            # doesn't silently accept a truncated answer.
            if translator.stop_reason is None:
                logger.warning(
                    "Azure messages stream ended without finish_reason | model={} — truncated",
                    alias,
                )
                for event in translator.fail(
                    "Downstream stream ended prematurely; the response may be incomplete."
                ):
                    yield event
            else:
                for event in translator.finish():
                    yield event
        except Exception as exc:
            logger.error("Azure messages stream error: {}", exc)
            err_payload = json.dumps({"type": "error", "error": {"type": "api_error", "message": str(exc)}})
            yield f"event: error\ndata: {err_payload}\n\n"

        input_tk = translator.input_tokens
        output_tk = translator.output_tokens
        _log_usage(user, alias, model_type, input_tk, output_tk, "/azure/v1/messages", route=route, cached_tokens=cached_tk)
        if _monitoring:
            cost = float(_calc_cost(route, model_type, input_tk, output_tk, cached_tk))
            log_monitor(user.id, monitor_body, chunks, alias, "/azure/v1/messages", input_tk, output_tk, cost, model_type)

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

    Azure OpenAI does not expose a tokenize endpoint, so we always return a
    chars/4 estimate. Auth and daily-limit checks still apply but the call
    is not billed.
    """
    try:
        anthropic_body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    alias = anthropic_body.get("model", "")
    _resolve_azure(alias)  # validate alias exists; raises 400 if not

    openai_body = anthropic_to_openai_request(anthropic_body)
    approx = _approx_token_count(openai_body.get("messages", []))
    return JSONResponse(content={"input_tokens": int(approx)})
