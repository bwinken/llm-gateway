"""
OpenAI-compatible API endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.config import get_model_routing_snapshot
from app.core.deps import get_current_user
from app.models.schema import User
from app.services.proxy import (
    forward_count_tokens_request,
    forward_messages_request,
    forward_request,
    forward_simple_request,
    forward_to_path,
)

router = APIRouter()

# Capability labels for the /v1/models response
_TYPE_CAPABILITIES: dict[str, str] = {
    "llm": "text-generation",
    "vlm": "vision-language",
    "embedding": "embeddings",
    "vision_embedding": "vision-embeddings",
    "reranker": "reranking",
    "vision_reranker": "vision-reranking",
}


@router.get("/v1/models")
async def list_models(user: User = Depends(get_current_user)):
    models = []
    for name, route in get_model_routing_snapshot().items():
        model_type = route["type"]
        if model_type not in ("llm", "vlm"):
            continue
        models.append(
            {
                "id": name,
                "object": "model",
                "owned_by": "llm-gateway",
                "type": model_type,
                "capability": _TYPE_CAPABILITIES.get(model_type, model_type),
            }
        )
    return {"object": "list", "data": models}


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, user: User = Depends(get_current_user)):
    return await forward_request(request, user, allowed_types=["llm", "vlm"])


@router.post("/v1/responses")
@router.post("/responses")
async def responses(request: Request, user: User = Depends(get_current_user)):
    return await forward_to_path(
        request, user, allowed_types=["llm", "vlm"], path_suffix="/responses"
    )


@router.post("/v1/messages")
@router.post("/messages")
async def messages(request: Request, user: User = Depends(get_current_user)):
    """Anthropic Messages API compatibility endpoint.

    Translates Anthropic /v1/messages requests to OpenAI chat completions
    format, forwards to the downstream LLM/VLM, then translates the response
    back to Anthropic format.

    Both ``/v1/messages`` and ``/messages`` are accepted so that clients work
    regardless of whether their ``ANTHROPIC_BASE_URL`` already includes the
    ``/v1`` prefix (a common Claude Code / Anthropic SDK misconfiguration
    that otherwise causes a silent 404 on the direct connection).
    """
    return await forward_messages_request(request, user, allowed_types=["llm", "vlm"])


@router.post("/v1/messages/count_tokens")
@router.post("/messages/count_tokens")
async def messages_count_tokens(
    request: Request, user: User = Depends(get_current_user)
):
    """Anthropic-compatible token counting endpoint.

    Used by Claude Code (and other Anthropic SDK clients) to estimate the
    input token count of a prospective request — typically for context
    window tracking. Forwards to the downstream vLLM ``/tokenize`` endpoint
    and returns ``{"input_tokens": N}``. Not billed.

    Mirrors ``/v1/messages`` by also exposing ``/messages/count_tokens`` for
    clients whose base URL already includes the ``/v1`` prefix.
    """
    return await forward_count_tokens_request(
        request, user, allowed_types=["llm", "vlm"]
    )


@router.post("/v1/embeddings")
async def embeddings(request: Request, user: User = Depends(get_current_user)):
    return await forward_simple_request(
        request,
        user,
        allowed_types=["embedding", "vision_embedding"],
        path_suffix="/embeddings",
        endpoint_label="/v1/embeddings",
    )


@router.post("/v1/rerank")
@router.post("/v1/score")
async def rerank(request: Request, user: User = Depends(get_current_user)):
    return await forward_simple_request(
        request,
        user,
        allowed_types=["reranker", "vision_reranker"],
        path_suffix="/score",
        endpoint_label="/v1/score",
    )
