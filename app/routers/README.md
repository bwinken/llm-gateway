# Routers — API Endpoints and Page Routes

[中文版](README.zh-TW.md)

Defines FastAPI routes, split into four modules: vLLM API endpoints, Azure OpenAI API endpoints, Web UI pages, and Admin management.

## Module Overview

```mermaid
graph LR
    Client["Client App<br/>(SDK / curl)"] -->|Bearer API key| vllm_api
    Client -->|Bearer API key| azure_api
    Browser["Browser"] -->|JWT via oauth2-proxy| web_ui
    Browser -->|JWT via oauth2-proxy| admin

    subgraph routers
        vllm_api["vllm_api.py<br/>/v1/*"]
        azure_api["azure_api.py<br/>/azure/v1/*"]
        web_ui["web_ui.py<br/>/dashboard"]
        admin["admin.py<br/>/admin"]
    end

    vllm_api --> vllm_proxy["services/vllm_proxy.py"]
    azure_api --> azure_proxy["services/azure_proxy.py"]
    web_ui --> stats["services/stats.py"]
    admin --> stats
    admin --> config["core/config.py"]
    admin --> monitor["services/monitor.py"]
```

---

## `vllm_api.py` — OpenAI-Compatible API (vLLM backend)

**Auth**: `deps.get_current_user` (API key + daily limit check)

### Endpoints

| Method | Path | Description | Proxy Method | Allowed Types |
|---|---|---|---|---|
| `GET` | `/v1/models` | List available models (LLM/VLM only; Azure aliases excluded) | Direct response | `llm`, `vlm` |
| `POST` | `/v1/chat/completions` | Chat completion generation | `forward_request` | `llm`, `vlm` |
| `POST` | `/v1/responses` | Responses API | `forward_to_path` | `llm`, `vlm` |
| `POST` | `/v1/embeddings` | Text embeddings | `forward_simple_request` | `embedding`, `vision_embedding` |
| `POST` | `/v1/rerank` | Document reranking | `forward_simple_request` | `reranker`, `vision_reranker` |
| `POST` | `/v1/score` | Relevance scoring | `forward_simple_request` | `reranker`, `vision_reranker` |
| `POST` | `/v1/messages`, `/messages` | Anthropic Messages (translates to OpenAI; streams `reasoning_content` as `thinking` blocks; emits SSE `ping` every 10 s of downstream silence) | `forward_messages_request` | `llm`, `vlm` |
| `POST` | `/v1/messages/count_tokens` | Anthropic token counting (forwards to vLLM `/tokenize`, falls back to chars/4) | `forward_count_tokens_request` | `llm`, `vlm` |
| `POST` | `/v1/tokenize`, `/tokenize` | vLLM-native pass-through tokenize | `forward_tokenize_request` | `llm`, `vlm` |

### Proxy Methods

| Method | Streaming | Use Case | Features |
|---|---|---|---|
| `forward_request` | stream + non-stream | Chat Completions | SSE parsing, auto-injects `stream_options.include_usage` |
| `forward_simple_request` | non-stream only | Embeddings, Rerank, Score | 120s timeout, handles reranker total_tokens reporting |
| `forward_to_path` | stream + non-stream | Responses API | Raw pass-through, only replaces model field |
| `forward_messages_request` | stream + non-stream | Anthropic Messages | Anthropic→OpenAI request, OpenAI→Anthropic response (uses `services/anthropic_adapter.py`) |
| `forward_count_tokens_request` | non-stream | `count_tokens` | Forwards to vLLM `/tokenize`; falls back to chars/4 if downstream tokenizer unavailable; not billed |
| `forward_tokenize_request` | non-stream | `/tokenize` | vLLM-native pass-through; not billed |

### Common Behavior

All proxy methods share `_resolve_model()` for health-aware routing:

```
1. Exact match + server alive → use directly
2. Server DOWN → use configured fallback of same type
3. No match found → use any alive server of same type
4. No alive servers → best-effort try any server of compatible type
```

When fallback occurs, the response includes an `X-Model-Fallback` header explaining the reason.

---

## `azure_api.py` — Azure OpenAI Compatibility

**Auth**: `deps.require_azure_access` (wraps `get_current_user` and additionally checks `user.can_use_azure`; admins bypass) — same gateway API key + daily limit as `/v1/*`, plus a per-user Azure access flag

Same client API key, same usage logging, same monitoring as the `/v1/*` path. Only the downstream URL/header conventions differ — handled by `services/azure_proxy.py`. Azure deployments are configured under `[azure_models.<alias>]` in `config.toml`. Users without `can_use_azure` (and not admin) get 403 with `"Azure access not granted. Contact your administrator."`.

### Endpoints

| Method | Path | Description | Proxy Method |
|---|---|---|---|
| `GET` | `/azure/v1/models` | List configured Azure deployments (not on `/v1/models`) | Direct response |
| `POST` | `/azure/v1/chat/completions` | Chat completion via Azure deployment | `forward_chat_completions` |
| `POST` | `/azure/v1/embeddings` | Embeddings via Azure deployment | `forward_embeddings` |
| `POST` | `/azure/v1/messages`, `/azure/messages` | Anthropic Messages → Azure (shared `anthropic_adapter`; same `thinking`-block translation and 10 s SSE `ping` heartbeat as the vLLM path) | `forward_messages` |
| `POST` | `/azure/v1/messages/count_tokens`, `/azure/messages/count_tokens` | Token counting (chars/4 estimate; Azure has no tokenize endpoint) | `forward_count_tokens` |

