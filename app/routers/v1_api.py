"""
Unified ``/v1/*`` public API surface.

Routes the OpenAI-compatible and Anthropic-compatible endpoints. Defaults
to the vLLM backend (via ``vllm_proxy``); chat / responses / messages /
count_tokens additionally dispatch to Azure OpenAI (via ``azure_proxy``)
when the requested ``model`` alias is configured under ``[azure_models.*]``
AND the caller has ``can_use_azure`` (or is admin). One base URL lets clients
like Claude Code's model picker see both backends side-by-side.

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
``/models``, ``/embeddings``, ``/rerank``, ``/score``, ``/chat/completions/render``
alongside the canonical ``/v1/...`` paths; the Anthropic-shaped ``/messages``,
``/messages/count_tokens``, ``/responses``, ``/tokenize`` already had this alias). Clients whose base URL
omits ``/v1`` reach the same handler — a common Roo Code / Cline / Cursor
misconfiguration that previously surfaced as a silent 404.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool

from app.core.config import (
    _MODEL_METADATA_KEYS,
    get_azure_models_snapshot,
    get_bedrock_models_snapshot,
    get_model_routing_snapshot,
)
from app.core.deps import ensure_azure_budget, ensure_bedrock_budget, get_current_user
from app.core.logger import logger
from app.models.schema import User
from app.services.azure_proxy import (
    azure_forward_chat_completions,
    azure_forward_count_tokens,
    azure_forward_messages,
    azure_forward_responses,
)
from app.services.bedrock_proxy import (
    bedrock_forward_chat_completions,
    bedrock_forward_count_tokens,
    bedrock_forward_messages,
)
from app.services.vllm_proxy import (
    vllm_forward_chat_completions,
    vllm_forward_count_tokens,
    vllm_forward_messages,
    vllm_forward_render,
    vllm_forward_responses,
    vllm_forward_simple_request,
    vllm_forward_tokenize,
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


def _route_to_bedrock(alias: str, user: User) -> bool:
    """Whether ``alias`` should be dispatched to the Bedrock backend.

    Same contract as ``_route_to_azure``: True only when the alias is
    configured under ``[bedrock_models.*]`` AND the caller has Bedrock
    access; otherwise the request stays on the vLLM path and falls back
    through ``_resolve_model`` like any unknown alias.
    """
    bedrock_models = get_bedrock_models_snapshot()
    if alias not in bedrock_models:
        return False
    if user.can_use_bedrock or user.is_admin:
        return True
    logger.info(
        "Bedrock alias '{}' requested by user '{}' without can_use_bedrock; "
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

    if user.can_use_bedrock or user.is_admin:
        for alias, route in get_bedrock_models_snapshot().items():
            model_type = route.get("type", "llm")
            if model_type not in ("llm", "vlm"):
                continue
            entry = {
                "id": alias,
                "object": "model",
                "owned_by": "aws-bedrock",
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
        # Azure-bound: enforce the per-user Azure daily sub-limit (429 on
        # exceed — deliberately NOT a silent fallback to vLLM, so the caller
        # knows their Azure budget is exhausted rather than getting a
        # different model's output). Blocking DB read → threadpool.
        await run_in_threadpool(ensure_azure_budget, user)
        return await azure_forward_chat_completions(request, user)
    if alias and _route_to_bedrock(alias, user):
        # Same contract as the Azure gate above, for the Bedrock sub-limit.
        await run_in_threadpool(ensure_bedrock_budget, user)
        return await bedrock_forward_chat_completions(request, user)
    return await vllm_forward_chat_completions(request, user, allowed_types=["llm", "vlm"])


# OpenAPI documentation for the render endpoint.
#
# Every handler in this router takes a raw ``Request`` (the gateway is a
# pass-through — binding a Pydantic body model would make FastAPI validate
# and silently DROP vLLM's long tail of extra fields: chat_template_kwargs,
# vllm_xargs, structured_outputs, …), so FastAPI has no body schema to infer
# and ``/docs`` renders these operations with no request body at all. That is
# tolerable for the generation endpoints, whose shape every OpenAI client
# already knows — but this one is a debug aid whose whole audience is a human
# poking at Swagger UI's "Try it out", and an empty body box is useless there.
#
# ``openapi_extra`` documents the body without binding it: the schema below is
# descriptive only, nothing here is enforced. It names the fields that actually
# change what gets rendered rather than mirroring vLLM's full
# ChatCompletionRequest (which drifts per vLLM release — copying it would be a
# promise this gateway can't keep). ``additionalProperties: true`` is the honest
# statement of the contract: anything else you send is forwarded verbatim.
_RENDER_REQUEST_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["model", "messages"],
    "additionalProperties": True,
    "description": (
        "A standard OpenAI chat completions body. Only the fields that affect "
        "rendering are documented here; any other field (sampling knobs, "
        "vLLM extensions such as `vllm_xargs` or `structured_outputs`, …) is "
        "forwarded to the downstream verbatim and shows up in the resolved "
        "`sampling_params` of the response."
    ),
    "properties": {
        "model": {
            "type": "string",
            "description": "Model alias as listed by `GET /v1/models`.",
        },
        "messages": {
            "type": "array",
            "description": "Chat messages, exactly as for `/v1/chat/completions`.",
            "items": {"type": "object", "additionalProperties": True},
        },
        "tools": {
            "type": "array",
            "description": (
                "Tool definitions. Included in the render because the chat "
                "template is what turns them into prompt text."
            ),
            "items": {"type": "object", "additionalProperties": True},
        },
        "tool_choice": {"description": "`none` | `auto` | `required` | a tool object."},
        "documents": {
            "type": "array",
            "description": "RAG documents, for templates that render them.",
            "items": {"type": "object", "additionalProperties": True},
        },
        "chat_template": {
            "type": "string",
            "description": "Override the model's chat template for this render.",
        },
        "chat_template_kwargs": {
            "type": "object",
            "additionalProperties": True,
            "description": (
                "Extra variables passed to the chat template — e.g. "
                "`{\"enable_thinking\": false}` on Qwen3."
            ),
        },
        "mm_processor_kwargs": {
            "type": "object",
            "additionalProperties": True,
            "description": "Multi-modal processor options (VLM routes).",
        },
        "add_generation_prompt": {
            "type": "boolean",
            "default": True,
            "description": "Append the assistant generation prefix, as generation would.",
        },
        "continue_final_message": {
            "type": "boolean",
            "default": False,
            "description": "Continue the last assistant message instead of starting a new turn.",
        },
        "temperature": {"type": "number"},
        "top_p": {"type": "number"},
        "max_tokens": {"type": "integer"},
        "stop": {"description": "String or array of stop sequences."},
        "reasoning_effort": {
            "type": "string",
            "description": "Passed through; visible in the resolved sampling_params.",
        },
    },
    "example": {
        "model": "your-model-alias",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"},
        ],
        "add_generation_prompt": True,
        "temperature": 0.7,
    },
}

_RENDER_OPENAPI: dict[str, object] = {
    "summary": "Render a chat request without generating (debug)",
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": _RENDER_REQUEST_SCHEMA,
                # Named examples become a dropdown in Swagger UI — the three
                # questions this endpoint actually gets asked.
                "examples": {
                    "minimal": {
                        "summary": "What does the template do to my messages?",
                        "value": {
                            "model": "your-model-alias",
                            "messages": [
                                {"role": "system", "content": "You are a helpful assistant."},
                                {"role": "user", "content": "Hello!"},
                            ],
                        },
                    },
                    "with_tools": {
                        "summary": "How are my tools rendered into the prompt?",
                        "description": (
                            "Tool definitions are prompt text after the chat "
                            "template runs — this is how you see what the model "
                            "is actually told about them."
                        ),
                        "value": {
                            "model": "your-model-alias",
                            "messages": [{"role": "user", "content": "What is the weather?"}],
                            "tools": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "description": "Get the weather for a city.",
                                        "parameters": {
                                            "type": "object",
                                            "properties": {"city": {"type": "string"}},
                                            "required": ["city"],
                                        },
                                    },
                                }
                            ],
                        },
                    },
                    "template_kwargs": {
                        "summary": "Did my chat_template_kwargs take effect?",
                        "description": (
                            "Template switches such as Qwen3's `enable_thinking` "
                            "only show up in the rendered prompt — compare two "
                            "renders to confirm the flag did anything."
                        ),
                        "value": {
                            "model": "your-model-alias",
                            "messages": [{"role": "user", "content": "Hello!"}],
                            "chat_template_kwargs": {"enable_thinking": False},
                        },
                    },
                },
            }
        },
    },
    "responses": {
        "200": {
            "description": (
                "The rendered request, as a single object — whatever the "
                "downstream vLLM returned, with `model` swapped back to the "
                "alias you asked for.\n\n"
                "| field | meaning |\n"
                "|---|---|\n"
                "| `decoded_prompt` | The prompt as **text**. Added by the "
                "gateway (vLLM does not return it); omitted when "
                "`?decode=false`. This is the field you usually want. |\n"
                "| `token_ids` | The prompt as token **IDs** — integers, not "
                "text. What the engine is actually handed. |\n"
                "| `sampling_params` | The parameters the engine would run "
                "with, defaults filled in — so you can see what your request "
                "resolved to. |\n"
                "| `decode_error` | Present only when `decode` was asked for "
                "and failed; the render itself is still returned. |\n"
            ),
            "content": {
                "application/json": {
                    "examples": {
                        "default": {
                            "summary": "Default (decode on)",
                            "value": {
                                "request_id": "chatcmpl-b1f0c2e4",
                                "model": "your-model-alias",
                                "decoded_prompt": (
                                    "<|im_start|>system\nYou are a helpful "
                                    "assistant.<|im_end|>\n<|im_start|>user\n"
                                    "Hello!<|im_end|>\n<|im_start|>assistant\n"
                                ),
                                "token_ids": [151644, 8948, 198, 2610, 525],
                                "sampling_params": {
                                    "temperature": 0.7,
                                    "top_p": 1.0,
                                    "max_tokens": 4096,
                                },
                            },
                        },
                        "decode_false": {
                            "summary": "?decode=false — exactly what vLLM sent",
                            "value": {
                                "request_id": "chatcmpl-b1f0c2e4",
                                "model": "your-model-alias",
                                "token_ids": [151644, 8948, 198, 2610, 525],
                                "sampling_params": {"temperature": 0.7},
                            },
                        },
                        "decode_failed": {
                            "summary": "Detokenize failed — render still returned",
                            "value": {
                                "request_id": "chatcmpl-b1f0c2e4",
                                "model": "your-model-alias",
                                "token_ids": [151644, 8948, 198, 2610, 525],
                                "decode_error": "detokenize returned HTTP 404",
                            },
                        },
                    }
                }
            },
        },
        "404": {
            "description": (
                "Propagated from the downstream. Most likely its vLLM is too "
                "old to serve `/chat/completions/render` — the endpoint "
                "arrived with the disaggregated-serving render server "
                "(vllm-project/vllm#36166). Nothing to configure on the "
                "gateway; the model has to be on a newer vLLM."
            )
        },
        "429": {"description": "Daily spend limit exceeded (this call is not itself billed)."},
        "502": {"description": "Downstream unreachable."},
    },
}


@router.post("/v1/chat/completions/render", openapi_extra=_RENDER_OPENAPI)
@router.post("/chat/completions/render", openapi_extra=_RENDER_OPENAPI)
async def chat_completions_render(
    request: Request,
    decode: bool = Query(
        True,
        description=(
            "Return the rendered prompt as text under `decoded_prompt` "
            "(default). Costs one extra call to the same server's "
            "`/detokenize`; on failure the render is still returned, with the "
            "reason under `decode_error`. Pass `false` for a pure "
            "pass-through of what vLLM returned."
        ),
    ),
    user: User = Depends(get_current_user),
):
    """See exactly what the model receives — the chat template applied to your
