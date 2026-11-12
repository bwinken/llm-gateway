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

Windows users can get a pre-configured Claude Code installer from the **Setup** page of the web UI (a site whose users are not on Windows may have turned that page off).

## Other endpoints

| Endpoint | Purpose |
|---|---|
| `GET /v1/models` | Models your account can use |
| `GET /v1/me`, `/me` | Your access (on-prem / Azure / AWS) and today's budget: limit, spent, remaining, reset time. Answers even when the budget is spent. See [Checking your quota](#checking-your-quota) |
| `GET /v1/status` | The models you can use and whether each is up, with load and price. See [Model status](#model-status) |
| `GET /v1/usage` | Your usage per day, and today and this month by model. See [Usage history](#usage-history) |
| `GET /v1/apps` | The app accounts you own, with their limits and this month's usage. See [App accounts](#app-accounts) |
| `GET /v1/notices` | What the dashboard asks you to fix in your client's settings. See [Notices](#notices) |
| `POST /auth/api-key` | Your API key for an AuthCenter access token: how a terminal tool signs in. See [Signing in from a terminal tool](#signing-in-from-a-terminal-tool) |
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
  "legacy_key": false,
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
- `is_admin` is always `false`: an API key carries no admin right (administrators are recognized by their sign-in on the web UI). The field stays for clients built against it.
- `legacy_key: true` means your key is of an older, weaker format; regenerate it on the dashboard when convenient. The key itself is never returned by any endpoint.
- `concurrency.limit` is how many requests your key may have in flight at once. With `"mode": "off"` there is no limit and `limit` is left out.

## Model status

```bash
curl https://your-gateway/v1/status -H "Authorization: Bearer sk-your-api-key"
```

```json
{
  "object": "status",
  "onprem": {"total": 3, "up": 2, "down": ["glm-4.6"]},
  "models": [
    {"id": "qwen3-coder", "backend": "onprem", "type": "llm", "state": "up",
     "running": 2, "waiting": 0, "display_name": "Qwen3 Coder", "context_window": 131072,
     "input_price_per_1m": 0.5, "output_price_per_1m": 1.5},
    {"id": "glm-4.6", "backend": "onprem", "type": "llm", "state": "down",
     "input_price_per_1m": 0.5, "output_price_per_1m": 1.5},
    {"id": "bge-m3", "backend": "onprem", "type": "embedding", "state": "up",
     "input_price_per_1m": 0.02, "output_price_per_1m": 0.0},
    {"id": "gpt-5", "backend": "azure", "type": "llm", "state": "unchecked",
     "context_window": 400000, "input_price_per_1m": 1.25, "output_price_per_1m": 10.0}
  ]
}
```

- `models` lists every model your key can see, of every type (`/v1/models` lists only the chat models): on-prem first, then the cloud backends you have access to, in the order the dashboard shows them.
- `backend` is `onprem`, `azure` or `aws`, the same names as in `/v1/me`.
- `state` is `up` or `down` for on-prem models, as the gateway's health check last saw them, and `unchecked` for cloud models, which the gateway does not probe.
- `running` / `waiting` are the requests an on-prem server is working on and has queued. They are present only when the server reports them.
- `input_price_per_1m` / `output_price_per_1m` are what a request is billed at, in USD per 1M tokens.
- `display_name`, `context_window`, `max_output_tokens` and the capability flags (`supports_tools`, `supports_vision`, `supports_prompt_caching`, `is_reasoning`) appear when the administrator declared them, as in `/v1/models`. A field is never `null`: when there is no value, the key is absent.
- `onprem` sums the on-prem models up: how many, how many are up, and the names of those that are down.
- A request for a model that is down is answered by another model of its type when one is up (the response then carries `X-Model-Fallback`).

## Usage history

```bash
curl "https://your-gateway/v1/usage?days=3" -H "Authorization: Bearer sk-your-api-key"
```

```json
{
  "object": "usage",
  "timezone": "Asia/Taipei",
  "days": [
    {"date": "2026-10-11", "requests": 0,  "input_tokens": 0,      "output_tokens": 0,     "cost_usd": 0.0},
    {"date": "2026-10-12", "requests": 31, "input_tokens": 98000,  "output_tokens": 12500, "cost_usd": 4.1},
    {"date": "2026-10-13", "requests": 42, "input_tokens": 120000, "output_tokens": 18000, "cost_usd": 3.25}
  ],
  "today": {
    "date": "2026-10-13",
    "requests": 42, "input_tokens": 120000, "output_tokens": 18000, "cost_usd": 3.25,
    "models": [
      {"model": "qwen3-coder", "backend": "onprem", "type": "llm",
       "requests": 30, "input_tokens": 90000, "output_tokens": 13000, "cost_usd": 1.25},
      {"model": "gpt-5", "backend": "azure", "type": "llm",
       "requests": 12, "input_tokens": 30000, "output_tokens": 5000, "cost_usd": 2.0}
    ]
  },
  "month": {
    "month": "2026-10",
    "requests": 412, "input_tokens": 1310000, "output_tokens": 164000, "cost_usd": 42.1,
    "models": [
      {"model": "qwen3-coder", "backend": "onprem", "type": "llm",
       "requests": 380, "input_tokens": 1200000, "output_tokens": 150000, "cost_usd": 30.0},
      {"model": "gpt-5", "backend": "azure", "type": "llm",
       "requests": 32, "input_tokens": 110000, "output_tokens": 14000, "cost_usd": 12.1}
    ]
  }
}
```

- `days` has one entry per `Asia/Taipei` calendar day, oldest first. The last one is today and still running. Days without usage are present as zeros. `?days=` is how many to return: 1 to 90, default 7.
- `today` and `month` are today and the current month so far, each with one `models` entry per model and backend, in the dashboard's order.
- `today` and `month` leave out a cloud backend you no longer have access to, as the dashboard does; `days` counts everything, as your daily budget does.

## App accounts

```bash
curl https://your-gateway/v1/apps -H "Authorization: Bearer sk-your-api-key"
```

```json
{
  "object": "apps",
  "month": "2026-10",
  "apps": [
    {"username": "app_batch", "daily_limit_usd": 50.0, "legacy_key": false,
     "month": {"requests": 1200, "input_tokens": 4100000, "output_tokens": 380000, "cost_usd": 61.5}}
  ]
}
```

- `apps` lists the app accounts you own (empty when you own none), each with its own daily limit and its usage this month.
- An app's API key is not included, and no endpoint returns a key. See or regenerate an app's key on the dashboard.

## Notices

```bash
curl https://your-gateway/v1/notices -H "Authorization: Bearer sk-your-api-key"
```

```json
{
  "object": "notices",
  "window_days": 15,
  "notices": [
    {"id": "CC001", "title": "Auto mode needs a setting on this gateway",
     "message": "Claude Code is waiting for a server-side check this gateway does not provide.",
     "last_seen_at": "2026-10-13T09:12:00+08:00", "client_version": "2.1.280",
     "patch": {"set": {"CLAUDE_CODE_AUTO_MODE_SERVER": "0", "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"},
               "unset": []}},
    {"id": "CC003", "title": "Update Claude Code",
     "message": "Your Claude Code is older than this gateway supports.",
     "last_seen_at": "2026-10-12T17:40:00+08:00", "client_version": "2.1.280"}
  ],
  "patch": {
    "set": {"CLAUDE_CODE_AUTO_MODE_SERVER": "0", "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"},
    "unset": [],
    "covers": ["CC001"],
    "manual": ["CC003"]
  }
}
```

- `notices` is what the dashboard's notices panel shows you: the problems the gateway saw in your client's requests during the last `window_days` days that you have not reported as fixed. It is empty when there is nothing to do.
- A notice's `patch` is its fix, when the fix is a setting: variables to `set` in the `env` block of `~/.claude/settings.json` and variables to remove from it (`unset`). A notice without `patch` needs something else (update Claude Code, ask for access).
- The top-level `patch` is the one to apply: everything open, merged, with the settings every account should have. `covers` lists the notices it fixes, `manual` those it cannot. It is absent when nothing is open. A patch never holds a key.
- This endpoint only reads. After applying a fix, press *I've applied it* on the dashboard; the notice also goes away by itself once the gateway has not seen the problem for `window_days` days.

`/v1/me`, `/v1/status`, `/v1/usage`, `/v1/apps` and `/v1/notices` are what a status line, a terminal tool or a script should read: together they carry what the dashboard shows. All of them answer when your budget is spent, none is billed, and none returns an API key: a key is only ever handed out for a sign-in (next section, or the dashboard).

## Signing in from a terminal tool

A program that is not a browser (a terminal tool, a TUI) can get your API key without you copying it from the dashboard:

1. It signs you in with AuthCenter, using AuthCenter's own flow for command-line programs, and receives an access token for the gateway: the same kind of token the web UI gets when you sign in there.
2. It trades that token for your API key, once:

```bash
curl -X POST https://your-gateway/auth/api-key -H "Authorization: Bearer $AUTHCENTER_ACCESS_TOKEN"
```

```json
{"object": "api_key", "username": "alice", "api_key": "sk-your-api-key", "legacy_key": false}
```

3. From then on it uses the API key, like every other client: for the models, and for `/v1/me`, `/v1/status`, `/v1/usage`, `/v1/apps` and `/v1/notices`. The access token is not needed again and nothing else accepts it; throw it away.

- You always get your existing key. Signing in on a second machine does not change the key on the first.
- A first sign-in creates your account, as a first visit to the dashboard does.
- If the API later answers 401, the key was regenerated: sign in and trade again.
- `401`: the token is missing, expired or not AuthCenter's. `403`: the token has no `read` scope, the account is disabled (and the token has no `admin` scope), or your address is not allowed to use the API (the message says which). `429`: too many bad tokens or keys came from your address; wait for `Retry-After`.
- Every key handed out this way is in the gateway's audit log.

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
