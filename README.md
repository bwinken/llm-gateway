# LLM Gateway

OpenAI-compatible reverse proxy gateway for [vLLM](https://github.com/vllm-project/vllm) serving clusters. Routes, proxies, and monitors traffic from client applications to downstream vLLM instances (LLM, VLM, Embedding, Reranker).

## Features

- **OpenAI-compatible API** — `/v1/chat/completions`, `/v1/embeddings`, `/v1/rerank`, `/v1/score`, `/v1/responses`, `/v1/models`
- **Multi-model routing** — LLM, VLM, Embedding, Vision Embedding, Reranker, Vision Reranker
- **SSE streaming** — Full Server-Sent Events support with `aiter_lines()` and `X-Accel-Buffering: no`
- **Smart fallback** — Type-safe automatic fallback to compatible model when requested model is unavailable
- **Tiered pricing** — Per-type input/output token pricing with automatic cost calculation
- **Usage tracking** — Per-user token and cost logging to SQLite/PostgreSQL
- **OAuth2 SSO** — AuthCenter integration with RS256 JWT, auto-provisioning users on first login
- **Dual auth** — API key (Bearer token) for SDK/API calls, OAuth2 session for web UI
- **Web dashboard** — Usage stats, Chart.js trend charts (Requests/Cost/Tokens), grouped server health status
- **Background health checks** — Periodic pings to all downstream vLLM instances with ONLINE/DOWN status cache
- **Admin API** — User management endpoints (`/admin/users`)

## Project Structure

```
llm-gateway/
├── config.toml                # Model routing + tiered pricing
├── .env                       # Secrets, DB config, default admin key
├── requirements.txt
└── app/
    ├── main.py                # FastAPI app, lifespan, middleware, routers
    ├── core/
    │   ├── config.py          # TOML parsing -> MODEL_ROUTING + PRICING_MAP
    │   ├── database.py        # SQLModel engine + session dependency
    │   ├── deps.py            # Bearer token API key validation
    │   ├── server_state.py    # Global httpx.AsyncClient + health cache
    │   └── logger.py          # Unified logging
    ├── models/
    │   └── schema.py          # User + UsageLog tables
    ├── routers/
    │   ├── llm_api.py         # OpenAI-compatible API endpoints
    │   ├── web_ui.py          # Dashboard pages (Jinja2)
    │   ├── auth_api.py        # OAuth2 SSO (AuthCenter callback + logout)
    │   └── admin.py           # Admin user management
    ├── services/
    │   ├── proxy.py           # Core proxy (3 forwarding methods + fallback + usage logging)
    │   ├── stats.py           # Dashboard aggregations
    │   └── health.py          # Background health check loop
    └── templates/
        ├── base.html          # Tailwind + Chart.js layout
        ├── dashboard.html
        └── admin.html
```

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure

Edit `config.toml` to point models to your downstream vLLM instances:

```toml
[models.llm."llama-3.1-8b"]
base_url = "http://localhost:8001/v1"   # vLLM server serving this model
```

Edit `.env` to set your secret key and default admin credentials:

```env
SECRET_KEY=your-random-secret-key
DEFAULT_ADMIN_KEY=your-admin-api-key
DEFAULT_ADMIN_USER=admin
```

### 3. Run

```bash
fastapi run app/main.py
```

For development with auto-reload:

```bash
fastapi dev app/main.py
```

> **Windows note:** If you see a `UnicodeEncodeError`, set the environment variable `PYTHONUTF8=1` before running, or run `set PYTHONUTF8=1` in your terminal first.

The gateway starts on `http://localhost:8000`.

## Usage

### Default admin account

On first startup, an admin user is created automatically from `.env` values. Use the admin API key as a Bearer token.

### API examples

**Chat completion:**

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer gw-admin-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama-3.1-8b",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

**Streaming:**

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer gw-admin-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama-3.1-8b",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": true
  }'
```

**Embeddings:**

```bash
curl http://localhost:8000/v1/embeddings \
  -H "Authorization: Bearer gw-admin-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "bge-m3",
    "input": "Search query text"
  }'
```

**Reranking:**

```bash
curl http://localhost:8000/v1/rerank \
  -H "Authorization: Bearer gw-admin-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "bge-reranker-v2-m3",
    "query": "What is AI?",
    "documents": ["AI is...", "Machine learning is..."]
  }'
