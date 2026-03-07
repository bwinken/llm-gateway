# LLM Gateway

OpenAI-compatible reverse proxy gateway for [vLLM](https://github.com/vllm-project/vllm) serving clusters.
Routes, proxies, and monitors traffic from client applications to downstream vLLM instances (LLM, VLM, Embedding, Reranker).

```
Client App ──▶ LLM Gateway (:8050) ──▶ vLLM Instance A  [LLM]
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

## Features

- **OpenAI-compatible API** — `/v1/chat/completions`, `/v1/embeddings`, `/v1/rerank`, `/v1/score`, `/v1/responses`, `/v1/models`
- **Multi-model routing** — LLM, VLM, Embedding, Vision Embedding, Reranker, Vision Reranker
- **SSE streaming** — Full Server-Sent Events support for chat completions and responses
- **Smart fallback** — Health-check-aware fallback to compatible models; `X-Model-Fallback` response header
- **Tiered pricing** — Per-type input/output token pricing with automatic cost calculation
- **Usage tracking** — Per-user token and cost logging to PostgreSQL
- **OAuth2 SSO** — [AuthCenter](https://github.com/bwinken/authcenter) integration with RS256 JWT, auto-provisioning users
- **Dual auth** — API key (Bearer token) for SDK/API, OAuth2 session for web UI
- **Web dashboard** — Usage stats, Chart.js trend charts, grouped server health status
- **Admin panel** — User management, leaderboards, daily limit control
- **Background health checks** — Periodic pings to all downstream servers

## Tech Stack

| Component | Technology |
|---|---|
| Framework | FastAPI (async ASGI) |
| HTTP Client | HTTPX (connection-pooled) |
| Database | PostgreSQL + SQLModel |
| Auth | PyJWT (RS256), [AuthCenter](https://github.com/bwinken/authcenter) OAuth2 |
| Frontend | Jinja2 + Tailwind CSS (CDN) + Chart.js |
| Downstream | [vLLM](https://github.com/vllm-project/vllm) (OpenAI-compatible) |

---

## Quick Start (Development)

### 1. Clone and install

```bash
git clone https://github.com/bwinken/llm-gateway.git
cd llm-gateway
pip install -r requirements.txt
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
base_url = "http://192.168.1.100:8000/v1"
api_key = "token-abc123"
```

Edit `.env` — set secrets and database URL:

```env
SECRET_KEY=your-random-secret-key
DATABASE_URL=postgresql://llm_gateway:your_password@localhost:5432/llm_gateway
```

### 3. Start PostgreSQL

```bash
docker compose up -d postgres
```

### 4. Set up AuthCenter public key

Place the AuthCenter RS256 public key at `keys/public.pem` (or change `AUTH_CENTER_PUBLIC_KEY_PATH` in `.env`).

### 5. Run

```bash
fastapi dev app/main.py
```

The gateway starts at `http://localhost:8050`.

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

### .env

| Variable | Description | Default |
|---|---|---|
| `SECRET_KEY` | Session cookie encryption | `change-me` |
| `DATABASE_URL` | PostgreSQL connection string | `sqlite:///./llm_gateway.db` |
| `AUTH_CENTER_BASE_URL` | AuthCenter server URL | `http://localhost:8000` |
| `AUTH_CENTER_APP_ID` | OAuth2 application ID | `llm_gateway` |
| `AUTH_CENTER_CLIENT_SECRET` | OAuth2 client secret | `change-me` |
| `AUTH_CENTER_REDIRECT_URI` | OAuth2 callback URL | `http://localhost:8050/auth/callback` |
| `AUTH_CENTER_PUBLIC_KEY_PATH` | RS256 public key path | `./keys/public.pem` |

---

## API Usage

All API endpoints require `Authorization: Bearer <api_key>`.

### Chat Completions

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://your-gateway:8050/v1",
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
curl http://your-gateway:8050/v1/rerank \
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
curl http://your-gateway:8050/v1/models \
  -H "Authorization: Bearer sk-your-api-key"
```

### Web Dashboard

Open `http://your-gateway:8050` in browser. Redirects to AuthCenter for SSO login. Admin features require `admin` scope in AuthCenter.

---

## Deployment

### Prerequisites

- PostgreSQL (via Docker or system package)
- AuthCenter RS256 public key at `keys/public.pem`
- Nginx (system-level, managed by `systemctl`)

---

### Method 1: systemd user service + Nginx

Runs the gateway as a user-level systemd service. No root required for the app process.

#### Step 1: Clone and install

```bash
cd ~
git clone https://github.com/bwinken/llm-gateway.git
cd llm-gateway
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

#### Step 2: Configure

```bash
cp config.toml.example config.toml
cp .env.example .env
```

Edit `config.toml` with your vLLM server addresses and API keys.

Edit `.env`:

```env
SECRET_KEY=<random-secret>
DATABASE_URL=postgresql://llm_gateway:your_password@localhost:5432/llm_gateway
AUTH_CENTER_BASE_URL=https://your-authcenter.example.com
AUTH_CENTER_CLIENT_SECRET=<your-secret>
AUTH_CENTER_REDIRECT_URI=https://llm-gateway.example.com/auth/callback
AUTH_CENTER_PUBLIC_KEY_PATH=./keys/public.pem
```

Place the AuthCenter public key:

```bash
mkdir -p keys
cp /path/to/public.pem keys/public.pem
```

#### Step 3: Start PostgreSQL

```bash
docker compose up -d postgres
```

Or use an existing PostgreSQL server — just update `DATABASE_URL` in `.env`.

#### Step 4: Create systemd user service

```bash
mkdir -p ~/.config/systemd/user
```

Create `~/.config/systemd/user/llm-gateway.service`:

```ini
[Unit]
Description=LLM Gateway
After=network.target

[Service]
Type=simple
WorkingDirectory=%h/llm-gateway
Environment=PYTHONUTF8=1
ExecStart=%h/llm-gateway/venv/bin/fastapi run app/main.py --host 127.0.0.1 --port 8050
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

#### Step 5: Enable and start

```bash
systemctl --user daemon-reload
systemctl --user enable llm-gateway
systemctl --user start llm-gateway
```

Enable lingering so the service runs after logout:

```bash
sudo loginctl enable-linger $USER
```

Check status:

```bash
systemctl --user status llm-gateway
journalctl --user -u llm-gateway -f
```

#### Step 6: Configure Nginx

Create `/etc/nginx/sites-available/llm-gateway`:

```nginx
server {
    listen 80;
    server_name llm-gateway.example.com;

    location / {
        proxy_pass http://127.0.0.1:8050;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE streaming support
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
        chunked_transfer_encoding on;
    }
}
```

Enable and reload:

```bash
sudo ln -s /etc/nginx/sites-available/llm-gateway /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

(Optional) Add HTTPS with certbot:

```bash
sudo certbot --nginx -d llm-gateway.example.com
```

---

### Method 2: Docker + Nginx

Runs the gateway and PostgreSQL together in Docker containers.

#### Step 1: Clone and configure

```bash
git clone https://github.com/bwinken/llm-gateway.git
cd llm-gateway
cp config.toml.example config.toml
cp .env.example .env
```

Edit `config.toml` with your vLLM server addresses and API keys.

Edit `.env`:

```env
SECRET_KEY=<random-secret>
DATABASE_URL=postgresql://llm_gateway:your_password@postgres:5432/llm_gateway
AUTH_CENTER_BASE_URL=https://your-authcenter.example.com
AUTH_CENTER_CLIENT_SECRET=<your-secret>
AUTH_CENTER_REDIRECT_URI=https://llm-gateway.example.com/auth/callback
AUTH_CENTER_PUBLIC_KEY_PATH=./keys/public.pem
```

> **Note:** `DATABASE_URL` uses `postgres` (the Docker service name) instead of `localhost`.

Place the AuthCenter public key:

```bash
mkdir -p keys
cp /path/to/public.pem keys/public.pem
```

#### Step 2: Build and start

```bash
docker compose up -d --build
```

This starts both PostgreSQL and the gateway. The gateway listens on port `8050`.

Check logs:

```bash
docker compose logs -f gateway
```

#### Step 3: Configure Nginx

Create `/etc/nginx/sites-available/llm-gateway`:

```nginx
server {
    listen 80;
    server_name llm-gateway.example.com;

    location / {
        proxy_pass http://127.0.0.1:8050;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE streaming support
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
        chunked_transfer_encoding on;
    }
}
```

Enable and reload:

```bash
sudo ln -s /etc/nginx/sites-available/llm-gateway /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

(Optional) Add HTTPS:

```bash
sudo certbot --nginx -d llm-gateway.example.com
```

---

### Management Commands

```bash
# View logs (systemd)
journalctl --user -u llm-gateway -f

# View logs (Docker)
docker compose logs -f gateway

# Restart (systemd)
systemctl --user restart llm-gateway

# Restart (Docker)
docker compose restart gateway

# Migrate data from old SQLite DB
python scripts/migrate_sqlite_to_pg.py /path/to/llm_gateway.db
```

---

## Testing

```bash
python -m pytest tests/ -v
```

Tests use in-memory SQLite and mock all downstream calls. No PostgreSQL or vLLM servers required.

---

## Project Structure

```
llm-gateway/
├── config.toml.example        # Model routing + pricing template
├── .env.example                # Environment variables template
├── Dockerfile
├── docker-compose.yml          # Gateway + PostgreSQL
├── requirements.txt
├── scripts/
│   └── migrate_sqlite_to_pg.py
├── app/
│   ├── main.py                 # FastAPI app, lifespan, middleware
│   ├── core/
│   │   ├── config.py           # TOML → MODEL_ROUTING + PRICING_MAP
│   │   ├── database.py         # SQLModel engine + session
│   │   ├── deps.py             # Bearer token auth
│   │   ├── server_state.py     # httpx client + health cache
│   │   └── logger.py
│   ├── models/
│   │   └── schema.py           # User + UsageLog tables
│   ├── routers/
│   │   ├── llm_api.py          # /v1/* API endpoints
│   │   ├── web_ui.py           # Dashboard (Jinja2)
│   │   ├── auth_api.py         # OAuth2 SSO callback
│   │   └── admin.py            # Admin panel + API
│   ├── services/
│   │   ├── proxy.py            # Core proxy + fallback + logging
│   │   ├── stats.py            # Dashboard aggregations
│   │   └── health.py           # Background health loop
│   └── templates/
│       ├── base.html
│       ├── dashboard.html
│       └── admin.html
└── tests/
    ├── conftest.py
    ├── test_chat_completions.py
    ├── test_embeddings.py
    ├── test_rerank_score.py
    ├── test_responses.py
    └── test_vlm.py
```
