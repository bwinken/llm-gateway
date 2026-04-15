# LLM Gateway

[中文版](README.zh-TW.md)

OpenAI-compatible reverse proxy gateway for [vLLM](https://github.com/vllm-project/vllm) serving clusters.
Routes, proxies, and monitors traffic from client applications to downstream vLLM instances (LLM, VLM, Embedding, Reranker).

```
Client App ──▶ LLM Gateway ──▶ vLLM Instance A  [LLM]
                    │                 ──▶ vLLM Instance B  [VLM]
                    │                 ──▶ vLLM Instance C  [Embedding]
                    │                 ──▶ vLLM Instance D  [Reranker]
                    │
                    ├── Auth (API key / OAuth2 SSO)
                    ├── Routing (model alias → vLLM instance)
                    ├── Smart fallback (health-aware)
                    ├── Usage logging (tokens + cost)
                    └── Health monitoring (30s interval)
```

## Screenshots

| Welcome | Dashboard |
|---|---|
| ![Welcome](docs/screenshots/welcome.png) | ![Dashboard](docs/screenshots/dashboard.png) |

| Admin Panel | Model Config |
|---|---|
| ![Admin](docs/screenshots/admin.png) | ![Models](docs/screenshots/models.png) |

## Features

- **OpenAI-compatible API** — `/v1/chat/completions`, `/v1/embeddings`, `/v1/rerank`, `/v1/score`, `/v1/responses`, `/v1/models` (LLM/VLM only)
- **Multi-model routing** — LLM, VLM, Embedding, Vision Embedding, Reranker, Vision Reranker
- **SSE streaming** — Full Server-Sent Events support for chat completions and responses
- **Smart fallback** — Configurable per-type fallback model, health-check-aware; `X-Model-Fallback` response header
- **Tiered pricing** — Per-type input/output token pricing with automatic cost calculation
- **Usage tracking** — Per-user token and cost logging to PostgreSQL
- **OAuth2 SSO** — [AuthCenter](https://github.com/bwinken/authcenter) integration with RS256 JWT, auto-provisioning users
- **Dual auth** — API key (Bearer token) for SDK/API, oauth2-proxy + JWT for web UI
- **Web dashboard** — Usage stats, Chart.js trend charts, grouped server health status
- **Admin panel** — User management, leaderboards, daily limit control, model config UI (routing/pricing/fallback)
- **Background health checks** — Periodic pings to all downstream servers

## Tech Stack

| Component | Technology |
|---|---|
| Framework | FastAPI (async ASGI) |
| HTTP Client | HTTPX (connection-pooled) |
| Database | PostgreSQL + SQLModel |
| Migrations | Alembic |
| Auth | PyJWT (RS256), [AuthCenter](https://github.com/bwinken/authcenter) OAuth2 |
| Frontend | Jinja2 + Tailwind CSS (CDN) + Chart.js |
| Downstream | [vLLM](https://github.com/vllm-project/vllm) (OpenAI-compatible) |

---

## Quick Start (Development)

### 1. Clone and install

```bash
git clone https://github.com/bwinken/llm-gateway.git
cd llm-gateway
uv sync
```

### 2. Configure

```bash
cp config.toml.example config.toml
cp .env.example .env
```

Edit `config.toml` — set your downstream vLLM server URLs and API keys:

```toml
[models.llm."my-model"]
real_model = "Qwen/Qwen2.5-72B"
base_url = "http://your-llm-server:8000/v1"
api_key = "token-abc123"
```

Edit `.env` — set database URL and auth settings:

```env
DATABASE_URL=postgresql://llm_gateway:your_password@localhost:5432/llm_gateway
AUTH_BASE_URL=http://auth.example.com
```

### 3. Start PostgreSQL

```bash
bash scripts/start-pg-dev.sh start
```

### 4. Set up AuthCenter public key

Place the AuthCenter RS256 public key at `keys/public.pem` (or change `AUTH_CENTER_PUBLIC_KEY_PATH` in `.env`).

### 5. Run

```bash
uv run fastapi dev app/main.py
```

The gateway starts on the port reported by FastAPI (8000 by default in dev mode).

> **Windows:** If you see `UnicodeEncodeError`, set `PYTHONUTF8=1` first.

---

## Configuration

### config.toml

Model routing and pricing. Each model maps an alias to a downstream vLLM instance:

```toml
[models.llm."model-alias"]
real_model = "actual-model-name"    # Model name sent to vLLM
base_url = "http://host:port/v1"    # vLLM server URL
api_key = "your-key"                # vLLM --api-key (leave empty if none)
```

Supported model types: `llm`, `vlm`, `embedding`, `vision_embedding`, `reranker`, `vision_reranker`.

Per-type pricing (USD per 1M tokens):

```toml
[pricing.llm]
input_price_per_1m = 0.50
output_price_per_1m = 1.50
```

Per-type fallback model (optional). When a model's server is down, prefer this model as fallback:

```toml
[fallback]
llm = "backup-llm"
vlm = "backup-vlm"
```

> All model routing, pricing, and fallback settings can also be managed through the **Admin Panel → Model Config** web UI, which reads and writes `config.toml` directly.

### .env

| Variable | Description | Default |
|---|---|---|
| `APP_TITLE` | Service name shown in UI, browser tab, and logs | `LLM Gateway` |
| `DATABASE_URL` | PostgreSQL connection string | `postgresql://llm_gateway:password@localhost:5432/llm_gateway` |
| `AUTH_CENTER_APP_ID` | JWT audience (AuthCenter app ID) | `llm_gateway` |
| `AUTH_CENTER_PUBLIC_KEY_PATH` | RS256 public key path | `./keys/public.pem` |
| `AUTH_BASE_URL` | JWT issuer URL (AuthCenter base URL) | `auth-center` |

> OAuth2 login settings (OIDC issuer, client secret, redirect URL) are configured in `deploy/.env` for oauth2-proxy. See [deploy/README.md](deploy/README.md).

---

## API Usage

All API endpoints require `Authorization: Bearer <api_key>`.

### Chat Completions

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://your-gateway/v1",
    api_key="sk-your-api-key"
)

resp = client.chat.completions.create(
    model="my-model",
    messages=[{"role": "user", "content": "Hello!"}]
)
```

### Embeddings

```python
resp = client.embeddings.create(
    model="bge-m3",
    input=["The quick brown fox"]
)
```

### Rerank

```bash
curl http://your-gateway/v1/rerank \
  -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "bge-reranker-v2-m3",
    "query": "What is AI?",
    "documents": ["AI is...", "Machine learning is..."]
  }'
```

### List Models

```bash
curl http://your-gateway/v1/models \
  -H "Authorization: Bearer sk-your-api-key"
```

### Web Dashboard

Open `http://your-gateway` in browser. oauth2-proxy handles SSO login via AuthCenter. Admin features require `admin` scope in AuthCenter.

---

## Deployment

Deployed with user-level systemd and PostgreSQL running in Docker. See [deploy/README.md](deploy/README.md) for details.

### Dev PostgreSQL

```bash
bash scripts/start-pg-dev.sh start    # Start (auto-creates container on first run)
bash scripts/start-pg-dev.sh stop     # Stop (data preserved)
bash scripts/start-pg-dev.sh status   # Check status
bash scripts/start-pg-dev.sh rm       # Remove container (data lost)
```

Uses the same credentials as `.env.example` — no extra configuration needed.

### Data Migration (SQLite → PostgreSQL)

```bash
# 1. Preview migration (no data written)
uv run python scripts/migrate_sqlite_to_pg.py /path/to/llm_gateway.db --dry-run

# 2. Full migration
uv run python scripts/migrate_sqlite_to_pg.py /path/to/llm_gateway.db

# 3. Incremental sync before going live (only migrates new data since last run)
uv run python scripts/migrate_sqlite_to_pg.py /path/to/llm_gateway.db --sync
```

> `--sync` uses the latest `usage_logs.created_at` in PostgreSQL as the cutoff, migrating only newer records and syncing updated user fields. The original SQLite file is not modified.

For the full migration steps, notes, and checklist, see the **[Migration Guide](docs/migration-guide.md)**.

---

## Database Migrations

Uses [Alembic](https://alembic.sqlalchemy.org/) for schema migrations. The `DATABASE_URL` from `.env` is used automatically.

```bash
# Apply all pending migrations
uv run alembic upgrade head

# Generate a new migration after changing models/schema.py
uv run alembic revision --autogenerate -m "describe your change"

# View current migration status
uv run alembic current
```

> For existing deployments upgrading to Alembic, run `uv run alembic stamp head` once to mark the current schema as up-to-date without re-running migrations.

---

## Testing

```bash
uv run pytest tests/ -v
```

Tests use in-memory SQLite and mock all downstream calls. No PostgreSQL or vLLM servers required.

---

## Project Structure

```
llm-gateway/
├── config.toml.example        # Model routing + pricing template
├── .env.example                # Environment variables template
├── pyproject.toml              # Dependencies and project config (uv)
├── uv.lock                     # Locked dependency versions
├── alembic.ini                 # Alembic migration config
├── alembic/
│   ├── env.py                  # Migration environment (reads DATABASE_URL)
│   └── versions/               # Migration scripts
├── scripts/
│   ├── migrate_sqlite_to_pg.py # SQLite → PostgreSQL migration
│   ├── cleanup_usage_logs.py   # Usage log retention cleanup
│   ├── add_owner_id.py         # App ownership assignment helper
│   └── start-pg-dev.sh         # Dev PostgreSQL container management
├── docs/
│   ├── migration-guide.md      # SQLite → PostgreSQL migration guide
│   └── screenshots/            # UI screenshots
├── app/
│   ├── main.py                 # FastAPI app, lifespan, middleware
│   ├── core/
│   │   ├── auth.py             # JWT validation (get_web_user)
│   │   ├── config.py           # TOML → MODEL_ROUTING + PRICING_MAP
│   │   ├── database.py         # SQLModel engine + session
│   │   ├── deps.py             # Bearer token auth
│   │   ├── server_state.py     # httpx client + health cache
│   │   └── logger.py
│   ├── models/
│   │   └── schema.py           # User + UsageLog + AppOwner tables
│   ├── routers/
│   │   ├── llm_api.py          # /v1/* API endpoints
│   │   ├── web_ui.py           # Dashboard (Jinja2)
│   │   └── admin.py            # Admin panel + API
│   ├── services/
│   │   ├── proxy.py            # Core proxy + fallback + logging
│   │   ├── stats.py            # Dashboard aggregations
│   │   └── health.py           # Background health loop
│   └── templates/
│       ├── base.html
│       ├── welcome.html        # Landing / login page
│       ├── dashboard.html
│       ├── admin.html          # User management + leaderboard
│       └── admin_models.html   # Model config UI
├── deploy/
│   ├── docker-compose.yml      # PostgreSQL + oauth2-proxy
│   ├── .env.example            # Docker services env vars
│   ├── setup.sh                # Deployment script
│   ├── llm-gateway.service     # systemd unit
│   ├── llm-gateway-example.nginx.conf  # Nginx config (auth_request)
│   └── README.md               # Deployment guide
└── tests/
    ├── conftest.py
    ├── test_chat_completions.py
    ├── test_embeddings.py
    ├── test_rerank_score.py
    ├── test_responses.py
    ├── test_vlm.py
    ├── test_vision_embedding.py
    ├── test_vision_rerank_score.py
    ├── test_admin.py
    └── test_app_ownership.py
```
