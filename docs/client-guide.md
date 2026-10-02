# Client Guide

[中文版](client-guide.zh-TW.md) · [Back to README](../README.md)

How to point clients at the gateway: base URLs, SDK examples, per-client tool-calling setup, and the responses you should be ready to handle.

Every request authenticates with your gateway API key, sent as either `Authorization: Bearer <key>` or `x-api-key: <key>`. Get the key from the web dashboard.

## Base URLs

| Base URL | Serves | Models listed |
|---|---|---|
| `http://your-gateway/v1` | Everything: on-prem vLLM, plus Azure / AWS Bedrock aliases you have access to | vLLM models, plus Azure and Bedrock models if your account is granted them |
| `http://your-gateway/azure/v1` | Azure OpenAI only | Azure models only |
| `http://your-gateway/aws/v1` | AWS Bedrock only | Bedrock models only |

**Use `/v1` unless you have a reason not to.** You pick the backend by picking the model name, and one base URL covers all of them. Use `/azure/v1` or `/aws/v1` only for a client that must never reach anything else.

Every route also works without the `/v1` prefix (`/chat/completions`, `/messages`, …), so a client whose base URL omits `/v1` still works.

An unknown model name is not an error on any surface: the request is served by that surface's default model, and the response carries an `X-Model-Fallback` header saying so.

## OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://your-gateway/v1", api_key="sk-your-api-key")

resp = client.chat.completions.create(
    model="my-model",              # any alias from GET /v1/models
    messages=[{"role": "user", "content": "Hello!"}],
)

emb = client.embeddings.create(model="bge-m3", input=["The quick brown fox"])
```

## Anthropic SDK and Claude Code

```python
from anthropic import Anthropic

client = Anthropic(base_url="http://your-gateway", api_key="sk-your-api-key")  # gateway key, not Anthropic's
resp = client.messages.create(
    model="my-model",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}],
)
```

```bash
ANTHROPIC_BASE_URL=http://your-gateway \
ANTHROPIC_AUTH_TOKEN=sk-your-api-key \
claude
```

`/v1/messages` works against every backend: requests to vLLM models are translated to OpenAI chat completions, Azure models go through the Responses API, and Bedrock models through Converse. Streaming, tool use, images, and reasoning (`thinking` blocks) work on all of them. During long silences the gateway sends a `ping` every 10 seconds so Claude Code doesn't drop the connection. A stream cut off mid-generation ends with a retryable `overloaded_error`, never a falsely complete turn.

Windows users can get a pre-configured Claude Code installer from the **Setup** page of the web UI.

## Other endpoints

| Endpoint | Purpose |
|---|---|
| `GET /v1/models` | Models your account can use |
| `POST /v1/embeddings` | Embeddings (vLLM only) |
| `POST /v1/rerank`, `/v1/score` | Reranking |
| `POST /v1/messages/count_tokens` | Token count for an Anthropic request (not billed) |
| `POST /v1/tokenize` | vLLM tokenizer pass-through (not billed) |
| `POST /v1/chat/completions/render` | Show exactly what the model receives: the chat-templated prompt, without generating. Not billed. Usage is documented at `/docs`. |
| `POST /azure/v1/responses` | Raw Azure Responses API pass-through, for `previous_response_id`, `store`, etc. |
| `POST /v1/systemone` | Typed decisions; see [System One](#system-one) |

```bash
curl http://your-gateway/v1/rerank \
  -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"model": "bge-reranker-v2-m3", "query": "What is AI?", "documents": ["AI is...", "Machine learning is..."]}'
```

## Tool calling: pick the right client mode

The two paths differ in how strictly they check tool calls.

### On-prem vLLM models: lenient

The gateway passes chat completions through without checking that tool calls and tool results pair up. What matters is the **model**: structured tool calls need a model with native function calling (Qwen 2.5+, Llama 3.1+, Hermes, Mistral Large, …).

| Client | Model supports function calling | Model doesn't |
|---|---|---|
| Roo Code | **OpenAI** provider, base URL `http://your-gateway/v1` | **OpenAI Compatible** provider, same base URL |
| Cline / Continue.dev / Cursor | OpenAI provider | Most have no XML fallback; use a model with tool calling |
| Claude Code | `ANTHROPIC_BASE_URL=http://your-gateway` | Same |

