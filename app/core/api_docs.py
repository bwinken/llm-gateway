"""
What ``/docs`` and ``openapi.json`` say about the model API, in one place.

Every route's description follows one plan, so whoever builds against the
gateway finds the same thing in the same place whatever the route:

1. a first line that says what the route does;
2. **Sign-in**: which credential it takes;
3. **Query** / **Body**: what it takes (a table with each field's limits
   where the gateway defines the shape);
4. **Response**: what it answers (a table, one row per key, where the
   gateway defines the shape);
5. **Limits and things to know**: ranges, how fresh a figure is, what is
   billed, what is left out;
6. **Errors**: the route's own; the group's are listed once, on its router.

The model API speaks other people's wire formats (OpenAI's Chat Completions,
Responses and Embeddings, Anthropic's Messages, vLLM's score and tokenize),
and the gateway forwards a body as it came. Those shapes are not written out
again here: a copy would go stale and would be read as the gateway's own
promise. A description names the format and says what the gateway adds to
it, changes or refuses. The one shape of its own is the model list
(``ModelList``).

``MODEL_API_ERRORS`` is what any model route can answer before a model is
reached. It goes on the three routers (``v1_api``, ``azure_api``,
``aws_api``); a route adds its own with ``responses=``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field
from typing_extensions import NotRequired, TypedDict

MODEL_API_ERRORS: dict[int | str, dict] = {
    400: {"description": "A service account that sends requests for its users did not name one in "
                         "`X-On-Behalf-Of` (or named more than one), or its `X-Gateway-Tags` header is malformed."},
    401: {"description": "No API key, or one the gateway does not know. An SSO access token is not accepted "
                         "on the model API."},
    403: {"description": "The account is disabled; the key comes from an address the IP allowlist refuses; or "
                         "the user named in `X-On-Behalf-Of` has no account here, is disabled, or is a service "
                         "account."},
    429: {"description": "Today's budget is spent (`Retry-After`: seconds until midnight, Asia/Taipei); the "
                         "cloud cap is spent, for an Azure or AWS model; or this address sent too many "
                         "different invalid keys and is locked for a while."},
    502: {"description": "The model's server failed or could not be reached. `detail` names the kind of "
                         "error only, never the server."},
}

# The routes that hold a concurrency slot (chat completions, responses,
# messages) can answer one more 429.
BUSY_429 = {429: {"description": MODEL_API_ERRORS[429]["description"] + " Also: the account already has as many "
                                 "requests in flight as it may (`Retry-After: 5`)."}}

# A prompt the guardrail refuses (on-prem chat completions, responses, messages).
GUARDRAIL_400 = {400: {"description": MODEL_API_ERRORS[400]["description"] + " Also: the guardrail refused the "
                                      "prompt (header `X-Guardrail: blocked`), when an admin set it to block."}}

# The day's first request from a client that needs a setting changed (the
# Messages routes), when an admin set that check to remind.
REMINDER_400 = {400: {"description": MODEL_API_ERRORS[400]["description"] + " Also: a reminder that this client "
                                     "needs a setting changed (the day's first such request, when an admin set "
                                     "the check to remind); `GET /v1/notices` has the fix."}}

# What a 200 holds where the body is another API's.
OPENAI_CHAT = ("An OpenAI Chat Completions response, as the backend gave it. With `stream: true`, a stream of "
               "`data:` events ending in `data: [DONE]`.")
OPENAI_RESPONSES = ("An OpenAI Responses API response, as the backend gave it. With `stream: true`, a stream of "
                    "its typed events.")
ANTHROPIC_MESSAGES = ("An Anthropic Messages response. With `stream: true`, Anthropic's event stream "
                      "(`message_start` ... `message_stop`), with a `ping` every 10 seconds while the model is silent.")


def answers(shape, description: str = "The answer.") -> dict:
    """A route's 200 with its shape for ``openapi.json``. It describes the
    answer and never filters it: the handler returns what it built."""
    return {200: {"model": shape, "description": description}}


def upstream(description: str) -> dict:
    """A 200 whose body is another API's (OpenAI's, Anthropic's, vLLM's):
    described in words, with no schema of the gateway's own."""
    return {200: {"description": description}}


# ── GET /v1/models, /azure/v1/models, /aws/v1/models ──

class ModelEntry(TypedDict):
    id: Annotated[str, Field(description="The name to send as `model`.")]
    object: Literal["model"]
    owned_by: Annotated[Literal["llm-gateway", "azure-openai", "aws-bedrock"], Field(
        description="Which backend serves it: on-prem, Azure OpenAI or AWS Bedrock.",
    )]
    type: Annotated[str, Field(description="llm, vlm, embedding, reranker, ...")]
    capability: Annotated[str, Field(description="The type in words: text-generation, vision-language, ...")]
    display_name: NotRequired[str]
    context_window: NotRequired[int]
    max_output_tokens: NotRequired[int]
    supports_tools: NotRequired[bool]
    supports_vision: NotRequired[bool]
    supports_prompt_caching: NotRequired[bool]
    is_reasoning: NotRequired[bool]


class ModelList(TypedDict):
    object: Literal["list"]
    data: list[ModelEntry]


class TokenCount(TypedDict):
    input_tokens: int