```

**List models:**

```bash
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer gw-admin-key-change-me"
```

### Web dashboard

Open `http://localhost:8000` in your browser. You will be redirected to AuthCenter for SSO login. After authentication, you'll see usage stats, cost trends, and server health status. Admin features are available if your AuthCenter account has the `admin` scope.

## Configuration Reference

### Pricing (`config.toml`)

Each model type has independent input/output pricing per 1M tokens:

```toml
[pricing.llm]
input_price_per_1m  = 0.50
output_price_per_1m = 1.50
```

### Model routing (`config.toml`)

Each entry maps a model name to a downstream vLLM instance. Quote model names containing dots:

```toml
[models.llm."qwen-2.5-72b"]
base_url = "http://localhost:8002/v1"   # vLLM --model Qwen/Qwen2.5-72B

[models.vlm."qwen-2.5-vl-72b"]
base_url = "http://localhost:8003/v1"   # vLLM --model Qwen/Qwen2.5-VL-72B

[models.embedding.bge-m3]
base_url = "http://localhost:8004/v1"   # vLLM --model BAAI/bge-m3
```

Supported types: `llm`, `vlm`, `embedding`, `vision_embedding`, `reranker`, `vision_reranker`.

### Environment variables (`.env`)

| Variable | Description | Default |
|---|---|---|
| `SECRET_KEY` | Session cookie encryption key | `change-me` |
| `DATABASE_URL` | SQLModel database URL | `sqlite:///./llm_gateway.db` |
| `DEFAULT_ADMIN_KEY` | Bootstrap admin API key | `gw-admin-key-change-me` |
| `DEFAULT_ADMIN_USER` | Bootstrap admin username | `admin` |
| `DEFAULT_ADMIN_DAILY_LIMIT` | Admin daily budget (USD) | `100.0` |
| `AUTH_CENTER_BASE_URL` | AuthCenter server URL | `http://localhost:8000` |
| `AUTH_CENTER_APP_ID` | OAuth2 application ID | `llm_gateway` |
| `AUTH_CENTER_CLIENT_SECRET` | OAuth2 client secret | `change-me` |
| `AUTH_CENTER_REDIRECT_URI` | OAuth2 callback URL | `http://localhost:8050/auth/callback` |
| `AUTH_CENTER_PUBLIC_KEY_PATH` | Path to AuthCenter RS256 public key | `./keys/public.pem` |

## Proxy Behavior

| Method | Endpoint | Allowed Types | Behavior |
|---|---|---|---|
| `forward_request` | `/v1/chat/completions` | llm, vlm | Streaming + non-streaming, SSE parsing |
| `forward_simple_request` | `/v1/embeddings` | embedding, vision_embedding | Non-streaming, 120s timeout |
| `forward_simple_request` | `/v1/rerank`, `/v1/score` | reranker, vision_reranker | Non-streaming, 120s timeout |
| `forward_to_path` | `/v1/responses` | llm, vlm | Pure pass-through, no schema mutation |

**Smart fallback:** If a requested model is missing or its type doesn't match the endpoint, the gateway falls back to the first available model of a compatible type — never crossing type boundaries.

## Architecture

```
Client App ──> LLM Gateway (:8000) ──> vLLM Instance A (:8001)  [llama-3.1-8b]
                    │                ──> vLLM Instance B (:8002)  [qwen-2.5-72b]
                    │                ──> vLLM Instance C (:8003)  [qwen-2.5-vl-72b]
                    │                ──> vLLM Instance D (:8004)  [bge-m3]
                    │                ──> ...
                    │
                    ├── Auth (API key validation)
                    ├── Routing (model name -> vLLM instance)
                    ├── Usage logging (tokens + cost)
                    └── Health monitoring
```

## Tech Stack

- **FastAPI** (async ASGI)
- **HTTPX** (connection-pooled async client)
- **SQLModel** (SQLite / PostgreSQL)
- **PyJWT** (RS256 JWT verification)
- **Jinja2** + **Tailwind CSS** (CDN) + **Chart.js**
- **Downstream:** [vLLM](https://github.com/vllm-project/vllm) (OpenAI-compatible serving)
- **Auth:** [AuthCenter](https://github.com/bwinken/authcenter) (OAuth2 SSO)