### Azure models: strict

Every Azure call goes through the Responses API, which rejects (400) any `function_call` without a matching `function_call_output`. Clients that mix structured tool calls with inline text results break here. The gateway logs `Dropping N orphan function_call(s)` and degrades so the conversation can continue, but the fix is the client setting.

| Client | Setup | Endpoint |
|---|---|---|
| Claude Code / Anthropic SDK | `ANTHROPIC_BASE_URL=http://your-gateway` (or `/azure`) | `/v1/messages` |
| Roo Code, **OpenAI** provider (recommended) | Base URL `http://your-gateway/v1`, Custom Model ID = the Azure alias | `/v1/chat/completions` |
| Roo Code, Anthropic provider | Base URL pointing at the gateway | `/v1/messages` |
| Roo Code, **OpenAI Compatible** | ⚠️ **Avoid**: it mixes native tool calls with inline results | — |
| Cursor / Continue.dev / OpenAI SDK | `base_url=http://your-gateway/v1` | `/v1/chat/completions` |
| OpenAI SDK, Responses API | `client.responses.create(...)` on `/azure/v1` | `/azure/v1/responses` (sampling params are not stripped here) |

**Rule of thumb:** Anthropic-style client → `/messages`; OpenAI-style client → `/chat/completions`; and on Azure, never use a mode that mixes tool-calling styles.

## Responses to expect

| Status / header | Meaning | What to do |
|---|---|---|
| `X-Model-Fallback` header | Served by a different model than requested (unknown alias, server down, or rate-limited cloud model) | Check the model name |
| `X-Budget-Fallback` header | Your Azure/Bedrock daily sub-limit is spent, so an on-prem model answered (only when the admin enabled it) | — |
| `429` "Daily spending limit … exceeded" | Today's budget is used up; `Retry-After` points to local midnight | Wait, or ask an admin to raise the limit |
| `429` "Too many concurrent requests" | Your account already has the maximum number of requests in flight; `Retry-After: 5` | Retry shortly. Claude Code does this automatically |
| `403` "Azure / Bedrock access not granted" | Your account isn't enabled for that backend | Ask an admin |
| `403` "Account disabled" | Your account was disabled | Ask an admin |

## System One

A `systemone` model, such as [Mapika's decider](https://huggingface.co/Mapika/decider-4b), answers typed questions about a piece of text (`choice`, `score`, or yes/no `noul`) with calibrated probabilities from a single forward pass, without generating text. The gateway speaks TypeSafe's wire format at `/v1/systemone`, so the [TypeSafe Python SDK](https://docs.typesafe.ai/sdk/python/) works unchanged:

```bash
pip install typesafe-sdk
export TYPESAFE_BASE_URL=http://your-gateway     # gateway root, WITHOUT /v1 (the SDK appends /v1/systemone)
export TYPESAFE_API_KEY=sk-your-api-key
export TYPESAFE_DEFAULT_MODEL=decider-4b         # a [models.systemone.*] alias
```

```python
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

with TypeSafeClient() as client:
    result = client.system_one(
        state="I was charged twice for order A-104. Please refund the duplicate.",
        questions={
            "department": Choice(instructions="Which team should handle this?",
                                 criteria={"billing": "Charges, invoices", "returns": "Exchanges, refunds", "other": None}),
            "refund_requested": Noul(instructions="Does the customer ask for a refund?"),
            "frustration": Score(instructions="How frustrated is the customer?",
                                 criteria=["calm", "frustrated", "very frustrated"]),
        },
    )
    print(result.choices["department"].choice, result.nouls["refund_requested"].noul)
```

- Without `TYPESAFE_DEFAULT_MODEL`, the SDK asks for `jev-latest`. The gateway still answers from its systemone default, but every response then carries `X-Model-Fallback` and is logged as a warning.
- `/v1/models` lists chat models only, so `client.models.list()` is not supported.
- Plain HTTP works too, and `model` may be omitted:

```bash
curl http://your-gateway/v1/systemone \
  -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"state": "I was charged twice.", "questions": {"refund": {"type": "noul", "instructions": "Does the customer ask for a refund?"}}}'
```

Billed on input tokens only.