### Azure-specific behavior

- URL pattern: `{endpoint}/openai/deployments/{deployment}/{path}?api-version=...`
- Header: `api-key: <key>` (not `Authorization: Bearer`)
- Body's `model` field is stripped before forwarding (Azure routes by deployment in URL)
- No `_resolve_model` / fallback — alias maps directly to a deployment
- OpenAI-shape responses pass through unchanged; Anthropic responses use the same translator as the vLLM path
- `_calc_cost` is decoupled — `azure_proxy` looks up `AZURE_MODELS` and passes the route dict to `_log_usage(route=...)`

---

## `web_ui.py` — User Dashboard

**Auth**: `auth.get_web_user` (JWT scope must include `read` or `admin`)

### Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Welcome page: API guide, available models, quick start |
| `GET` | `/dashboard` | Dashboard page: usage stats, trend charts, server status, app accounts |
| `POST` | `/dashboard/refresh-key` | Regenerate own API key, returns JSON `{ok, api_key}` |
| `POST` | `/dashboard/app/{app_id}/refresh-key` | Regenerate owned app account's API key (must be owner) |

### Dashboard Data Sources

| Section | Data Function | Description |
|---|---|---|
| Stats Cards | `get_user_monthly_summary()` | Current month requests, cost, tokens |
| Usage Trend | `get_daily_trends()` | Daily requests/cost for last 30 days |
| My App Accounts | `get_owned_apps_summary()` | Owned app accounts' monthly usage |
| System Status | `MODEL_ROUTING` + `is_alive()` | Server health grouped by type |

---

## `admin.py` — Admin Panel + Admin API

**Auth**: Router-level `Security(get_web_user, scopes=["admin"])` — all `/admin/*` endpoints (including REST API) use JWT auth

### Web UI Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/admin` | Admin home: usage summary, DAU trends, department usage, leaderboards, user management. Supports pagination (`?limit=15&offset=0&app_limit=15&app_offset=0`) and server-side search (`?q=keyword`, ILIKE on username/display_name/org_code) |
| `POST` | `/admin/users/create` | Create app account (form submit, username auto-prefixed with `app_`) |
| `POST` | `/admin/users/{id}/limit` | Update user daily limit (form submit) |
| `POST` | `/admin/users/{id}/delete` | Delete user and their usage logs (cannot delete self) |
| `POST` | `/admin/users/{id}/refresh-key` | Regenerate any user's API key, returns JSON |
| `POST` | `/admin/users/{id}/monitor` | Toggle request/response monitoring for a user (returns JSON `{ok, monitoring, username}`) |
| `POST` | `/admin/users/{id}/toggle-disable` | Flip the user's `is_disabled` flag. Refuses to disable yourself (400). Allows enabling yourself |
| `POST` | `/admin/users/{id}/toggle-azure` | Flip the user's `can_use_azure` flag (gates `/azure/v1/*` access) |
| `POST` | `/admin/default-limit` | Set the default daily limit (USD). Persists to `[app].default_daily_limit_usd` in `config.toml` and bulk-bumps any user with `0 < daily_limit_usd < new_floor` up to the floor (unlimited users with `daily_limit_usd = 0` are never modified) |
| `GET` | `/admin/monitor` | Get monitoring status: list of currently monitored users with per-type file sizes and total disk usage |
| `GET` | `/admin/models` | Model config page (SPA, loads data via API) |

### Admin REST API (JWT auth, requires admin scope)

| Method | Path | Description |
|---|---|---|
| `GET` | `/admin/users` | List all users (includes `display_name`, `org_code`) |
| `POST` | `/admin/users` | Create user (JSON body: `username`, `daily_limit_usd`, `is_admin`, `owner_ids`) |
| `PATCH` | `/admin/users/{id}` | Update user (`daily_limit_usd`, `is_admin`, `owner_ids`) |

### Model Config API (JWT auth, requires admin scope)

| Method | Path | Description |
|---|---|---|
| `GET` | `/admin/api/config` | Get `config.toml` contents (models, pricing, fallback, azure_models) |
| `PUT` | `/admin/api/config` | Save config to `config.toml` and reload immediately |

---

## Route & Auth Reference

```mermaid
graph TD
    subgraph "API Key Auth (deps.py)"
        V1["/v1/models<br/>/v1/chat/completions<br/>/v1/embeddings<br/>/v1/rerank • /v1/score<br/>/v1/responses<br/>/v1/messages • /v1/messages/count_tokens<br/>/v1/tokenize"]
        Azure["/azure/v1/models<br/>/azure/v1/chat/completions<br/>/azure/v1/embeddings<br/>/azure/v1/messages • /azure/messages<br/>/azure/v1/messages/count_tokens"]
    end

    subgraph "JWT Auth (auth.py via oauth2-proxy)"
        Dashboard["/ • /dashboard<br/>Security: read scope"]
        Admin["/admin/*<br/>Security: admin scope<br/>(Web UI + REST API + Model Config + Monitor)"]
    end

    V1 -->|get_current_user| DB["DB: users.api_key"]
    Azure -->|require_azure_access<br/>(get_current_user + can_use_azure)| DB
    Dashboard -->|get_web_user| JWT["JWT: sub → users.username"]
    Admin -->|get_web_user| JWT
```
