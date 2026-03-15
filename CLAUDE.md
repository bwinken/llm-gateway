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

- **API endpoints** (`/v1/*`): API key in `Authorization: Bearer <key>` header → `deps.py:get_current_user()` looks up User by `api_key` in DB
- **Web UI** (`/`, `/dashboard`, `/admin`): oauth2-proxy handles login via AuthCenter OIDC → nginx `auth_request` validates session → injects `Authorization: Bearer <JWT>` header → `auth.py:get_web_user()` decodes JWT, returns `(User, scopes, payload)`
- JWT validation: RS256, `audience=AUTH_CENTER_APP_ID`, `issuer=AUTH_BASE_URL`, `leeway=5` for clock skew tolerance. Public key loaded from `AUTH_CENTER_PUBLIC_KEY_PATH` with mtime-based reload (key rotation takes effect without restart).
- Admin access for web UI is determined by `"admin" in scopes` (from AuthCenter JWT), NOT by `user.is_admin` in DB
- JWT payload fields `display_name` and `org_code` are persisted to the User model on each login (auto-synced from IdP) and displayed in navbar + admin tables

### Proxy Layer (`app/services/proxy.py`)

Three forwarding methods, all sharing `_resolve_model()` for health-aware routing:

| Method | Used by | Behavior |
|---|---|---|
| `forward_request` | `/v1/chat/completions` | Stream + non-stream, SSE parsing |
| `forward_simple_request` | `/v1/embeddings`, `/v1/rerank`, `/v1/score` | Non-streaming, 120s timeout |
| `forward_to_path` | `/v1/responses` | Raw pass-through, only mutates model field |

`_resolve_model()` priority: exact match + alive server → alive fallback of same type → best-effort any server of compatible type. Returns `X-Model-Fallback` response header when fallback occurs.

### Configuration

- **`config.toml`**: Model routing (alias → base_url + real_model + api_key + type) and per-type pricing. Parsed at import time by `app/core/config.py` into `MODEL_ROUTING` and `PRICING_MAP` dicts. Downstream API keys are stored directly here as `api_key`.
- **`.env`**: DATABASE_URL, AUTH_CENTER_APP_ID/PUBLIC_KEY_PATH, AUTH_BASE_URL (JWT issuer).
- **`deploy/.env`**: Docker Compose settings: PG credentials, OIDC issuer, oauth2-proxy client.

### Key Patterns

- All downstream HTTP calls use a single shared `httpx.AsyncClient` (initialized in lifespan, accessed via `server_state.get_client()`)
- Background health check loop pings all unique `base_url`s every 30s, updates `server_state._health_cache`, prunes stale entries
- Usage is logged per-request to `usage_logs` table with cost calculated from `PRICING_MAP`
- Model alias (user-facing name) is swapped to `real_model` before forwarding downstream

## Testing

Tests use in-memory SQLite with `StaticPool` (all connections share one DB). Key setup in `tests/conftest.py`:

- `os.environ["DATABASE_URL"] = "sqlite://"` set before any app imports (avoids psycopg2 requirement)
- `_patch_all` autouse fixture patches: `MODEL_ROUTING`, `PRICING_MAP`, `get_client`, `engine`, `is_alive`
- `client` fixture builds a test app with noop lifespan (no health checks, no real DB init)
- `FakeStreamResponse` simulates SSE streaming for stream tests
- Mock httpx client accessible via `client.__httpx_mock__`

When adding new tests, always use the existing `client` fixture and `auth_header()` helper (for API key auth) or `web_auth_header()` helper (for JWT web auth). Set mock responses on `client.__httpx_mock__.post` (non-stream) or `client.__httpx_mock__.send` (stream).

## Database

- Production: PostgreSQL (`psycopg2-binary`)
- Tables: `users` (with auto-generated `api_key`, `display_name`, `org_code`), `usage_logs` (with covering index on `user_id + created_at + cost_usd`), `app_owners` (many-to-many: which users own which app accounts)
- `password_hash` field exists on User model with `default=""` for backward compatibility but is unused
- `display_name` and `org_code` are synced from IdP JWT on each web login
- `scripts/cleanup_usage_logs.py`: retention cleanup script (default 1 year, dry-run by default, `--execute` to delete)
