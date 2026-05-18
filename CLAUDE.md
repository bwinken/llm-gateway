# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync

# Run the app (dev with auto-reload)
uv run fastapi dev app/main.py

# Run all tests
uv run pytest tests/ -v

# Run a single test file
uv run pytest tests/test_chat_completions.py -v

# Run a single test by name
uv run pytest tests/test_chat_completions.py::TestChatCompletionsNonStream::test_basic_completion -v

# Migrate data from old SQLite to PostgreSQL
uv run python scripts/migrate_sqlite_to_pg.py /path/to/llm_gateway.db

# Database migrations (Alembic)
uv run alembic upgrade head                              # Apply all pending migrations
uv run alembic revision --autogenerate -m "description"  # Generate migration from model changes
uv run alembic current                                   # Show current migration version

# Add a dependency
uv add <package>
```

## Architecture

OpenAI-compatible reverse proxy gateway for vLLM clusters. Routes API requests to downstream LLM/VLM/Embedding/Reranker servers with usage tracking and per-user billing.

### Request Flow

```
Client (Bearer API key) → FastAPI → deps.py (auth + daily limit check)
                                   → vllm_api.py  (/v1/*)     → vllm_proxy.py  (_resolve_model → health-aware fallback)
                                     azure_api.py (/azure/v1/*) → azure_proxy.py (Azure OpenAI deployment)
                                   → httpx.AsyncClient → downstream (vLLM or Azure OpenAI)
                                   → _log_usage() → DB
```

The codebase has two parallel backends sharing the same auth, billing, monitoring, and pricing layers:
- **vLLM path** — `vllm_api.py` + `vllm_proxy.py` serve `/v1/*`
- **Azure OpenAI path** — `azure_api.py` + `azure_proxy.py` serve `/azure/v1/*` (and `/azure/messages*`)

### Dual Auth System

- **API endpoints** (`/v1/*`): API key in `Authorization: Bearer <key>` header **or** `x-api-key: <key>` header (Anthropic-style) → `deps.py:get_current_user()` looks up User by `api_key` in DB
- **Azure endpoints** (`/azure/v1/*`): Same API key, but wrapped via `deps.py:require_azure_access()` which additionally checks `user.can_use_azure` (admins bypass) and 403s otherwise
- **Web UI + Admin** (`/`, `/dashboard`, `/admin/*`): oauth2-proxy handles login via AuthCenter OIDC → nginx `auth_request` validates session → injects `Authorization: Bearer <JWT>` header → `auth.py:get_web_user()` (FastAPI `Security` dependency) decodes JWT, enforces declared scopes, returns `User`
- All `/admin/*` routes (including REST API) use JWT auth via router-level `Security(get_web_user, scopes=["admin"])` — no API key auth on admin endpoints
- `/setup` (CA cert install page) and `/dashboard/install-claude-code.bat` both require SSO login — they are **not** bypassed by nginx. The page is intended for users already inside the org; the Claude Code installer download is personalized server-side (replaces `__USER_API_KEY__` in `setup/install-claude-code.bat` with the requesting user's `api_key`)
- JWT validation: RS256, `audience=AUTH_CENTER_APP_ID`, `issuer=AUTH_BASE_URL`, `leeway=5` for clock skew tolerance. Public key loaded from `AUTH_CENTER_PUBLIC_KEY_PATH` with mtime-based reload (key rotation takes effect without restart).
- Admin access is determined by `"admin" in scopes` (from AuthCenter JWT), NOT by `user.is_admin` in DB
- JWT payload fields `display_name` and `org_code` are persisted to the User model on each login (auto-synced from IdP) and displayed in navbar + admin tables
- **Account disable** (`is_disabled` column on `users`): both `get_current_user` and `get_web_user` raise `AccountDisabledError` when set. A global handler in `app/main.py` discriminates by `Accept` header — `text/html` → renders `templates/disabled.html` (styled page with Sign Out button); otherwise → JSON 403 `{"detail": "Account disabled. Contact your administrator."}`. **Admins bypass** the check (so a misflagged admin row can still log in to fix itself); the `/admin/users/{id}/toggle-disable` endpoint refuses self-disable, so this admin bypass is only ever reachable via direct DB edit.

### vLLM Proxy Layer (`app/services/vllm_proxy.py`)

Six forwarding methods, all sharing `_resolve_model()` for health-aware routing:

| Method | Used by | Behavior |
|---|---|---|
| `forward_request` | `/v1/chat/completions` | Stream + non-stream, SSE parsing |
| `forward_simple_request` | `/v1/embeddings`, `/v1/rerank`, `/v1/score` | Non-streaming, 120s timeout |
| `forward_to_path` | `/v1/responses` | Raw pass-through, only mutates model field |
| `forward_messages_request` | `/v1/messages` | Anthropic→OpenAI translation, OpenAI→Anthropic response, stream + non-stream |
| `forward_count_tokens_request` | `/v1/messages/count_tokens` | Forwards to vLLM `/tokenize`, returns `{input_tokens}`; not billed |
| `forward_tokenize_request` | `/v1/tokenize`, `/tokenize` | vLLM-native pass-through to downstream `/tokenize`; only mutates model field; not billed |

`_resolve_model()` priority: exact match + alive server → alive fallback of same type → best-effort any server of compatible type. Returns `X-Model-Fallback` response header when fallback occurs.

### Azure OpenAI Proxy Layer (`app/services/azure_proxy.py`)

Parallel to the vLLM path; routed by `app/routers/azure_api.py` and shares the same auth, daily-limit, monitoring, pricing, and `_log_usage` machinery.

| Method | Used by | Behavior |
|---|---|---|
| `forward_chat_completions` | `/azure/v1/chat/completions` | Stream + non-stream |
| `forward_embeddings` | `/azure/v1/embeddings` | Non-streaming |
| `forward_messages` | `/azure/v1/messages`, `/azure/messages` | Anthropic→OpenAI request via `anthropic_adapter`, then Azure-shaped forwarding |
| `forward_count_tokens` | `/azure/v1/messages/count_tokens`, `/azure/messages/count_tokens` | Returns chars/4 estimate (Azure has no tokenize endpoint); not billed |

Azure-specific conventions:
- URL is `{endpoint}/openai/deployments/{deployment}/{path}?api-version=...` (no `_resolve_model` / fallback — alias maps directly to a deployment).
- Auth header is `api-key: <key>` (not `Authorization: Bearer`).
- Body's `model` field is stripped before forwarding (Azure routes by deployment in URL).
- Response bodies are OpenAI-shaped and pass through unchanged; Anthropic responses use the same translator as the vLLM path.
- Azure models are NOT shown on `/v1/models` or the user-facing dashboard. They appear only on `/azure/v1/models` (and the admin model config UI).
- **Optional HTTP proxy**: when `AZURE_HTTP_PROXY` is set, all `/azure/v1/*` downstream calls go through that corporate HTTP proxy via a dedicated `httpx.AsyncClient` (`server_state.get_azure_client()`). vLLM downstreams are internal LAN and are never proxied — they always use the shared `get_client()`. When `AZURE_HTTP_PROXY` is unset, `get_azure_client()` falls back to the shared client (unchanged behavior).

### Cost Calculation

`_calc_cost(route, model_type, input_tokens, output_tokens)` is decoupled from any specific routing map. Each caller looks up its own route map (vLLM uses `MODEL_ROUTING`, Azure uses `AZURE_MODELS`) and passes the resolved route dict in. `_log_usage` accepts an optional `route=` kwarg (defaulting to `MODEL_ROUTING` lookup for backward compatibility).

**Pricing lookup priority** (per request): per-model override on the route dict (`input_price_per_1m` / `output_price_per_1m`) → per-type `[pricing.<type>]` → `[pricing]` defaults (`_default`). Both vLLM `[models.<type>.<alias>]` and `[azure_models.<alias>]` entries can carry the per-model override fields.

### Anthropic Messages API (`app/services/anthropic_adapter.py`)

`/v1/messages` accepts Anthropic-format requests for any LLM/VLM and works with stock vLLM downstreams. The adapter is purely stateless translation:

- **Request**: `anthropic_to_openai_request()` flattens `system` into a system message, converts `image` blocks (base64/url) to OpenAI `image_url` parts, maps `tool_use`/`tool_result` to OpenAI `tool_calls`/`role:"tool"`, and translates `tools`/`tool_choice` into OpenAI's function-calling schema.
- **Thinking preservation (request direction)**: when an assistant message in the request history carries `thinking` content blocks (from a previous reasoning turn), the adapter carries that reasoning back downstream as a `reasoning_content` field on the OpenAI assistant message instead of dropping it — symmetric with the response-direction `reasoning_content` → `thinking` block translation. Whether the downstream actually re-injects historical reasoning into the prompt depends on its chat template (e.g. Qwen3's `preserve_thinking`, set at vLLM server startup); the gateway only guarantees the data survives translation.
- **Reasoning effort translation**: `anthropic_to_openai_request()` takes an `is_reasoning: bool = False` parameter (the resolved model's `is_reasoning` metadata flag). When `is_reasoning` is true, it maps the Anthropic request's reasoning preference to OpenAI's `reasoning_effort` — a top-level `effort` string (low/medium/high; Claude Code's extra-high/max clamp to high; minimal/none → low), or `thinking: {"type": "enabled", "budget_tokens": N}` bucketed by budget (N≤4096 → low, N≤16384 → medium, N>16384 → high). When `is_reasoning` is false (default, non-reasoning models), `reasoning_effort` is **never** emitted, so non-reasoning downstreams (e.g. a plain Azure gpt-4o deployment) are not sent a parameter they would reject with a 400. `vllm_proxy.forward_messages_request` and `azure_proxy.forward_messages` pass `is_reasoning=route.get("is_reasoning")` / `entry.get("is_reasoning")`; the count_tokens paths do not (they never forward `reasoning_effort`). Operators must mark a model `is_reasoning = true` in `config.toml` for effort translation to activate.
- **Non-stream response**: `openai_to_anthropic_response()` builds the Anthropic message envelope with `content` blocks (text + tool_use), `stop_reason` mapping (`stop`→`end_turn`, `length`→`max_tokens`, `tool_calls`→`tool_use`), and Anthropic-style `usage` (`input_tokens`/`output_tokens`). When the OpenAI message carries `reasoning_content` (vLLM `--enable-reasoning`, DeepSeek's OpenAI-compatible API, Qwen3-thinking, etc.), a `{"type": "thinking", "thinking": "...", "signature": ""}` block is prepended before the text block.
- **Stream**: `AnthropicStreamTranslator` is a stateful chunk-by-chunk converter that emits the canonical Anthropic SSE event sequence: `message_start` → `content_block_start` → `content_block_delta` (`text_delta` or `input_json_delta`) → `content_block_stop` → `message_delta` (with stop_reason + output_tokens) → `message_stop`. Tool-call deltas are tracked by their OpenAI `index` and mapped to distinct Anthropic content block indices. When a delta carries `reasoning_content`, `_ensure_thinking_block` opens a thinking content block (`content_block_start` with `{"type": "thinking", "thinking": ""}`) and streams the chunks as `content_block_delta` with `{"type": "thinking_delta", "thinking": "..."}`; the first regular `content` delta then transitions to a separate text block. **No `signature_delta` is emitted on thinking-block close** — vLLM has no upstream signature to forward, and strict Claude Code builds validate signature shape, so `content_block_stop` alone closes the block (matches LiteLLM's behavior of only emitting `signature_delta` when the upstream chunk actually carries one).
- **SSE ping heartbeat**: `_pump_anthropic_lines` in `vllm_proxy.py` / `azure_proxy.py` wraps `client.send(...)` + `aiter_lines()` as a background task feeding an `asyncio.Queue`; the consumer `wait_for(queue.get(), timeout=10s)` and emits `event: ping\ndata: {"type": "ping"}\n\n` every 10 seconds of downstream silence (`_ANTHROPIC_PING_INTERVAL = 10.0`, shared between vLLM and Azure paths). Reasoning models with long prefill, queued vLLM batches, and slow downstream HTTP-header turnaround can produce gaps far longer than typical idle timeouts; Claude Code (and other Anthropic SDK clients) treat long silence as a dead connection without these pings.
- **Stream-start logging**: Both `_stream_messages` helpers log `Stream start | user={username} model={alias} endpoint=/v1/messages` (or `/azure/v1/messages`) before contacting downstream, so operators can verify request arrival without waiting for the end-of-stream `_log_usage` line.
- **Token counting** (`/v1/messages/count_tokens`): Used by Claude Code and other Anthropic SDK clients for context-window tracking. Translates the Anthropic request via `anthropic_to_openai_request()` and forwards the resulting `messages` (plus `tools`) to vLLM's `/tokenize` endpoint with `add_generation_prompt: true`, then returns `{"input_tokens": N}`. If the downstream tokenizer is unavailable (connection error, 404, malformed body), falls back to a chars/4 estimate so Claude Code clients still get a usable answer instead of a 5xx. Auth/daily-limit checks still apply but the call is **not** logged to `usage_logs` (it's a metadata query, not billable inference).

### Configuration

- **`config.toml`**: Parsed at import time by `app/core/config.py` into `APP_CONFIG`, `MODEL_ROUTING`, `PRICING_MAP`, `FALLBACK_MAP`, and `AZURE_MODELS`. Downstream API keys are stored directly here as `api_key`. Sections:
  - `[app]` — `default_daily_limit_usd` (default `10.0`) used when auto-provisioning new users.
  - `[models.<type>.<alias>]` — vLLM routing: `base_url`, `real_model`, `api_key`. Optional per-model overrides: `input_price_per_1m`, `output_price_per_1m`, plus metadata (`display_name`, `context_window`, `max_output_tokens`, `supports_tools`, `supports_vision`, `supports_prompt_caching`, `is_reasoning`) and internal flags (`hidden`). `is_reasoning` also gates Anthropic `reasoning_effort` translation — see Anthropic Messages API above.
  - `[pricing]` and `[pricing.<type>]` — default + per-type pricing (USD per 1M tokens).
  - `[fallback]` — type → preferred fallback alias.
  - `[azure_models.<alias>]` — Azure OpenAI deployments: `type`, `endpoint`, `deployment`, `api_key`, `api_version` (default `2024-08-01-preview`). Same per-model pricing/metadata override fields are accepted.
- **`.env`**: DATABASE_URL, AUTH_CENTER_APP_ID/PUBLIC_KEY_PATH, AUTH_BASE_URL (JWT issuer), `AZURE_HTTP_PROXY` (optional — routes `/azure/v1/*` downstream traffic through a corporate HTTP proxy; supports inline credentials `http://user:pass@host:port`; vLLM traffic is never proxied).
- **`deploy/.env`**: Docker Compose settings: PG credentials, OIDC issuer, oauth2-proxy client.

### Key Patterns

- vLLM downstream HTTP calls use a single shared `httpx.AsyncClient` (initialized in lifespan, accessed via `server_state.get_client()`); Azure downstream calls use `server_state.get_azure_client()`, which returns a separate proxied client when `AZURE_HTTP_PROXY` is set or falls back to the shared client otherwise
- Background health check loop pings all unique `base_url`s every 30s, updates `server_state._health_cache`, prunes stale entries (vLLM only; Azure deployments are not health-checked)
- Usage is logged per-request to `usage_logs` table; cost lookup follows per-model override → per-type → `_default`
- Model alias (user-facing name) is swapped to `real_model` (vLLM) or routed to its `deployment` (Azure) before forwarding downstream
- New users are auto-provisioned with `daily_limit_usd = APP_CONFIG.default_daily_limit_usd` (admins can change the floor at runtime via `POST /admin/default-limit`, which also bulk-bumps any user with `0 < daily_limit_usd < new_floor`; users at `0` (unlimited) are never modified)

### Per-User Monitoring (`app/services/monitor.py`)

- Admins can toggle monitoring on individual users via `POST /admin/users/{id}/monitor`; state is in-memory (cleared on restart)
- When monitoring is active, full request/response payloads are logged as JSONL files under `monitor/{username}/{date}_{type}.jsonl` (e.g. `20260402_chat.jsonl`)
- Only monitors `llm`, `embedding`, and `reranker` model types (vision variants excluded)
- File writes happen in a background thread via `asyncio.run_in_executor` (fire-and-forget, non-blocking)
- `GET /admin/monitor` returns currently monitored users with per-type file sizes and total disk usage; warns at 100 MB per user

## Testing

Tests use in-memory SQLite with `StaticPool` (all connections share one DB). Key setup in `tests/conftest.py`:

- `os.environ["DATABASE_URL"] = "sqlite://"` set before any app imports (avoids psycopg2 requirement)
- `_patch_all` autouse fixture patches: `MODEL_ROUTING`, `PRICING_MAP`, `get_client`, `engine`, `is_alive`
- `client` fixture builds a test app with noop lifespan (no health checks, no real DB init)
- `FakeStreamResponse` simulates SSE streaming for stream tests
- Mock httpx client accessible via `client.__httpx_mock__`

When adding new tests, always use the existing `client` fixture and `auth_header()` helper (for `/v1/*` API key auth) or `web_auth_header()` helper (for JWT web/admin auth). Set mock responses on `client.__httpx_mock__.post` (non-stream) or `client.__httpx_mock__.send` (stream).

## Database

- Production: PostgreSQL (`psycopg2-binary`)
- Tables: `users` (with auto-generated `api_key`, `display_name`, `org_code`, plus boolean access-control flags `is_disabled` and `can_use_azure`), `usage_logs` (with covering index on `user_id + created_at + cost_usd`), `app_owners` (many-to-many: which users own which app accounts)
- `password_hash` field exists on User model with `default=""` for backward compatibility but is unused
- `display_name` and `org_code` are synced from IdP JWT on each web login
- `is_disabled` and `can_use_azure` default to `False`; flipped via the admin panel buttons (`/admin/users/{id}/toggle-disable`, `/admin/users/{id}/toggle-azure`). Admins bypass both checks at auth time
- `scripts/cleanup_usage_logs.py`: retention cleanup script (default 1 year, dry-run by default, `--execute` to delete)