request, **without generating anything**.

Post the same body you would post to `/v1/chat/completions`; you get back the
prompt that request renders into, plus the sampling parameters it resolves to.
Nothing is generated, so it is fast and **not billed**.

### Use it when

* the model behaves as if it saw something other than what you sent
* you want to check how your `tools` are turned into prompt text
* you are tuning `chat_template_kwargs` (e.g. Qwen3's `enable_thinking`) and
  need to confirm the switch actually changed the prompt
* you want the resolved `sampling_params` — the defaults your request inherits

### Reading the response

`decoded_prompt` is the prompt as text and is usually the only field you need.
vLLM itself returns token IDs only, so the gateway detokenizes them for you;
pass `?decode=false` to skip that and get exactly what vLLM sent. If the
detokenize step fails the render is still returned, with the reason in
`decode_error`.

### Try it

```bash
curl -s "$GATEWAY/v1/chat/completions/render" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "your-model-alias",
       "messages": [{"role": "user", "content": "Hello!"}]}' \
  | jq -r .decoded_prompt
```

With the OpenAI SDK there is no typed method for this endpoint — use the raw
request escape hatch, which keeps your existing base URL and key:

```python
import httpx
resp = client.post("/chat/completions/render", cast_to=httpx.Response,
                   body={"model": "your-model-alias",
                         "messages": [{"role": "user", "content": "Hello!"}]})
print(resp.json()["decoded_prompt"])
```

### Limits

* **On-prem vLLM only.** Azure and Bedrock have no rendering surface, so a
  cloud alias posted here is not dispatched to them — it falls back to a vLLM
  model like any alias the vLLM side doesn't know (same as `/v1/tokenize`).
* Any field not listed in the schema below is **forwarded verbatim**; this is
  a pass-through, not a validated API.
* A downstream on an older vLLM answers 404 — see the response below.
"""
    return await vllm_forward_render(
        request, user, allowed_types=["llm", "vlm"], decode=decode
    )


@router.post("/v1/responses")
@router.post("/responses")
async def responses(request: Request, user: User = Depends(get_current_user)):
    """OpenAI Responses API endpoint.

    Dispatches to the Azure backend (pure Responses pass-through) when the
    requested ``model`` alias is Azure-configured and the user has permission;
    otherwise forwards to vLLM. Same Azure dispatch rule as
    ``/v1/chat/completions``; see ``_route_to_azure``.
    """
    alias = await _peek_model_alias(request)
    if alias and _route_to_azure(alias, user):
        # Same Azure sub-limit gate as /v1/chat/completions.
        await run_in_threadpool(ensure_azure_budget, user)
        return await azure_forward_responses(request, user)
    return await vllm_forward_responses(
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
        # Same Azure sub-limit gate as /v1/chat/completions.
        await run_in_threadpool(ensure_azure_budget, user)
        return await azure_forward_messages(request, user)
    if alias and _route_to_bedrock(alias, user):
        await run_in_threadpool(ensure_bedrock_budget, user)
        return await bedrock_forward_messages(request, user)
    return await vllm_forward_messages(request, user, allowed_types=["llm", "vlm"])


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
        # Gated like the billable Azure paths for parity with
        # /azure/v1/messages/count_tokens (whose require_azure_access
        # dependency also enforces the sub-limit).
        await run_in_threadpool(ensure_azure_budget, user)
        return await azure_forward_count_tokens(request, user)
    if alias and _route_to_bedrock(alias, user):
        await run_in_threadpool(ensure_bedrock_budget, user)
        return await bedrock_forward_count_tokens(request, user)
    return await vllm_forward_count_tokens(
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
    return await vllm_forward_tokenize(
        request, user, allowed_types=["llm", "vlm"]
    )


@router.post("/v1/embeddings")
@router.post("/embeddings")
async def embeddings(request: Request, user: User = Depends(get_current_user)):
    return await vllm_forward_simple_request(
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
    return await vllm_forward_simple_request(
        request,
        user,
        allowed_types=["reranker", "vision_reranker"],
        path_suffix="/score",
        endpoint_label="/v1/score",
    )
