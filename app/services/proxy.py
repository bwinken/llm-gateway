"""
Core proxy logic:
  1. forward_request        - Standard Chat Completion (streaming + non-streaming)
  2. forward_simple_request - Embeddings & Rerankers (non-streaming, 120s timeout)
  3. forward_to_path        - Pure pass-through for custom APIs (e.g. /responses)
  4. Type-safe smart fallback
  5. Usage logging
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlmodel import Session

from app.core.config import FALLBACK_MAP, MODEL_ROUTING, PRICING_MAP
from app.core.database import engine
from app.core.logger import logger
from app.core.server_state import get_client, is_alive
from app.models.schema import UsageLog, User


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
    for fb_name, fb_route in MODEL_ROUTING.items():
        if fb_route["type"] in allowed_types and is_alive(fb_route["base_url"]):
            logger.warning("{} - falling back to '{}'", reason, fb_name)
            return fb_name, fb_route, reason

    # No alive server — best effort: use any model with compatible type
    for fb_name, fb_route in MODEL_ROUTING.items():
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


def _calc_cost(model_type: str, input_tokens: int, output_tokens: int) -> float:
    pricing = PRICING_MAP.get(model_type, PRICING_MAP.get("_default", {}))
    inp_price = pricing.get("input_price_per_1m", 0.0)
    out_price = pricing.get("output_price_per_1m", 0.0)
    return (input_tokens * inp_price + output_tokens * out_price) / 1_000_000


def _log_usage(
    user: User,
    model: str,
    model_type: str,
    input_tokens: int,
    output_tokens: int,
    endpoint: str,
) -> None:
    cost = _calc_cost(model_type, input_tokens, output_tokens)
    with Session(engine) as session:
        log = UsageLog(
            user_id=user.id,  # type: ignore[arg-type]
            model=model,
            model_type=model_type,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            endpoint=endpoint,
        )
        session.add(log)
        session.commit()
    logger.info(
        "Usage | user={} model={} in={} out={} cost=${:.6f}",
        user.username,
        model,
        input_tokens,
        output_tokens,
        cost,
    )


# ---------------------------------------------------------------------------
# 1. forward_request - Standard Chat Completion (stream + non-stream)
# ---------------------------------------------------------------------------

def _fallback_headers(fallback_reason: str | None) -> dict[str, str]:
    """Return extra response headers when a fallback occurred."""
    if fallback_reason:
        return {"X-Model-Fallback": fallback_reason}
    return {}


async def forward_request(
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

    if is_stream:
        return await _stream_chat(
            client, target_url, body, downstream_headers, user, resolved_alias, model_type,
            extra_headers,
        )
    else:
        return await _non_stream_chat(
            client, target_url, body, downstream_headers, user, resolved_alias, model_type,
            extra_headers,
        )


async def _non_stream_chat(
    client, url: str, body: dict, headers: dict, user: User, model: str, model_type: str,
    extra_headers: dict[str, str] | None = None,
) -> JSONResponse:
    try:
        resp = await client.post(url, json=body, headers=headers, timeout=120.0)
    except Exception as exc:
        logger.error("Downstream error: {}", exc)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        return JSONResponse(content=resp.json(), status_code=resp.status_code)

    data = resp.json()
    usage = data.get("usage", {})
    _log_usage(
        user,
        model,
        model_type,
        usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0),
        "/v1/chat/completions",
    )
    return JSONResponse(content=data, headers=extra_headers)


async def _stream_chat(
    client, url: str, body: dict, headers: dict, user: User, model: str, model_type: str,
    extra_headers: dict[str, str] | None = None,
) -> StreamingResponse:
    req = client.build_request("POST", url, json=body, headers=headers, timeout=120.0)

    async def event_generator():
        input_tokens = 0
        output_tokens = 0
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
                        usage = chunk.get("usage")
                        if usage:
                            input_tokens = usage.get("prompt_tokens", input_tokens)
                            output_tokens = usage.get("completion_tokens", output_tokens)
                    except (json.JSONDecodeError, KeyError):
                        pass

            await resp.aclose()
        except Exception as exc:
            logger.error("Stream error: {}", exc)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

        _log_usage(user, model, model_type, input_tokens, output_tokens, "/v1/chat/completions")

    resp_headers = {"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}
    if extra_headers:
        resp_headers.update(extra_headers)
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=resp_headers,
    )


# ---------------------------------------------------------------------------
# 2. forward_simple_request - Embeddings & Rerankers (non-streaming)
# ---------------------------------------------------------------------------

async def forward_simple_request(
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
        resp = await client.post(target_url, json=body, headers=downstream_headers, timeout=120.0)
    except Exception as exc:
        logger.error("Downstream error: {}", exc)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        return JSONResponse(content=resp.json(), status_code=resp.status_code)

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
# 3. forward_to_path - Pure pass-through (e.g. /responses)
# ---------------------------------------------------------------------------

async def forward_to_path(
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
            user, resolved_alias, model_type, path_suffix, extra_headers,
        )
    else:
        return await _passthrough_non_stream(
            client, target_url, raw_body, downstream_headers,
            user, resolved_alias, model_type, path_suffix, extra_headers,
        )


async def _passthrough_non_stream(
    client, url: str, raw_body: bytes, headers: dict,
    user: User, model: str, model_type: str, path_suffix: str,
    extra_headers: dict[str, str] | None = None,
) -> JSONResponse:
    try:
        resp = await client.post(url, content=raw_body, headers=headers, timeout=120.0)
    except Exception as exc:
        logger.error("Downstream error: {}", exc)
        raise HTTPException(status_code=502, detail=f"Downstream error: {exc}")

    if resp.status_code != 200:
        return JSONResponse(content=resp.json(), status_code=resp.status_code)

    data = resp.json()
    usage = data.get("usage", {})
    _log_usage(
        user,
        model,
        model_type,
        usage.get("input_tokens", usage.get("prompt_tokens", 0)),
        usage.get("output_tokens", usage.get("completion_tokens", 0)),
        path_suffix,
    )
    return JSONResponse(content=data, headers=extra_headers)


async def _passthrough_stream(
    client, url: str, raw_body: bytes, headers: dict,
    user: User, model: str, model_type: str, path_suffix: str,
    extra_headers: dict[str, str] | None = None,
) -> StreamingResponse:
    req = client.build_request("POST", url, content=raw_body, headers=headers, timeout=120.0)

    async def event_generator():
        input_tokens = 0
        output_tokens = 0
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
                        usage = chunk.get("usage")
                        if usage:
                            input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", input_tokens))
                            output_tokens = usage.get("output_tokens", usage.get("completion_tokens", output_tokens))
                    except (json.JSONDecodeError, KeyError):
                        pass

            await resp.aclose()
        except Exception as exc:
            logger.error("Stream error: {}", exc)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

        _log_usage(user, model, model_type, input_tokens, output_tokens, path_suffix)

    resp_headers = {"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}
    if extra_headers:
        resp_headers.update(extra_headers)
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=resp_headers,
    )
