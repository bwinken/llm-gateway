"""
OpenAI-compatible API endpoints for the vLLM downstream backend.

Mounted at ``/v1/*``. Chat / messages / count_tokens additionally dispatch
to Azure OpenAI when the requested ``model`` alias is configured under
``[azure_models.*]`` AND the caller has ``can_use_azure`` (or is admin),
so one base URL lets clients like Claude Code's model picker see both
backends side-by-side.

Design: each base URL has its own default fallback. ``/v1/*`` serves
vLLM-first (Azure is additive when the caller is authorized); aliases the
caller can't reach — whether typed wrong, or an Azure alias requested by a
user without access — silently fall back through ``_resolve_model`` to the
vLLM default. ``/azure/v1/*`` (see ``azure_api.py``) is the opposite: Azure
is the only backend there, with its own ``AZURE_FALLBACK_MAP``. A request's
fallback target is therefore determined by where it came in, not by what
permissions the user happens to lack — which matches the "be liberal with
unknown aliases" stance the gateway has always taken.

Every route is also exposed without the ``/v1`` prefix (``/chat/completions``,
``/models``, ``/embeddings``, ``/rerank``, ``/score`` alongside the canonical
``/v1/...`` paths; the Anthropic-shaped ``/messages``, ``/messages/count_tokens``,
``/responses``, ``/tokenize`` already had this alias). Clients whose base URL
omits ``/v1`` reach the same handler — a common Roo Code / Cline / Cursor
misconfiguration that previously surfaced as a silent 404.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.config import (
    _MODEL_METADATA_KEYS,
    get_azure_models_snapshot,
    get_model_routing_snapshot,
)
from app.core.deps import get_current_user
from app.core.logger import logger
from app.models.schema import User
from app.services.azure_proxy import (
    forward_chat_completions as azure_forward_chat_completions,
    forward_count_tokens as azure_forward_count_tokens,
    forward_messages as azure_forward_messages,
)
from app.services.vllm_proxy import (
    forward_count_tokens_request,
    forward_messages_request,
    forward_request,
    forward_simple_request,
    forward_to_path,
    forward_tokenize_request,
)

router = APIRouter()


async def _peek_model_alias(request: Request) -> str:
    """Read the ``model`` field from the request body without consuming it.

    FastAPI's ``Request`` caches the raw body bytes on first read, so a
    downstream call to ``request.json()`` inside ``forward_*`` will still
    succeed. Returns ``""`` on a body that isn't JSON or doesn't carry
    ``model`` so the downstream handler can produce its own error.
    """
    try:
        body = await request.json()
    except Exception:
        return ""
    if not isinstance(body, dict):
        return ""
    alias = body.get("model")
    return alias if isinstance(alias, str) else ""


def _route_to_azure(alias: str, user: User) -> bool:
    """Whether ``alias`` should be dispatched to the Azure backend.

    Returns True only when the alias is configured under ``[azure_models.*]``
    AND the caller has Azure access. Otherwise returns False, letting the
    vLLM handler take it — which, for an Azure-only alias from a user
    without ``can_use_azure``, means the request quietly falls back through
    ``_resolve_model`` to the vLLM default. That preserves the gateway's
    "be liberal with unknown aliases" stance: from the caller's perspective
    the Azure alias just behaves like any other unknown name on ``/v1/*``.

    Azure existence is still hidden via ``GET /v1/models`` (which omits
    Azure entries for users without permission), so this fallback only
    matters when a client explicitly forces an alias it shouldn't have
    seen — at which point a forgiving fallback is friendlier than a 404.
    """
    azure_models = get_azure_models_snapshot()
    if alias not in azure_models:
        return False
    if user.can_use_azure or user.is_admin:
        return True
    # Azure alias requested by user without access — fall through to the
    # vLLM handler so its _resolve_model can route to the configured
    # vLLM-side default. Logged so operators can spot clients that keep
    # asking for an alias they don't have access to.
    logger.info(
        "Azure alias '{}' requested by user '{}' without can_use_azure; "
        "falling back to vLLM default",
        alias, user.username,
    )
    return False

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
@router.get("/models")
async def list_models(user: User = Depends(get_current_user)):
    """List chat-capable models available to this user.

    vLLM entries are always included. Azure entries are merged in only when
    the user has ``can_use_azure`` (or is admin) so an Anthropic / OpenAI
    client pointed at this single base URL sees exactly the set of aliases
    its API key may actually use.

    The ``hidden`` flag is intentionally NOT filtered here (operators rely on
    it only for the web UI; existing tests pin this contract).
    """
    models = []
    for name, route in get_model_routing_snapshot().items():
        model_type = route["type"]
        if model_type not in ("llm", "vlm"):
            continue
        entry: dict[str, object] = {
            "id": name,
            "object": "model",
            "owned_by": "llm-gateway",
            "type": model_type,
            "capability": _TYPE_CAPABILITIES.get(model_type, model_type),
        }
        # Surface optional per-model metadata (context_window,
        # max_output_tokens, supports_*, display_name, ...) when it's
        # declared in config.toml. Any missing key is simply omitted so
        # existing configs — which don't set any of these — produce the
        # exact same response they did before.
        for meta_key in _MODEL_METADATA_KEYS:
            if meta_key in route:
                entry[meta_key] = route[meta_key]
        models.append(entry)

    if user.can_use_azure or user.is_admin:
        for alias, route in get_azure_models_snapshot().items():
            model_type = route.get("type", "llm")
            if model_type not in ("llm", "vlm"):
                continue
            entry = {
                "id": alias,
                "object": "model",
                "owned_by": "azure-openai",
                "type": model_type,
                "capability": _TYPE_CAPABILITIES.get(model_type, model_type),
            }
            for meta_key in _MODEL_METADATA_KEYS:
                if meta_key in route:
                    entry[meta_key] = route[meta_key]
            models.append(entry)

    return {"object": "list", "data": models}


@router.post("/v1/chat/completions")
@router.post("/chat/completions")
async def chat_completions(request: Request, user: User = Depends(get_current_user)):
    alias = await _peek_model_alias(request)
    if alias and _route_to_azure(alias, user):
        return await azure_forward_chat_completions(request, user)
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

    Dispatches to the Azure backend when the requested ``model`` alias is
    Azure-configured and the user has permission; see ``_route_to_azure``.
    """
    alias = await _peek_model_alias(request)
    if alias and _route_to_azure(alias, user):
        return await azure_forward_messages(request, user)
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

    Same Azure dispatch rule as ``/v1/messages``: Azure-configured aliases
    are routed to the Azure count-tokens helper (chars/4 estimate) when the
    user has permission; otherwise vLLM's tokenizer-backed count is used.
    """
    alias = await _peek_model_alias(request)
    if alias and _route_to_azure(alias, user):
        return await azure_forward_count_tokens(request, user)
    return await forward_count_tokens_request(
        request, user, allowed_types=["llm", "vlm"]
    )


@router.post("/v1/tokenize")
@router.post("/tokenize")
async def tokenize(request: Request, user: User = Depends(get_current_user)):
    """vLLM-native ``/tokenize`` pass-through.

    vLLM exposes ``/tokenize`` (not ``/v1/tokenize``) since it isn't part of
    the OpenAI API spec. The gateway accepts both paths for client convenience
    and forwards to the downstream ``/tokenize`` endpoint. Not billed.
    """
    return await forward_tokenize_request(
        request, user, allowed_types=["llm", "vlm"]
    )


@router.post("/v1/embeddings")
@router.post("/embeddings")
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
@router.post("/rerank")
@router.post("/score")
async def rerank(request: Request, user: User = Depends(get_current_user)):
    return await forward_simple_request(
        request,
        user,
        allowed_types=["reranker", "vision_reranker"],
        path_suffix="/score",
        endpoint_label="/v1/score",
    )
