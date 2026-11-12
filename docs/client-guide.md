# Client Guide

[中文版](client-guide.zh-TW.md) · [Back to README](../README.md)

How to point clients at the gateway: base URLs, SDK examples, per-client tool-calling setup, and the responses you should be ready to handle.

Every request authenticates with your gateway API key, sent as either `Authorization: Bearer <key>` or `x-api-key: <key>`. Get the key from the web dashboard.

Use `https://`. A request to `http://` is redirected (308), but some clients drop the API key or the request body on a redirect, so set the base URL to `https://` rather than relying on it.

## Base URLs

| Base URL | Serves | Models listed |
|---|---|---|
| `https://your-gateway/v1` | Everything: on-prem vLLM, plus Azure / AWS Bedrock aliases you have access to | vLLM models, plus Azure and Bedrock models if your account is granted them |
| `https://your-gateway/azure/v1` | Azure OpenAI only | Azure models only |
| `https://your-gateway/aws/v1` | AWS Bedrock only | Bedrock models only |

**Use `/v1` unless you have a reason not to.** You pick the backend by picking the model name, and one base URL covers all of them. Use `/azure/v1` or `/aws/v1` only for a client that must never reach anything else.

Every route also works without the `/v1` prefix (`/chat/completions`, `/messages`, …), so a client whose base URL omits `/v1` still works.

An unknown model name is not an error on any surface: the request is served by that surface's default model, and the response carries an `X-Model-Fallback` header saying so.

## OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="https://your-gateway/v1", api_key="sk-your-api-key")

resp = client.chat.completions.create(
    model="my-model",              # any alias from GET /v1/models
    messages=[{"role": "user", "content": "Hello!"}],
)

emb = client.embeddings.create(model="bge-m3", input=["The quick brown fox"])
```

## Anthropic SDK and Claude Code

```python
from anthropic import Anthropic

client = Anthropic(base_url="https://your-gateway", api_key="sk-your-api-key")  # gateway key, not Anthropic's
resp = client.messages.create(
    model="my-model",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}],
)
```

```bash
ANTHROPIC_BASE_URL=https://your-gateway \
ANTHROPIC_AUTH_TOKEN=sk-your-api-key \
claude
```

`/v1/messages` works against every backend: requests to vLLM models are translated to OpenAI chat completions, Azure models go through the Responses API, and Bedrock models through Converse. Streaming, tool use, images, and reasoning (`thinking` blocks) work on all of them. During long silences the gateway sends a `ping` every 10 seconds so Claude Code doesn't drop the connection. A stream cut off mid-generation ends with a retryable `overloaded_error`, never a falsely complete turn.

Windows users can get a pre-configured Claude Code installer from the **Setup** page of the web UI.

## Other endpoints

| Endpoint | Purpose |
|---|---|
| `GET /v1/models` | Models your account can use |
| `GET /v1/me`, `/me` | Your access (on-prem / Azure / AWS) and today's budget: limit, spent, remaining, reset time. Answers even when the budget is spent. See [Checking your quota](#checking-your-quota) |
| `GET /v1/status` | The models you can use and whether each is up, with load and price. See [From a terminal](#from-a-terminal) |
| `GET /v1/usage` | Your usage per day (`?days=`, default 7, up to 90) and this month by model |
| `GET /v1/cli` | `gw`, a shell script that shows the three above in a terminal |
| `POST /v1/embeddings` | Embeddings (vLLM only) |
| `POST /v1/rerank`, `/v1/score` | Reranking |
| `POST /v1/messages/count_tokens` | Token count for an Anthropic request (not billed) |
| `POST /v1/tokenize` | vLLM tokenizer pass-through (not billed) |
| `POST /v1/chat/completions/render` | Show exactly what the model receives: the chat-templated prompt, without generating. Not billed. Usage is documented at `/docs`. |
| `POST /azure/v1/responses` | Raw Azure Responses API pass-through, for `previous_response_id`, `store`, etc. |
| `POST /v1/systemone` | Typed decisions; see [System One](#system-one) |

```bash
curl https://your-gateway/v1/rerank \
  -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"model": "bge-reranker-v2-m3", "query": "What is AI?", "documents": ["AI is...", "Machine learning is..."]}'
