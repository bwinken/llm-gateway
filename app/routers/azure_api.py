"""
Azure OpenAI-compatible API endpoints.

Mounted at `/azure/v1/*`. Requests use the same client API key auth as the
main `/v1/*` endpoints; usage logging, daily limits, monitoring, and pricing
are all shared. Only the downstream URL/header conventions differ — handled
by `app/services/azure_proxy.py`.

This module is intentionally separate from `v1_api.py` (the unified `/v1/*`
surface) so the Azure-specific routing logic stays scoped to clients that
explicitly opted into the `/azure` prefix.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core import api_docs

from app.core.config import _MODEL_METADATA_KEYS, get_azure_models_snapshot, listed_in_api
from app.core.deps import limited_azure_access, require_azure_access
from app.models.schema import User
from app.services import client_rules
from app.services.azure_proxy import (
    azure_forward_chat_completions,
    azure_forward_count_tokens,
    azure_forward_messages,
    azure_forward_responses,
)

# What every route of this group can answer, besides the model API's own:
# the account must have been given this cloud.
_ERRORS = {
    **api_docs.MODEL_API_ERRORS,
    403: {"description": "The account has no Azure access (an admin grants it). Or: "
                         + api_docs.MODEL_API_ERRORS[403]["description"][0].lower()
                         + api_docs.MODEL_API_ERRORS[403]["description"][1:]},
    429: {"description": "The cloud cap for today is spent (Azure and AWS share one), or today's overall budget "
                         "is (`Retry-After`: seconds until midnight, Asia/Taipei). There is no on-prem stand-in "
                         "on this route. Or this address sent too many different invalid keys and is locked."},
}
_BUSY = {429: {"description": _ERRORS[429]["description"] + " Also: the account already has as many cloud "
                               "requests in flight as it may (`Retry-After: 5`)."}}
_REMINDER = {400: {"description": api_docs.REMINDER_400[400]["description"]}}

router = APIRouter(tags=["azure"], responses=_ERRORS)

# Every docstring below is the route's description in /docs and openapi.json,
# written to the plan in ``app/core/api_docs.py``.

_TYPE_CAPABILITIES: dict[str, str] = {
    "llm": "text-generation",
    "vlm": "vision-language",
    "embedding": "embeddings",
    "vision_embedding": "vision-embeddings",
    "reranker": "reranking",
    "vision_reranker": "vision-reranking",
}


@router.get(
    "/azure/v1/models", summary="Azure models to pick from", responses=api_docs.answers(api_docs.ModelList),
)
@router.get(
    "/azure/models", summary="Azure models (alias of /azure/v1/models)",
    responses=api_docs.answers(api_docs.ModelList),
)
async def list_azure_models(user: User = Depends(require_azure_access)):
    """The Azure models the gateway serves, in OpenAI's list shape.

    **Sign-in.** API key only: `Authorization: Bearer <key>` or `x-api-key: <key>`. An SSO access token is not a key here.

    **Response**

    | Key | Type | Meaning |
    |---|---|---|
    | `object` | string | `list` |
    | `data` | array | One entry per model |
    | `data[].id` | string | The name to send as `model` |
    | `data[].object`, `owned_by` | string | `model`, `azure-openai` |
    | `data[].type`, `capability` | string | The kind of model, and the same in words |
    | `data[].display_name`, `context_window`, `max_output_tokens`, `supports_tools`, `supports_vision`, `supports_prompt_caching`, `is_reasoning` | various | What the admin declared for the model. Each absent when not declared |

    **Limits and things to know**

    - Azure models of every kind, where `GET /v1/models` lists chat models of every backend.
    - An admin can leave a model out of this list; a request that names it is still served.
    - The account needs Azure access, or it is a 403.
    - Not billed, but it passes the budget checks: 429 once today's budget or the cloud cap is spent.

    **Errors.** Only the group's (400, 401, 403, 429).
    """
    models = []
    for alias, entry in get_azure_models_snapshot().items():
        if not listed_in_api(entry):
            continue  # an admin left it out of the API listing: still served by alias
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


_CHAT = {**api_docs.upstream(api_docs.OPENAI_CHAT), **_BUSY}


@router.post("/azure/v1/chat/completions", summary="Chat completion on Azure (OpenAI format)", responses=_CHAT)
@router.post(
    "/azure/chat/completions", summary="Chat completion on Azure (alias of /azure/v1/chat/completions)",
    responses=_CHAT,
)
async def azure_chat_completions(
    request: Request,
    user: User = Depends(limited_azure_access),
):
    """Generate a reply to a conversation with a Azure model, in OpenAI's Chat Completions format.

    **Sign-in.** API key only: `Authorization: Bearer <key>` or `x-api-key: <key>`. An SSO access token is not a key here.

    **Body.** An OpenAI Chat Completions request (`model`, `messages`, `stream`, `tools`, ...).

    **Response.** An OpenAI Chat Completions response. With `stream: true`, a stream of `data:` events
    ending in `data: [DONE]`.

    **Limits and things to know**

    - Azure only: a request here never goes on-prem. The account needs Azure access, or it is a 403.
    - A name that is not one of the gateway's Azure models is answered by another Azure model of the
      same kind: the stand-in an admin set, else the first that fits. The header `X-Model-Fallback` says
      which, and why.
    - If Azure limits the rate, the model an admin set as the next one to try answers instead, when one
      is set, before the client has received anything.
    - Billed by the tokens Azure reports, at the model's price, against the cloud cap (one cap for Azure
      and AWS together) and today's overall budget.
    - The gateway translates the request to Azure's Responses API and the answer back. `temperature`
      and the other sampling fields are not sent on, and an output limit under 16 tokens is raised to 16.
    - Holds one of the account's concurrent cloud-request slots until the response, a stream included,
      has been sent.
    - Not screened by the guardrail, which is for on-prem prompts.
    - A non-streamed answer, or a stream's first event, has to arrive within 280 seconds.

    **Errors**

    | Status | When |
    |---|---|
    | 403 | The account has no Azure access |
    | 429 | The cloud cap or today's budget is spent, or the account's cloud requests in flight are at their limit (`Retry-After: 5`) |
    | 502 | Azure failed or could not be reached |
    """
    return await azure_forward_chat_completions(request, user)


_RESPONSES = {**api_docs.upstream(api_docs.OPENAI_RESPONSES), **_BUSY}


@router.post("/azure/v1/responses", summary="Response on Azure (OpenAI Responses format)", responses=_RESPONSES)
@router.post(
    "/azure/responses", summary="Response on Azure (alias of /azure/v1/responses)", responses=_RESPONSES,
)
async def azure_responses(
    request: Request,
    user: User = Depends(limited_azure_access),
):
    """Generate a response with a Azure model, in OpenAI's Responses API format, passed straight through.

    **Sign-in.** API key only: `Authorization: Bearer <key>` or `x-api-key: <key>`. An SSO access token is not a key here.

    **Body.** An OpenAI Responses API request (`model`, `input`, ...). Forwarded as sent, sampling fields
    included, with `model` replaced by the deployment's name.

    **Response.** The Responses API response as Azure gave it. With `stream: true`, a stream of its typed
    events.

    **Limits and things to know**

    - Azure only: a request here never goes on-prem. The account needs Azure access, or it is a 403.
    - A name that is not one of the gateway's Azure models is answered by another Azure model of the
      same kind: the stand-in an admin set, else the first that fits. The header `X-Model-Fallback` says
      which, and why.
    - If Azure limits the rate, the model an admin set as the next one to try answers instead, when one
      is set, before the client has received anything.
    - Billed by the tokens Azure reports, at the model's price, against the cloud cap (one cap for Azure
      and AWS together) and today's overall budget.
    - Nothing is translated, so what is specific to the Responses API works as Azure offers it
      (`previous_response_id`, `store`, ...). For a client that speaks Chat Completions use
      `/azure/v1/chat/completions`; for one that speaks Anthropic's Messages, `/azure/v1/messages`.
    - Holds one of the account's concurrent cloud-request slots until the response, a stream included,
      has been sent.
    - Not screened by the guardrail, which is for on-prem prompts.

    **Errors**

    | Status | When |
    |---|---|
    | 403 | The account has no Azure access |
    | 429 | The cloud cap or today's budget is spent, the account's cloud requests in flight are at their limit, or Azure limits the rate and no other model could take the request |
    | 502 | Azure failed or could not be reached |
    """
    return await azure_forward_responses(request, user)


_MESSAGES = {**api_docs.upstream(api_docs.ANTHROPIC_MESSAGES), **_REMINDER, **_BUSY}


@router.post("/azure/v1/messages", summary="Message on Azure (Anthropic format)", responses=_MESSAGES)
@router.post(
    "/azure/messages", summary="Message on Azure (alias of /azure/v1/messages)", responses=_MESSAGES,
)
async def azure_messages(
    request: Request,
    user: User = Depends(limited_azure_access),
):
    """Generate a reply to a conversation with a Azure model, in Anthropic's Messages format.

    **Sign-in.** API key only: `Authorization: Bearer <key>` or `x-api-key: <key>`. An SSO access token is not a key here.

    **Body.** An Anthropic Messages request (`model`, `max_tokens`, `messages`, `system`, `tools`,
    `stream`, ...).

    **Response.** An Anthropic Messages response. With `stream: true`, Anthropic's event stream
    (`message_start` ... `message_stop`).

    **Limits and things to know**

    - Azure only: a request here never goes on-prem. The account needs Azure access, or it is a 403.
    - A name that is not one of the gateway's Azure models is answered by another Azure model of the
      same kind: the stand-in an admin set, else the first that fits. The header `X-Model-Fallback` says
      which, and why.
    - If Azure limits the rate, the model an admin set as the next one to try answers instead, when one
      is set, before the client has received anything.
    - Billed by the tokens Azure reports, at the model's price, against the cloud cap (one cap for Azure
      and AWS together) and today's overall budget.
    - The gateway translates the request to Azure's Responses API and the answer back, so a field Azure
      has no counterpart for is dropped. `temperature` and the other sampling fields are not sent on.
    - Holds one of the account's concurrent cloud-request slots until the response, a stream included,
      has been sent.
    - Not screened by the guardrail, which is for on-prem prompts.
    - While the model is silent the stream carries a `ping` event every 10 seconds. A stream that ends
      without a stop reason is closed with an `overloaded_error` event, which Claude Code retries.
    - Claude Code's version and setup are read from these requests for the account's notices.

    **Errors**

    | Status | When |
    |---|---|
    | 400 | A reminder that this client needs a setting changed, on the day's first such request, when an admin set that check to remind |
    | 403 | The account has no Azure access |
    | 429 | The cloud cap or today's budget is spent, or the account's cloud requests in flight are at their limit (`Retry-After: 5`) |
    | 502 | Azure failed or could not be reached |
    """
    reminder = await client_rules.observe_request(request, user)
    if reminder is not None:
        return reminder
    return await azure_forward_messages(request, user)


@router.post(
    "/azure/v1/messages/count_tokens", summary="Estimate a message's input tokens on Azure",
    responses=api_docs.answers(api_docs.TokenCount),
)
@router.post(
    "/azure/messages/count_tokens", summary="Estimate input tokens (alias of /azure/v1/messages/count_tokens)",
    responses=api_docs.answers(api_docs.TokenCount),
)
async def azure_count_tokens(
    request: Request,
    user: User = Depends(require_azure_access),
):
    """Roughly how many input tokens a Messages request would be on a Azure model, without sending it.

    **Sign-in.** API key only: `Authorization: Bearer <key>` or `x-api-key: <key>`. An SSO access token is not a key here.

    **Body.** The Anthropic Messages request to count (`model`, `messages`, `system`, `tools`, ...).

    **Response**

    | Key | Type | Meaning |
    |---|---|---|
    | `input_tokens` | integer | The estimate |

    **Limits and things to know**

    - An estimate of one token per four characters: Azure offers no token counter. Good for watching a
      context window fill, not for a bill.
    - Not billed, and it holds no concurrent-request slot, but it passes the budget checks: 429 once
      today's budget or the cloud cap is spent.

    **Errors.** Only the group's (400, 401, 403, 429).
    """
    return await azure_forward_count_tokens(request, user)
