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
| `GET /v1/service-accounts` | The service accounts you own, with their limits and this month's usage. See [Service accounts](#service-accounts) |
| `GET /v1/notices` | What the dashboard asks you to fix in your client's settings. See [Notices](#notices) |
| `POST /v1/notices/resolved` | Tell the gateway you applied a fix, for all notices or the ones you name. See [Notices](#notices) |
| `GET /v1/settings` | The client settings a tool can set up for you, each with a description, the suggested value and the other options for your account. See [Settings](#settings) |
| `POST /v1/events` | A setup tool reports what it installed, updated or removed for you. See [Install events](#install-events) |
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

## Naming your application

The gateway records which program each request came from, so the people running it can see what is in use. A tool is recognised by what it sends anyway (Claude Code, Roo Code, the OpenAI and Anthropic SDKs, `curl`, ...). A program you wrote looks like the SDK it uses, `openai-python` for example, unless it says its name in an `X-Title` header:

```python
client = OpenAI(
    base_url="https://your-gateway/v1",
    api_key="sk-your-api-key",
    default_headers={"X-Title": "invoice-bot"},
)
```

The Anthropic SDK takes `default_headers` the same way; an application built on Claude Code or the Claude Agent SDK sets `ANTHROPIC_CUSTOM_HEADERS="X-Title: invoice-bot"`. Keep the name short and stable: it is stored in lowercase, anything other than letters, digits, `.` and `-` becomes `-`, and it is cut at 40 characters. The header is optional and only labels the request; it changes nothing about access, limits or billing. A program that runs unattended should still have its own [service account](#service-accounts).

## Checking your quota

```bash
curl https://your-gateway/v1/me -H "Authorization: Bearer sk-your-api-key"
```

```json
{
  "object": "account",
  "username": "alice",
  "display_name": "Alice",
  "legacy_key": false,
  "org_code": "RD1",
  "api_key": {"masked": "sk-xxxxxxxx…xxxx", "created_at": "2026-09-02T10:15:00+08:00"},
  "site": {
    "title": "LLM Gateway", "api_base": "https://your-gateway/v1",
    "links": {"docs": "https://wiki.example/llm-gateway"},
    "setup_page": true, "installer": true
  },
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
- `is_admin` is not in the answer for an API key: a key carries no admin right and cannot say whether its holder is an administrator. It is there, `true` or `false`, only when you call with your SSO access token (below). Read it as absent = not known.
- `legacy_key: true` means your key is of an older, weaker format; regenerate it on the dashboard when convenient. The key itself is never returned by any endpoint.
- `api_key.masked` is enough to recognise your key (its start and its last four characters), not to use it; `created_at` is when it was issued. `org_code` is your department, absent when your sign-in gave none. `site` is what a tool shows around the figures: the site's `title`, the `api_base` to print in examples, the administrator's `links` (only the ones that are set) and whether the Setup page and the Windows installer are available.
- A program can also call these account endpoints (`/v1/me`, `/v1/status`, `/v1/usage`, `/v1/service-accounts`, `/v1/notices`, `/v1/settings`) with the SSO access token it signed in with, instead of the key: `Authorization: Bearer <access token>`. The answer is the same, with two additions: `is_admin` (`true` or `false`), and in `/v1/service-accounts` who each application was used for (`sent_for`). The token expires and the key does not, so most tools trade the token for the key once (*Signing in from a terminal tool*) and use the key. The model API (chat, messages, embeddings) takes the key only.
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
    ],
    "sources": [
      {"source": "own_key",
       "requests": 372, "input_tokens": 1190000, "output_tokens": 148000, "cost_usd": 38.6,
       "models": [
         {"model": "qwen3-coder", "backend": "onprem", "type": "llm",
          "requests": 340, "input_tokens": 1080000, "output_tokens": 134000, "cost_usd": 26.5},
         {"model": "gpt-5", "backend": "azure", "type": "llm",
          "requests": 32, "input_tokens": 110000, "output_tokens": 14000, "cost_usd": 12.1}
       ]},
      {"source": "service_account", "service_account": "hr-chat", "description": "HR chat page",
       "requests": 40, "input_tokens": 120000, "output_tokens": 16000, "cost_usd": 3.5,
       "models": [
         {"model": "qwen3-coder", "backend": "onprem", "type": "llm",
          "requests": 40, "input_tokens": 120000, "output_tokens": 16000, "cost_usd": 3.5}
       ]}
    ]
  }
}
```

- `days` has one entry per `Asia/Taipei` calendar day, oldest first. The last one is today and still running. Days without usage are present as zeros. `?days=` is how many to return: 1 to 90, default 7.
- `today` and `month` are today and the current month so far, each with one `models` entry per model and backend, in the dashboard's order.
- `today` and `month` leave out a cloud backend you no longer have access to, as the dashboard does; `days` counts everything, as your daily budget does.
- `sources`, in `today` and in `month` (the example shows the month's), is that same usage by the way it came in: the dashboard's *Usage Flow* card. `own_key` is what you sent with your own API key; each `service_account` entry is an application you signed in to that sent requests for you ([below](#an-application-with-users-of-its-own-x-on-behalf-of)), named in `service_account`, with `description` when an administrator wrote one. Each has its totals and its own `models` rows. They are part of the period's figures, not on top of them, and add up to them. An entry without usage is absent, so the list is empty when nothing was used. More `source` values may be added later: skip one you do not know.
- A model row may carry `"overflow": true`: you asked for an on-prem model and the gateway served it from this cloud model while on-prem was congested.

## Service accounts

```bash
curl https://your-gateway/v1/service-accounts -H "Authorization: Bearer sk-your-api-key"
```

```json
{
  "object": "service_accounts",
  "month": "2026-10",
  "service_accounts": [
    {"id": 41, "username": "billing-etl", "description": "Nightly billing export", "department": "FIN2",
     "daily_limit_usd": 50.0, "legacy_key": false, "api_key_masked": "sk-xxxxxxxx…xxxx",
     "month": {"requests": 1200, "input_tokens": 4100000, "output_tokens": 380000, "cost_usd": 61.5,
               "models": [
                 {"model": "qwen3-coder", "backend": "onprem", "type": "llm",
                  "requests": 1100, "input_tokens": 3700000, "output_tokens": 340000, "cost_usd": 21.5},
                 {"model": "gpt-5", "backend": "azure", "type": "llm",
                  "requests": 100, "input_tokens": 400000, "output_tokens": 40000, "cost_usd": 40.0}
               ]}}
  ],
  "withheld": [{"username": "hr-chat", "department": "HR1"}]
}
```

- A service account is an account for a program (a batch job, a service) that an administrator assigned to you. They used to be called app accounts: older ones are named `app_...`, newer ones anything.
- `service_accounts` lists the ones you own (empty when you own none), each with its own daily limit and its usage this month: the dashboard's *Service accounts* tab.
- `description` is one line an administrator wrote to say what the account is, and `department` is the department it belongs to. Each is absent when there is none.
- An account that belongs to another department than yours is not listed, here or on the dashboard, even though it is still assigned to you: `withheld` names it with its department (an empty list when there is none), as the dashboard's *Service accounts* tab does. Ask an administrator if that is wrong.
- `month.models` is that month by model: one row per model and backend, in the dashboard's order, adding up to the month's totals. Every backend the service account used is listed, whatever your own access.
- A service account's API key is not included, and no endpoint returns a key: `api_key_masked` is enough to recognise it, not to use it. See or regenerate it on the dashboard's *Service accounts* tab. `id` is the account's number there.
- Called with your SSO access token instead of your key, each account that sent requests for its users this month also has `sent_for` (how many `users`, and one row per user and backend with requests, tokens and cost; the ten users with the highest cost by name, the rest as one row per backend with `others`), and the answer has `top_users`. A key is not told this: a leaked key must not say who uses an application.
- `acts_for_users` and `on_behalf` are about an application that sends requests for its users, below: whether an administrator turned that on for the account, and what it sent for them this month (for how many `users`). That usage is billed to those users, so it is no part of `month`.

### An application with users of its own (`X-On-Behalf-Of`)

An application people sign in to (a chat page, an internal tool) holds one service account key. So that the gateway knows who each request is for, and charges that person instead of the application, it names the user in a header:

```bash
curl https://your-gateway/v1/chat/completions \
  -H "Authorization: Bearer $SERVICE_ACCOUNT_KEY" \
  -H "X-On-Behalf-Of: alice" \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen3-coder", "messages": [{"role": "user", "content": "Hello"}]}'
```

- **It only works once an administrator turned on *On behalf of users* for that service account.** Until then the header is ignored and the request is the service account's own. The dashboard's *Service accounts* tab says which it is, with a test to run.
- The value is the user's gateway username (their sign-in name), one per request. Not an email address or a display name.
- The request is then that user's: their daily limit and cloud cap, their cloud access, their bill and their dashboard. Nothing is charged to the service account.
- With it on, every model request needs the header. `GET /v1/models` (or `/models`) with it lists the models that user may use, the same list they get with their own key.
- The header is for model requests only. The account endpoints (`/v1/me`, `/v1/status`, `/v1/usage`, ...) answer about the account the key belongs to: without the header they give the service account's own (a way to check the key works), with it they answer 403. An application cannot read a user's budget or anything else about them; the user sees that on their dashboard or with their own key, and a request for a user who is over their limit comes back as 429.
- You may label a request with entries of your own, to look it up by later (which feature or module sent it, a ticket number): `-H "X-Gateway-Tags: module=invoice-ocr,ticket=4821"`. Comma-separated `key=value`, up to 16; a key is letters, digits and `_` (40 at most), a value up to 200 characters, percent-encoded when it is not plain ASCII (`team=%E8%B2%A1%E5%8B%99`). A malformed header is a 400. The labels are recorded with the request only: nothing is decided by them and they are not passed on to the model. With an SDK, send both headers through `default_headers` or the per-request `extra_headers`.
- Refused: no header or more than one name (400); a username that has no account on the gateway, a disabled account or a service account (403; a person gets an account by opening the dashboard once); the user over their limit (429).
- Your application is what vouches for the user: sign them in before you send their name, send it from your server, and keep the key there. Whoever holds the key can spend any user's budget.

## Notices

```bash
curl https://your-gateway/v1/notices -H "Authorization: Bearer sk-your-api-key"
```

```json
{
  "object": "notices",
  "window_days": 15,
  "quiet_days": 14,
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
  },
  "model_changes": {
    "changes": [
      {"id": "MC3", "title": "qwen-coder is being replaced by qwen-coder-2",
       "message": "The old model is switched off on the 30th.",
       "models": {"qwen-coder": "qwen-coder-2"}, "applied": false}
    ],
    "retiring": ["qwen-coder"],
    "patch": {"models": {"qwen-coder": "qwen-coder-2"}}
  }
}
```

- `notices` is what the dashboard's notices panel shows you: the problems the gateway saw in your client's requests during the last `window_days` days that you have not reported as fixed. It is empty when there is nothing to do.
- A notice's `patch` is its fix, when the fix is a setting: variables to `set` in the `env` block of `~/.claude/settings.json` and variables to remove from it (`unset`). A notice without `patch` needs something else (update Claude Code, ask for access).
- The top-level `patch` is the one to apply: everything open, merged, with the settings every account should have. `covers` lists the notices it fixes, `manual` those it cannot. It is absent when nothing is open. A patch never holds a key.
- `model_changes` is a second kind of notice, in a block of its own: the models an admin is replacing, for your account. `retiring` lists the model names that are going away and `patch.models` what to rename each to; `changes` gives the announcements one by one (`applied` is whether you pressed *I've switched* on the dashboard). To apply it, replace the old name with the new one where `~/.claude/settings.json` names a model (the `model` setting and the `env` variables with `MODEL` in their name), only where the value is exactly the old name, keeping a bracket suffix (`old[1m]`), and add nothing. All three are empty when no model is being replaced. It is not a problem with your client: nothing of it is in `notices` or in the top-level `patch`, and `POST /v1/notices/resolved` does not touch it.
- After applying a fix, say so: press *I've applied it* on the dashboard, or from a program:

```bash
# everything that is open
curl -X POST https://your-gateway/v1/notices/resolved -H "Authorization: Bearer sk-your-api-key"

# only some
curl -X POST https://your-gateway/v1/notices/resolved -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" -d '{"ids": ["CC001"]}'
```

```json
{"object": "notices.resolved", "resolved": ["CC001"]}
```

- `resolved` lists the notices that were open and are now cleared. An id with nothing open is left out and a second call answers an empty list; neither is an error. `400`: `ids` is not a list of ids.
- The gateway cannot see your settings file, so it takes your word. It leaves you alone for at least two weeks after that (`quiet_days` in `GET /v1/notices`); if it still sees the same problem then (a second machine, a patch that did not take), the notice comes back. A notice also goes away by itself once the gateway has not seen the problem for `window_days` days.
- These calls are in the gateway's audit log, like the dashboard's button.

## Settings

```bash
curl https://your-gateway/v1/settings -H "Authorization: Bearer sk-your-api-key"
```

```json
{
  "object": "settings",
  "client": "claude-code",
  "fields": [
    {"name": "ANTHROPIC_MODEL", "label": "Default model",
     "description": "The model Claude Code starts a session with.",
     "type": "model", "default": "qwen3-coder",
     "options": [
       {"value": "qwen3-coder", "label": "Qwen3 Coder", "backend": "onprem"},
       {"value": "qwen3-small", "label": "qwen3-small", "backend": "onprem"},
       {"value": "gpt-5", "label": "GPT-5", "backend": "azure"}
     ]},
    {"name": "CLAUDE_CODE_AUTO_MODE_SERVER", "label": "Auto mode safety check",
     "description": "No model behind this gateway reviews auto mode's actions, so Claude Code has to run that check itself.",
     "type": "choice", "default": "0",
     "options": [{"value": "0", "label": "Run by Claude Code"}]},
    {"name": "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY", "label": "Model list from the gateway",
     "description": "Fill Claude Code's /model picker with this gateway's models at startup.",
     "type": "choice", "default": "1",
     "options": [{"value": "1", "label": "On"}, {"value": "0", "label": "Off"}]}
  ]
}
```

- This is for a tool that sets Claude Code up for you: it can apply every `default` as it is, or let you pick. Each field is one variable of the `env` block of `~/.claude/settings.json` (`client` names the client the fields are for). Your gateway's administrator decides which fields there are.
- `description` says what the variable is for. It and `label` are always there.
- `default` is the suggested value for **your** account. It is never empty and is always the `value` of one of the `options`, so it can be applied or preselected.
- `options` is everything you may pick. For a `model` field they are chat models your account can use, each with its `backend`; for a `choice` field they are fixed values. An option may have its own `description`. A field with a single option has nothing else to pick: the only question is whether to apply it.
- Both follow your tier, which is your cloud access (none, Azure, AWS, or both): another account may get a different default, different options, or a field you do not have. A model you cannot use is never offered.
- The gateway does not know what you have set now: compare with your own settings.json.
- The fields change with the gateway's configuration and your access, so read them when you need them rather than keeping a copy. Never a key, and read-only: applying a value is up to the tool.
- Settings and [notices](#notices) are two answers to two questions. `/v1/settings` is what to set when a client is set up; a notice's `patch` is the least that fixes a problem the gateway saw. To put everything at the recommended values, apply the defaults and then the top-level notices `patch` on top; to fix a problem without touching what the user picked, apply the `patch` alone. The gateway does not merge them, because it does not know which values the user chose.

## Install events

A tool that installs or configures things for you (a terminal tool, a desktop app) can tell the gateway what it did, so your administrators can see which tools are in use, in which versions and on which operating systems. One event per request:

```bash
curl -X POST https://your-gateway/v1/events -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" -d '{
    "event": "installed", "tool": "gateway-tui", "tool_version": "0.3.1",
    "component": "claude-code", "component_version": "2.1.310", "method": "npm",
    "os": "windows", "os_version": "10.0.22631", "arch": "x64", "hostname": "pc-0421"
  }'
```

```json
{"object": "event", "recorded": true}
```

| Field | | What it is |
|---|---|---|
| `event` | required | `installed`, `updated`, `uninstalled` or `settings_applied` |
| `ok` | optional | `false` when the attempt failed (default `true`). A failed install is `"event": "installed", "ok": false` |
| `tool`, `tool_version` | required | The program reporting |
| `component` | required | What the event is about, by its own name: `claude-code`, `codex`, `vscode`, `claude-desktop`, `@modelcontextprotocol/server-github`, `anthropic-skills:pdf` |
| `kind` | optional | What sort of thing that is: `app` (a program of its own; the default), `skill`, `mcp` or `plugin` |
| `client` | optional | For a `skill`, `mcp` or `plugin`: the program it was added to, such as `claude-code` |
| `component_version`, `previous_version` | optional | The version now on the machine, and for an update the one before |
| `method` | optional | How it was installed: `npm`, `native`, `offline` |
| `os` | required | `windows`, `linux` or `macos` |
| `os_version`, `arch` | optional | Which one: `10.0.22631`, `Ubuntu 22.04 (WSL2)`; `x64`, `arm64` |
| `hostname` | optional | The machine's name. Send it: without one, your machines of the same OS count as one |
| `message` | optional | Why it failed, or a note (500 characters are kept) |

A skill, an MCP server or a plugin says what it was added to:

```json
{"event": "installed", "tool": "gateway-tui", "tool_version": "0.3.1",
 "kind": "mcp", "component": "@modelcontextprotocol/server-github", "component_version": "1.2.0",
 "client": "claude-desktop", "os": "windows", "hostname": "pc-0421"}
```

- Names (`tool`, `client`, `method`, `arch`) are letters, digits, `.`, `_` and `-`, 40 characters at most; `hostname` the same characters, 100 at most; `component` also takes `@`, `/`, `:` and `+`, 100 at most. Versions are 40 characters without spaces. Names are stored in lowercase.
- The account is your key's. Fields the gateway does not know are ignored, so a newer tool can send more than an older gateway keeps.
- `400`: a field does not validate (`detail` says which). `429`: more than 300 events in an hour from your account. `503`: it could not be stored.
- This is a report and nothing else: it changes nothing about your account, and a tool should carry on whatever the answer. It does not clear a notice; that is `POST /v1/notices/resolved`.
- Never put a key in an event. Your own key is taken out of `message` and `os_version`, and an event with it anywhere else is refused.

`/v1/me`, `/v1/status`, `/v1/usage`, `/v1/service-accounts`, `/v1/notices` and `/v1/settings` are what a status line, a terminal tool or a script should read: together they carry what the dashboard shows, and what a tool needs to set a client up. `POST /v1/notices/resolved` and `POST /v1/events` are the two a tool writes with. All of them answer when your budget is spent, none is billed, and none returns an API key: a key is only ever handed out for a sign-in (next section, or the dashboard).

## API versions

A program that is released apart from the gateway (a frontend, a terminal tool) asks this first, before it signs in. No key:

```bash
curl https://your-gateway/v1/version
```

```json
{
  "object": "version",
  "gateway": "1.0.0",
  "stable": "v1",
  "statuses": ["stable", "deprecated", "nightly"],
  "versions": [{"version": "v1", "status": "stable"}]
}
```

Look your own version up in `versions`:

- `stable`: served, and changed by addition only. `stable` at the top names it: the version to build against.
- `deprecated`: still served until `sunset` (a date, the last day); move to `successor`. Tell your user to update.
- `nightly`: served for trying out. It may change or go without notice, so release nothing built against it.
- Not in the list: this gateway does not serve it. Stop and say so, rather than call an API that is gone.

Treat a status you do not know like `nightly`. `gateway` is the gateway's own release number, for a bug report: never decide from it what is supported. Today `v1` is everything described in this guide.

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

3. From then on it uses the API key, like every other client: for the models, and for `/v1/me`, `/v1/status`, `/v1/usage`, `/v1/service-accounts`, `/v1/notices`, `/v1/settings` and `/v1/events`. The access token is not needed again and nothing else accepts it; throw it away.

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
