# LLM Gateway

[![CI](https://github.com/bwinken/llm-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/bwinken/llm-gateway/actions/workflows/ci.yml)

[中文版](README.zh-TW.md)

One OpenAI- and Anthropic-compatible endpoint in front of your on-prem [vLLM](https://github.com/vllm-project/vllm) servers, Azure OpenAI, and AWS Bedrock, with per-user API keys, budgets, and usage billing.

```
                                  ┌──▶ vLLM servers   (LLM, VLM, embedding, reranker, System One)
Clients ──▶ LLM Gateway ──────────┼──▶ Azure OpenAI   (per-user access)
 (OpenAI SDK, Anthropic SDK,      └──▶ AWS Bedrock    (per-user access)
  Claude Code, Roo Code, …)
              │
              ├── auth: API keys for clients, SSO for the web UI
              ├── routing by model name, with health-aware fallback
              ├── per-user daily budgets and concurrency limits
              └── usage and cost logging, web dashboard, admin panel
```

| Dashboard | Admin Panel |
|---|---|
| ![Dashboard](docs/screenshots/dashboard.png) | ![Admin](docs/screenshots/admin.png) |

## Features

- **One base URL for every backend.** `/v1/*` serves vLLM, Azure, and Bedrock models side by side; the model name picks the backend. Dedicated `/azure/v1/*` and `/aws/v1/*` surfaces exist for clients that must stay on one backend.
- **OpenAI and Anthropic APIs.** Chat completions, Responses, Messages (works with Claude Code), embeddings, rerank, and token counting, streaming included, on every backend that supports them.
- **Reliable under load.** Health checks every 30 seconds, automatic failover to another server of the same model type, and keepalive pings so long reasoning turns don't time out.
- **Cost control.** Per-user daily budgets with optional Azure and Bedrock sub-limits, per-user concurrency limits, and per-model pricing (including prompt-cache discounts).
- **Access control.** Disable accounts, and grant Azure or Bedrock access per user.
- **Web UI.** Users see their usage, remaining budget, and live server load. Admins manage users, models, pricing, and limits without editing files, and can export monthly cost reports.
- **Observability.** Optional [Langfuse](https://langfuse.com) tracing per request, plus `/healthz` and `/readyz` probes for load balancers.

## Quick start

Requires Python 3.11, [uv](https://docs.astral.sh/uv/), and Docker (for the dev database).

```bash
git clone https://github.com/bwinken/llm-gateway.git && cd llm-gateway
uv sync
cp config.toml.example config.toml    # set your model servers here
cp .env.example .env                  # set DATABASE_URL and AUTH_* here
bash scripts/start-pg-dev.sh start    # dev PostgreSQL in Docker
uv run alembic upgrade head           # create the schema
uv run fastapi dev app/main.py
```

Put your [AuthCenter](https://github.com/bwinken/authcenter) RS256 public key at `keys/public.pem` (or set `AUTH_CENTER_PUBLIC_KEY_PATH`) for web-UI login. On Windows, set `PYTHONUTF8=1` if you see `UnicodeEncodeError`.

## Configuration

### `config.toml`: models and pricing

A minimal model entry:

```toml
[models.llm."my-model"]                # type: llm | vlm | embedding | vision_embedding | reranker | vision_reranker | systemone
base_url   = "http://vllm-host:8000/v1"
real_model = "Qwen/Qwen3-32B"          # name the vLLM server knows the model by
api_key    = ""                        # vLLM --api-key, if set
```

| Section | Purpose |
|---|---|
| `[app]` | Default daily budget for new users and other runtime settings. Most are editable from the admin panel. |
| `[models.<type>.<alias>]` | On-prem vLLM models. Optional: per-model pricing, display metadata, `hidden`, reasoning-effort rules. |
| `[azure_models.<alias>]` | Azure OpenAI deployments (`endpoint`, `deployment`, `api_key`). |
| `[bedrock_models.<alias>]` | AWS Bedrock models (`region`, `model_id`, long-term Bedrock API key). |
| `[pricing]`, `[pricing.<type>]` | Default and per-type prices in USD per 1M tokens. A model's own price fields override them. |
| `[fallback]`, `[azure_fallback]`, `[bedrock_fallback]` | Which model to use when a requested one is unknown or down. |

Model names must be unique across all three backends. [`config.toml.example`](config.toml.example) documents every key. Models, pricing, and fallbacks can also be edited from **Admin → Model Config**, which writes `config.toml` for you.

### `.env`: environment

| Variable | Purpose | Default |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection string | `postgresql://llm_gateway:password@localhost:5432/llm_gateway` |
| `AUTH_BASE_URL`, `AUTH_CENTER_APP_ID`, `AUTH_CENTER_PUBLIC_KEY_PATH` | JWT issuer, audience, and public key for web-UI login | `auth-center`, `llm_gateway`, `./keys/public.pem` |
| `APP_TITLE` | Name shown in the UI and logs | `LLM Gateway` |
| `AZURE_HTTP_PROXY`, `BEDROCK_HTTP_PROXY` | HTTP proxy for cloud traffic only (vLLM traffic is never proxied) | unset |
| `AZURE_INSECURE`, `BEDROCK_INSECURE` | Skip TLS verification for that cloud (for TLS-inspecting corporate proxies) | `false` |
| `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Enable Langfuse tracing (all three required) | unset |
| `LANGFUSE_CAPTURE_IO` | Also send prompt and response **content** to Langfuse. Contains personal data, so confirm notice and consent first. | `false` |
| `LANGFUSE_SAMPLE_RATE` | Fraction of requests traced, `0.0`–`1.0` (billing always records every request) | `1.0` |
| `ENFORCE_DAILY_LIMIT` | `false` logs budget overruns instead of rejecting them | `true` |
| `SLOW_REQUEST_WARN_S` | Log a warning for requests slower than this many seconds (`0` turns it off) | `120` |
| `LOG_DIR`, `LOG_LEVEL`, `LOG_ROTATION`, `LOG_RETENTION` | Log files, one per worker | `./logs`, `WARNING`, `100 MB`, `14 days` |
| `LOG_REQUEST_BODIES` | Log raw request bodies on downstream errors. Debugging only: these contain user prompts. | `false` |
| `DOWNSTREAM_MAX_CONNECTIONS` | HTTP connection pool size. Each open stream holds one connection. | `1000` |

SSO login itself (OIDC issuer, client secret) is configured for oauth2-proxy in `deploy/.env`; see [deploy/README.md](deploy/README.md).

## Using the gateway

Clients authenticate with their gateway API key (shown on the dashboard).

```python
from openai import OpenAI

client = OpenAI(base_url="http://your-gateway/v1", api_key="sk-your-api-key")
client.chat.completions.create(model="my-model", messages=[{"role": "user", "content": "Hello!"}])
```

```bash
# Claude Code
ANTHROPIC_BASE_URL=http://your-gateway ANTHROPIC_AUTH_TOKEN=sk-your-api-key claude
```

The **[Client Guide](docs/client-guide.md)** covers the rest: every endpoint, Azure and Bedrock access, tool-calling setup for Roo Code, Cursor and others, the error responses clients should handle, and System One.

## Administration

Open `http://your-gateway` and sign in through SSO. Accounts with the `admin` scope in AuthCenter see the admin panel, where they can:

- set each user's daily budget, plus optional Azure and Bedrock sub-limits;
- grant Azure or Bedrock access, or disable an account;
- set the per-user **concurrency limit** (Off, Monitor, or Enforce) and waive it for individual accounts (app accounts start out waived);
- choose whether a spent cloud sub-limit falls back to on-prem models instead of returning 429;
- edit models, pricing, and fallbacks under **Model Config**;
- export monthly cost reports (xlsx, or CSV split by backend) and review usage anomalies.

## Deployment

Production runs as a user-level systemd service behind nginx and oauth2-proxy, with PostgreSQL in Docker. See [deploy/README.md](deploy/README.md).

On upgrade, apply schema changes before restarting:

```bash
uv run alembic upgrade head
```

Moving from the old SQLite database? See the [migration guide](docs/migration-guide.md).

## Development

```bash
uv run pytest tests/ -q     # tests: in-memory SQLite, mocked downstreams, no config.toml needed
uv run ruff check .         # lint
```

CI runs both on every push to `main` and every pull request. [CLAUDE.md](CLAUDE.md) is the contributor map: architecture, conventions, and the rules that are easy to break.

| Docs | |
|---|---|
| [Client Guide](docs/client-guide.md) | Connecting clients, endpoints, error handling |
| [Architecture](docs/architecture.md) | How every subsystem behaves, in detail |
| [deploy/](deploy/README.md) | Production deployment |
| [Langfuse](docs/langfuse-observability.md) | Observability design |
| [alembic/](alembic/README.md) | Schema migrations |
| [app/core](app/core/README.md), [routers](app/routers/README.md), [models](app/models/README.md), [templates](app/templates/README.md), [tests](tests/README.md) | Per-package notes |
