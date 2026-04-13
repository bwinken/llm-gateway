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
                                   → llm_api.py (endpoint routing)
                                   → proxy.py (_resolve_model → health-aware fallback)
                                   → httpx.AsyncClient → downstream vLLM server
                                   → _log_usage() → DB
```

### Dual Auth System

- **API endpoints** (`/v1/*`): API key in `Authorization: Bearer <key>` header **or** `x-api-key: <key>` header (Anthropic-style) → `deps.py:get_current_user()` looks up User by `api_key` in DB
- **Web UI + Admin** (`/`, `/dashboard`, `/admin/*`): oauth2-proxy handles login via AuthCenter OIDC → nginx `auth_request` validates session → injects `Authorization: Bearer <JWT>` header → `auth.py:get_web_user()` (FastAPI `Security` dependency) decodes JWT, enforces declared scopes, returns `User`
- All `/admin/*` routes (including REST API) use JWT auth via router-level `Security(get_web_user, scopes=["admin"])` — no API key auth on admin endpoints
- JWT validation: RS256, `audience=AUTH_CENTER_APP_ID`, `issuer=AUTH_BASE_URL`, `leeway=5` for clock skew tolerance. Public key loaded from `AUTH_CENTER_PUBLIC_KEY_PATH` with mtime-based reload (key rotation takes effect without restart).
- Admin access is determined by `"admin" in scopes` (from AuthCenter JWT), NOT by `user.is_admin` in DB
- JWT payload fields `display_name` and `org_code` are persisted to the User model on each login (auto-synced from IdP) and displayed in navbar + admin tables

### Proxy Layer (`app/services/proxy.py`)

Four forwarding methods, all sharing `_resolve_model()` for health-aware routing:

| Method | Used by | Behavior |
|---|---|---|
| `forward_request` | `/v1/chat/completions` | Stream + non-stream, SSE parsing |
| `forward_simple_request` | `/v1/embeddings`, `/v1/rerank`, `/v1/score` | Non-streaming, 120s timeout |
| `forward_to_path` | `/v1/responses` | Raw pass-through, only mutates model field |
| `forward_messages_request` | `/v1/messages` | Anthropic→OpenAI translation, OpenAI→Anthropic response, stream + non-stream |

`_resolve_model()` priority: exact match + alive server → alive fallback of same type → best-effort any server of compatible type. Returns `X-Model-Fallback` response header when fallback occurs.

### Anthropic Messages API (`app/services/anthropic_adapter.py`)

`/v1/messages` accepts Anthropic-format requests for any LLM/VLM and works with stock vLLM downstreams. The adapter is purely stateless translation:

- **Request**: `anthropic_to_openai_request()` flattens `system` into a system message, converts `image` blocks (base64/url) to OpenAI `image_url` parts, maps `tool_use`/`tool_result` to OpenAI `tool_calls`/`role:"tool"`, and translates `tools`/`tool_choice` into OpenAI's function-calling schema.
- **Non-stream response**: `openai_to_anthropic_response()` builds the Anthropic message envelope with `content` blocks (text + tool_use), `stop_reason` mapping (`stop`→`end_turn`, `length`→`max_tokens`, `tool_calls`→`tool_use`), and Anthropic-style `usage` (`input_tokens`/`output_tokens`).
- **Stream**: `AnthropicStreamTranslator` is a stateful chunk-by-chunk converter that emits the canonical Anthropic SSE event sequence: `message_start` → `content_block_start` → `content_block_delta` (`text_delta` or `input_json_delta`) → `content_block_stop` → `message_delta` (with stop_reason + output_tokens) → `message_stop`. Tool-call deltas are tracked by their OpenAI `index` and mapped to distinct Anthropic content block indices.

### Configuration

- **`config.toml`**: Model routing (alias → base_url + real_model + api_key + type) and per-type pricing. Parsed at import time by `app/core/config.py` into `MODEL_ROUTING` and `PRICING_MAP` dicts. Downstream API keys are stored directly here as `api_key`.
- **`.env`**: DATABASE_URL, AUTH_CENTER_APP_ID/PUBLIC_KEY_PATH, AUTH_BASE_URL (JWT issuer).
- **`deploy/.env`**: Docker Compose settings: PG credentials, OIDC issuer, oauth2-proxy client.

### Key Patterns

- All downstream HTTP calls use a single shared `httpx.AsyncClient` (initialized in lifespan, accessed via `server_state.get_client()`)
- Background health check loop pings all unique `base_url`s every 30s, updates `server_state._health_cache`, prunes stale entries
- Usage is logged per-request to `usage_logs` table with cost calculated from `PRICING_MAP`
- Model alias (user-facing name) is swapped to `real_model` before forwarding downstream

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
- Tables: `users` (with auto-generated `api_key`, `display_name`, `org_code`), `usage_logs` (with covering index on `user_id + created_at + cost_usd`), `app_owners` (many-to-many: which users own which app accounts)
- `password_hash` field exists on User model with `default=""` for backward compatibility but is unused
- `display_name` and `org_code` are synced from IdP JWT on each web login
- `scripts/cleanup_usage_logs.py`: retention cleanup script (default 1 year, dry-run by default, `--execute` to delete)
