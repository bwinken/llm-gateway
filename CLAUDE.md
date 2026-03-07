# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the app (dev with auto-reload)
fastapi dev app/main.py

# Run all tests
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/test_chat_completions.py -v

# Run a single test by name
python -m pytest tests/test_chat_completions.py::TestChatCompletionsNonStream::test_basic_completion -v

# Migrate data from old SQLite to PostgreSQL
python scripts/migrate_sqlite_to_pg.py /path/to/llm_gateway.db
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
- **Web UI** (`/`, `/dashboard`, `/admin`): OAuth2 SSO via AuthCenter → JWT scopes stored in session → `auth_api.py:auth_callback()` auto-provisions users on first login
- Admin access for web UI is determined by `"admin" in session["scopes"]` (from AuthCenter JWT), NOT by `user.is_admin` in DB

### Proxy Layer (`app/services/proxy.py`)

Three forwarding methods, all sharing `_resolve_model()` for health-aware routing:

| Method | Used by | Behavior |
|---|---|---|
| `forward_request` | `/v1/chat/completions` | Stream + non-stream, SSE parsing |
| `forward_simple_request` | `/v1/embeddings`, `/v1/rerank`, `/v1/score` | Non-streaming, 120s timeout |
| `forward_to_path` | `/v1/responses` | Raw pass-through, only mutates model field |

`_resolve_model()` priority: exact match + alive server → alive fallback of same type → best-effort any server of compatible type. Returns `X-Model-Fallback` response header when fallback occurs.

### Configuration

- **`config.toml`**: Model routing (alias → base_url + real_model + type) and per-type pricing. Parsed at import time by `app/core/config.py` into `MODEL_ROUTING` and `PRICING_MAP` dicts.
- **`.env`**: Secrets, DATABASE_URL, AuthCenter OAuth2 settings, downstream API keys.

### Key Patterns

- All downstream HTTP calls use a single shared `httpx.AsyncClient` (initialized in lifespan, accessed via `server_state.get_client()`)
- Background health check loop pings all unique `base_url`s every 30s, updates `server_state._health_cache`
- Usage is logged per-request to `usage_logs` table with cost calculated from `PRICING_MAP`
- Model alias (user-facing name) is swapped to `real_model` before forwarding downstream

## Testing

Tests use in-memory SQLite with `StaticPool` (all connections share one DB). Key setup in `tests/conftest.py`:

- `os.environ["DATABASE_URL"] = "sqlite://"` set before any app imports (avoids psycopg2 requirement)
- `_patch_all` autouse fixture patches: `MODEL_ROUTING`, `PRICING_MAP`, `get_client`, `engine`, `is_alive`
- `client` fixture builds a test app with noop lifespan (no health checks, no real DB init)
- `FakeStreamResponse` simulates SSE streaming for stream tests
- Mock httpx client accessible via `client.__httpx_mock__`

When adding new tests, always use the existing `client` fixture and `auth_header()` helper. Set mock responses on `client.__httpx_mock__.post` (non-stream) or `client.__httpx_mock__.send` (stream).

## Database

- Production: PostgreSQL (`psycopg2-binary`)
- Tables: `users` (with auto-generated `api_key`), `usage_logs` (with composite index on `user_id + created_at`)
- `password_hash` field exists on User model with `default=""` for backward compatibility but is unused
