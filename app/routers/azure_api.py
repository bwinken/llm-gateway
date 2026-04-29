"""
Azure OpenAI-compatible API endpoints.

Mounted at `/azure/v1/*`. Requests use the same client API key auth as the
main `/v1/*` endpoints; usage logging, daily limits, monitoring, and pricing
are all shared. Only the downstream URL/header conventions differ — handled
by `app/services/azure_proxy.py`.

This module is intentionally separate from `vllm_api.py` so the Azure-specific
routing logic does not leak into the main OpenAI/vLLM code path.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.config import _MODEL_METADATA_KEYS, get_azure_models_snapshot
from app.core.deps import get_current_user
from app.models.schema import User
from app.services.azure_proxy import (
    forward_chat_completions,
    forward_count_tokens,
    forward_embeddings,
    forward_messages,
)

router = APIRouter()

_TYPE_CAPABILITIES: dict[str, str] = {
    "llm": "text-generation",
    "vlm": "vision-language",
    "embedding": "embeddings",
    "vision_embedding": "vision-embeddings",
    "reranker": "reranking",
    "vision_reranker": "vision-reranking",
}


@router.get("/azure/v1/models")
async def list_azure_models(user: User = Depends(get_current_user)):
    """List configured Azure OpenAI deployments as OpenAI-shaped model entries."""
    models = []
    for alias, entry in get_azure_models_snapshot().items():
        model_type = entry.get("type", "llm")
        out: dict[str, object] = {
            "id": alias,
            "object": "model",
            "owned_by": "azure-openai",
            "type": model_type,
            "capability": _TYPE_CAPABILITIES.get(model_type, model_type),
        }
        for meta_key in _MODEL_METADATA_KEYS:
            if meta_key in entry:
                out[meta_key] = entry[meta_key]
        models.append(out)
    return {"object": "list", "data": models}


@router.post("/azure/v1/chat/completions")
async def azure_chat_completions(
    request: Request,
    user: User = Depends(get_current_user),
):
    return await forward_chat_completions(request, user)


@router.post("/azure/v1/embeddings")
async def azure_embeddings(
    request: Request,
    user: User = Depends(get_current_user),
):
    return await forward_embeddings(request, user)


@router.post("/azure/v1/messages")
@router.post("/azure/messages")
async def azure_messages(
    request: Request,
    user: User = Depends(get_current_user),
):
    """Anthropic Messages API compatibility for Azure OpenAI deployments.

    Both ``/azure/v1/messages`` and ``/azure/messages`` are accepted so that
    Anthropic SDK / Claude Code clients work regardless of whether their
    ``ANTHROPIC_BASE_URL`` already includes the ``/v1`` prefix.
    """
    return await forward_messages(request, user)


@router.post("/azure/v1/messages/count_tokens")
@router.post("/azure/messages/count_tokens")
async def azure_count_tokens(
    request: Request,
    user: User = Depends(get_current_user),
):
    """Token counting for Azure deployments — returns a chars/4 estimate
    (Azure OpenAI does not expose a tokenize endpoint)."""
    return await forward_count_tokens(request, user)
