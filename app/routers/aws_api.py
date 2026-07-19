"""
AWS Bedrock API endpoints.

Mounted at `/aws/v1/*` (the user-facing prefix is "aws"; internally the
backend is named "bedrock" everywhere — config section, permission flag,
usage_logs backend tag). Requests use the same client API key auth as the
main `/v1/*` endpoints; usage logging, daily limits, observability, and
pricing are all shared. Only the downstream URL/auth/framing conventions
differ — handled by `app/services/bedrock_proxy.py`.

This module is intentionally separate from `v1_api.py` (the unified `/v1/*`
surface) so the Bedrock-specific routing stays scoped to clients that
explicitly opted into the `/aws` prefix — mirroring `azure_api.py`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.config import _MODEL_METADATA_KEYS, get_bedrock_models_snapshot
from app.core.deps import require_bedrock_access
from app.models.schema import User
from app.services.bedrock_proxy import (
    bedrock_forward_chat_completions,
    bedrock_forward_count_tokens,
    bedrock_forward_messages,
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


@router.get("/aws/v1/models")
@router.get("/aws/models")
async def list_bedrock_models(user: User = Depends(require_bedrock_access)):
    """List configured Bedrock models as OpenAI-shaped model entries.

    Both ``/aws/v1/models`` and ``/aws/models`` are accepted so clients work
    regardless of whether their base URL already includes the ``/v1`` prefix.
    """
    models = []
    for alias, entry in get_bedrock_models_snapshot().items():
        model_type = entry.get("type", "llm")
        out: dict[str, object] = {
            "id": alias,
            "object": "model",
            "owned_by": "aws-bedrock",
            "type": model_type,
            "capability": _TYPE_CAPABILITIES.get(model_type, model_type),
        }
        for meta_key in _MODEL_METADATA_KEYS:
            if meta_key in entry:
                out[meta_key] = entry[meta_key]
        models.append(out)
    return {"object": "list", "data": models}


@router.post("/aws/v1/chat/completions")
@router.post("/aws/chat/completions")
async def bedrock_chat_completions(
    request: Request,
    user: User = Depends(require_bedrock_access),
):
    """OpenAI chat completions for Bedrock models (translated to Converse).

    Both ``/aws/v1/chat/completions`` and ``/aws/chat/completions`` are
    accepted so clients work regardless of whether their base URL already
    includes the ``/v1`` prefix.
    """
    return await bedrock_forward_chat_completions(request, user)


@router.post("/aws/v1/messages")
@router.post("/aws/messages")
async def bedrock_messages(
    request: Request,
    user: User = Depends(require_bedrock_access),
):
    """Anthropic Messages API compatibility for Bedrock models.

    Both ``/aws/v1/messages`` and ``/aws/messages`` are accepted so that
    Anthropic SDK / Claude Code clients work regardless of whether their
    ``ANTHROPIC_BASE_URL`` already includes the ``/v1`` prefix.
    """
    return await bedrock_forward_messages(request, user)


@router.post("/aws/v1/messages/count_tokens")
@router.post("/aws/messages/count_tokens")
async def bedrock_count_tokens(
    request: Request,
    user: User = Depends(require_bedrock_access),
):
    """Token counting for Bedrock models — returns a chars/4 estimate
    (Bedrock does not expose a tokenize endpoint)."""
    return await bedrock_forward_count_tokens(request, user)
