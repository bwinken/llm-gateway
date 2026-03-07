"""
OpenAI-compatible API endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.config import MODEL_ROUTING
from app.core.deps import get_current_user
from app.models.schema import User
from app.services.proxy import forward_request, forward_simple_request, forward_to_path

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
    for name, route in MODEL_ROUTING.items():
        model_type = route["type"]
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