```

## Checking your quota

```bash
curl https://your-gateway/v1/me -H "Authorization: Bearer sk-your-api-key"
```

```json
{
  "object": "account",
  "username": "alice",
  "display_name": "Alice",
  "is_admin": false,
  "daily": {
    "limit_usd": 10.0, "used_usd": 3.25, "remaining_usd": 6.75,
    "requests": 42, "input_tokens": 120000, "output_tokens": 18000,
    "enforced": true, "timezone": "Asia/Taipei",
    "resets_at": "2026-10-14T00:00:00+08:00", "resets_in_seconds": 30120
  },
  "backends": {
    "onprem": {"access": true,                        "used_usd": 1.25, "remaining_usd": 6.75},
    "azure":  {"access": true,  "used_usd": 2.0,  "remaining_usd": 0.5},
    "aws":    {"access": false,                       "used_usd": 0.0,  "remaining_usd": 0.0}
  },
  "cloud": {"limit_usd": 2.5, "used_usd": 2.0, "remaining_usd": 0.5},
  "cloud_budget_fallback": false,
  "concurrency": {"mode": "enforce", "limit": 8}
}
```

- `daily` is your overall budget for today; every backend's spend counts toward it. The day is the `Asia/Taipei` calendar day.
- Every account has a daily limit; there is no "unlimited". A limit of `0` is a $0 budget: every request is refused.
- `backends.<name>.remaining_usd` is what you can still spend there today: for Azure / AWS the tighter of the cloud budget and your overall remaining budget, for on-prem the overall remaining budget; `0` without access. The remainders share one budget and don't add up: in the example, spending $0.50 on Azure leaves $6.25 for on-prem.
- `cloud` appears only when you have cloud access and a cloud budget (the admin's default, or one set for you): what you may spend on Azure and AWS together today, split between them however you like. It also bounds both cloud backends' `remaining_usd` (in the example, only $0.50 of the cloud budget is left). Without it, cloud is bounded by `daily` only. On-prem has no cap of its own.
- A request is refused only once `used_usd` reaches the limit, so the last request of the day can overshoot it slightly.
- `enforced: false` means the gateway only logs overages instead of returning 429.
- `cloud_budget_fallback: true` means a `/v1/*` request for a cloud model whose cap is spent is answered by an on-prem model instead of a 429.
- `concurrency.limit` is how many requests your key may have in flight at once. With `"mode": "off"` there is no limit and `limit` is left out.

## From a terminal

`gw` is a small shell script (it needs `curl`) that shows your budget, the models and your usage without opening the dashboard. Get it once, from the gateway:

```bash
mkdir -p ~/.local/bin
curl -fsS https://your-gateway/v1/cli -H "Authorization: Bearer sk-your-api-key" -o ~/.local/bin/gw
chmod +x ~/.local/bin/gw
```

| Command | Shows |
|---|---|
| `gw` | Today's budget, then the models |
| `gw me` | Today's budget: spent, left, when it resets, spend per backend |
| `gw models` | The models you can use: up or down, running / waiting requests, context window, price |
| `gw usage [N]` | The last N days (default 7) and this month by model |
| `gw update` | Replaces the script with the gateway's current one |

```
alice · Alice

Budget today
  total  [███████░░░░░░░░░░░░░]  33%  $3.25 of $10.00  $6.75 left
  cloud  [████████████████░░░░]  80%   $2.00 of $2.50  $0.50 left
  resets in 9h 28m (midnight, Asia/Taipei)

Spent today
  on-prem      $1.25
  Azure        $2.00
  AWS      no access
  42 requests, 120.0K tokens in, 18.0K out

Up to 8 requests at a time.
```

- **No key in the script.** `gw` uses `LLM_GATEWAY_API_KEY` when set; otherwise the key Claude Code already has (`ANTHROPIC_AUTH_TOKEN`, from the environment or `~/.claude/settings.json`), and only while `ANTHROPIC_BASE_URL` there points at this gateway. After you regenerate your key, updating Claude Code (the dashboard's *My API Key* script) updates `gw` too.
- The key is handed to `curl` on its standard input, not on the command line, so other users of a shared machine cannot read it from `ps`.
- `gw --json me|models|usage` prints the JSON instead, for scripts. `watch -n 60 --color gw --color` keeps it on screen.
- Colours are used when writing to a terminal (`--no-color` or `NO_COLOR` turns them off); outside a UTF-8 locale the bars are drawn in ASCII (`--ascii`).
- An administrator can install it for everyone instead: copy `setup/gw` from the repository to `/usr/local/bin/gw` and set `LLM_GATEWAY_URL=https://your-gateway` (the downloaded copy already knows the address).

Without the script, the same views come from `curl`: add `?format=text` (or send `Accept: text/plain`) to `/v1/me`, `/v1/status` or `/v1/usage`; `&color=1` adds colours, `&ascii=1` keeps to ASCII. Without it they answer in JSON:

```bash
curl "https://your-gateway/v1/status?format=text" -H "Authorization: Bearer sk-your-api-key"
```

- `/v1/status`: `models` lists every model your key can see, each with `backend` (`onprem` / `azure` / `aws`), `type`, the billed `input_price_per_1m` / `output_price_per_1m`, and `state`: `up` or `down` for on-prem models (with `running` / `waiting` request counts when the server reports them), `unchecked` for cloud models, which the gateway does not probe. `onprem` counts the on-prem ones (`total`, `up`, and the names that are `down`). A request for a model that is down is answered by another model of its type when one is up.
- `/v1/usage`: `days` has one entry per `Asia/Taipei` day, oldest first, today last and still running (`date`, `requests`, `input_tokens`, `output_tokens`, `cost_usd`); `month` is this month so far, with a `models` list per model and backend.
- All three answer when your budget is spent, and none of them is billed.

## Tool calling: pick the right client mode

The two paths differ in how strictly they check tool calls.

### On-prem vLLM models: lenient

The gateway passes chat completions through without checking that tool calls and tool results pair up. What matters is the **model**: structured tool calls need a model with native function calling (Qwen 2.5+, Llama 3.1+, Hermes, Mistral Large, …).

| Client | Model supports function calling | Model doesn't |
|---|---|---|
| Roo Code | **OpenAI** provider, base URL `https://your-gateway/v1` | **OpenAI Compatible** provider, same base URL |
| Cline / Continue.dev / Cursor | OpenAI provider | Most have no XML fallback; use a model with tool calling |
| Claude Code | `ANTHROPIC_BASE_URL=https://your-gateway` | Same |

### Azure models: strict

Every Azure call goes through the Responses API, which rejects (400) any `function_call` without a matching `function_call_output`. Clients that mix structured tool calls with inline text results break here. The gateway logs `Dropping N orphan function_call(s)` and degrades so the conversation can continue, but the fix is the client setting.

| Client | Setup | Endpoint |
|---|---|---|
| Claude Code / Anthropic SDK | `ANTHROPIC_BASE_URL=https://your-gateway` (or `/azure`) | `/v1/messages` |
| Roo Code, **OpenAI** provider (recommended) | Base URL `https://your-gateway/v1`, Custom Model ID = the Azure alias | `/v1/chat/completions` |
| Roo Code, Anthropic provider | Base URL pointing at the gateway | `/v1/messages` |
| Roo Code, **OpenAI Compatible** | ⚠️ **Avoid**: it mixes native tool calls with inline results | — |
| Cursor / Continue.dev / OpenAI SDK | `base_url=https://your-gateway/v1` | `/v1/chat/completions` |
| OpenAI SDK, Responses API | `client.responses.create(...)` on `/azure/v1` | `/azure/v1/responses` (sampling params are not stripped here) |

**Rule of thumb:** Anthropic-style client → `/messages`; OpenAI-style client → `/chat/completions`; and on Azure, never use a mode that mixes tool-calling styles.

## Responses to expect

| Status / header | Meaning | What to do |
|---|---|---|
| `X-Model-Fallback` header | Served by a different model than requested (unknown alias, server down, or rate-limited cloud model) | Check the model name |
| `X-Budget-Fallback` header | Your daily cloud budget is spent, so an on-prem model answered (only when the admin enabled it) | — |
| `429` "Daily spending limit … exceeded" | Today's budget is used up; `Retry-After` points to local midnight | Wait, or ask an admin to raise the limit |
| `429` "Daily spending limit is $0" | Your account has a $0 budget; waiting won't help | Ask an admin for a limit |
| `429` "Too many concurrent requests" | Your account already has the maximum number of requests in flight; `Retry-After: 5` | Retry shortly. Claude Code does this automatically |
| `403` "Azure / Bedrock access not granted" | Your account isn't enabled for that backend | Ask an admin |
| `403` "Account disabled" | Your account was disabled | Ask an admin |
| `429` "Too many failed authentication attempts from this address" | Your address sent many different invalid keys and is locked for a while (`Retry-After`); even a valid key is refused until then | Fix the key in your client, then wait |
| `502` "Downstream error (ConnectError)" (or another error type) | The model server could not be reached or failed; details are in the gateway log, not in the reply | Retry; if it persists, tell an admin the time and model |

## System One

A `systemone` model, such as [Mapika's decider](https://huggingface.co/Mapika/decider-4b), answers typed questions about a piece of text (`choice`, `score`, or yes/no `noul`) with calibrated probabilities from a single forward pass, without generating text. The gateway speaks TypeSafe's wire format at `/v1/systemone`, so the [TypeSafe Python SDK](https://docs.typesafe.ai/sdk/python/) works unchanged:

```bash
pip install typesafe-sdk
export TYPESAFE_BASE_URL=https://your-gateway     # gateway root, WITHOUT /v1 (the SDK appends /v1/systemone)
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
curl https://your-gateway/v1/systemone \
  -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"state": "I was charged twice.", "questions": {"refund": {"type": "noul", "instructions": "Does the customer ask for a refund?"}}}'
```

Billed on input tokens only.
