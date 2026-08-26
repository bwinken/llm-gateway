# LLM Gateway

[![CI](https://github.com/bwinken/llm-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/bwinken/llm-gateway/actions/workflows/ci.yml)

[中文版](README.zh-TW.md)

OpenAI-compatible reverse proxy gateway for [vLLM](https://github.com/vllm-project/vllm) serving clusters and Azure OpenAI deployments.
Routes, proxies, and monitors traffic from client applications to downstream LLM/VLM/Embedding/Reranker backends.

```
Client App ──▶ LLM Gateway ──▶ /v1/*      ──▶ vLLM Instance A  [LLM]
                    │                          ──▶ vLLM Instance B  [VLM]
                    │                          ──▶ vLLM Instance C  [Embedding]
                    │                          ──▶ vLLM Instance D  [Reranker]
                    │             /azure/v1/* ──▶ Azure OpenAI deployment(s)
                    │
                    ├── Auth (API key / OAuth2 SSO)
                    ├── Routing (model alias → vLLM instance / Azure deployment)
                    ├── Smart fallback (health-aware, vLLM only)
                    ├── Usage logging (tokens + cost; shared across both backends)
                    └── Health monitoring (30s interval, vLLM only)
```

## Screenshots

| Welcome | Dashboard |
|---|---|
| ![Welcome](docs/screenshots/welcome.png) | ![Dashboard](docs/screenshots/dashboard.png) |

| Admin Panel | Model Config |
|---|---|
| ![Admin](docs/screenshots/admin.png) | ![Models](docs/screenshots/models.png) |

## Features

- **Unified `/v1/*` surface** — One base URL exposes both backends. `/v1/chat/completions`, `/v1/messages`, `/v1/messages/count_tokens` dispatch by `model` alias: vLLM by default, Azure when the alias is configured under `[azure_models.*]` AND the caller has `can_use_azure`. `/v1/models` merges Azure aliases in for those callers so Claude Code's model picker shows both backends. Every route is also exposed without the `/v1` prefix (`/chat/completions`, `/messages`, ...) for clients whose base URL omits it. `/v1/chat/completions/render` (on-prem vLLM only) renders a request through the model's chat template and returns the resulting `token_ids` + resolved `sampling_params` **without generating** — a debug aid for seeing exactly what the model receives
- **Anthropic Messages API** — `/v1/messages` and `/v1/messages/count_tokens`, drop-in compatible with the Anthropic Python SDK and Claude Code (works against any vLLM LLM/VLM downstream, and any Azure deployment when the caller has Azure access). Streams `reasoning_content` from `--enable-reasoning` / DeepSeek / Qwen3-thinking as Anthropic `thinking` content blocks, emits SSE `ping` keepalives every 10 s of downstream silence so clients survive long reasoning prefill, and surfaces a mid-stream downstream disconnect as a retryable `overloaded_error` rather than a falsely-complete turn
- **Azure OpenAI backend** — Same client, same API key, same billing. Reachable via the unified `/v1/*` surface above (per-user, gated by `can_use_azure`), or via a dedicated Azure-only surface at `/azure/v1/*` (chat completions, responses, Anthropic Messages, count_tokens — no embeddings; that's vLLM-only by design)
- **Multi-model routing** — LLM, VLM, Embedding, Vision Embedding, Reranker, Vision Reranker
- **SSE streaming** — Full Server-Sent Events support for chat completions and responses
- **Smart fallback** — Configurable per-type fallback model, health-check-aware; `X-Model-Fallback` response header (vLLM path)
- **Tiered pricing** — Per-type defaults plus optional per-model overrides on either vLLM or Azure entries, including a discounted `cached_input_price_per_1m` for prompt-cache hits (Azure cached tokens billed at the lower rate)
- **Usage tracking** — Per-user token and cost logging to PostgreSQL (shared across `/v1/*` and `/azure/v1/*`)
- **OAuth2 SSO** — [AuthCenter](https://github.com/bwinken/authcenter) integration with RS256 JWT, auto-provisioning users with a configurable default daily limit
- **Dual auth** — API key (Bearer token) for SDK/API, oauth2-proxy + JWT for web UI
- **Per-user access control** — Admins can disable a user (rejects both API key and JWT auth with a styled HTML page or JSON 403) and gate Azure / AWS Bedrock deployments behind per-user `can_use_azure` / `can_use_bedrock` flags (admins bypass all)
- **Cost export by backend** — Admin xlsx report includes a **Cost by Backend** sheet, and `GET /admin/api/export/user-backend-costs.csv` exports one row per (month × account) with separate On-prem (vLLM) / Azure / AWS Bedrock cost columns for billing/chargeback
- **Web dashboard** — Usage stats, remaining-quota indicator, Chart.js trend charts, grouped server health status with admin-set model metadata badges (context window, tools, vision, cache) and live per-vLLM-server load (`running N · waiting M`, amber when requests queue, red overload warning); separate Azure access card listing configured Azure aliases when granted
- **Admin panel** — User management with per-row Disable / Enable, Azure, Monitor, and Delete buttons; leaderboards, runtime-adjustable default daily limit, model config UI (routing/pricing/fallback)
- **Health probes** — Unauthenticated `/healthz` (liveness, zero I/O) and `/readyz` (readiness, gated on the database) for load balancers and monitoring
- **Background health checks** — Periodic pings to all downstream vLLM servers, plus a Prometheus `/metrics` scrape of each alive server for live running/waiting request counts shown on the dashboard

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

Edit `config.toml` — set your downstream vLLM server URLs and API keys (and, optionally, Azure OpenAI deployments):

```toml
[app]
default_daily_limit_usd = 10.0   # New users auto-provision with this; admins can change it at runtime

[models.llm."my-model"]
real_model = "Qwen/Qwen2.5-72B"
base_url = "http://your-llm-server:8000/v1"
api_key = "token-abc123"
# Optional per-model pricing override (USD per 1M tokens); falls back to [pricing.llm] then [pricing] defaults
# input_price_per_1m = 0.50
# output_price_per_1m = 1.50

[azure_models."gpt-4o-mini-azure"]
type        = "llm"
endpoint    = "https://my-resource.openai.azure.com"
deployment  = "gpt-4o-mini"
api_key     = "azure-key-here"
api_version = "2024-08-01-preview"
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

Lookup order for cost calculation: per-model `input_price_per_1m` / `output_price_per_1m` on the model entry → per-type `[pricing.<type>]` → top-level `[pricing]` defaults. A model entry may also set `cached_input_price_per_1m`; when present, cache-hit input tokens reported by the backend (e.g. Azure's `prompt_tokens_details.cached_tokens`) are billed at that discounted rate while uncached input bills at the full price.

Per-type fallback model (optional, vLLM path only). When a model's server is down, prefer this model as fallback:

```toml
[fallback]
llm = "backup-llm"
vlm = "backup-vlm"
```

Azure OpenAI deployments (optional). Each entry exposes a deployment as a model alias served from `/azure/v1/*`:

```toml
[azure_models."gpt-4o-mini-azure"]
type        = "llm"          # llm | vlm | embedding
endpoint    = "https://my-resource.openai.azure.com"
deployment  = "gpt-4o-mini"
api_key     = "azure-key"
api_version = "2024-08-01-preview"
```

AWS Bedrock models (optional). Each entry exposes a Bedrock model as an alias served from `/aws/v1/*` (all families go through the Converse API; auth is a long-term Bedrock API key sent as a Bearer token):

```toml
[bedrock_models."claude-sonnet-bedrock"]
type         = "llm"          # llm | vlm
region       = "us-east-1"
model_id     = "anthropic.claude-sonnet-4-20250514-v1:0"   # or an inference-profile ID
api_key      = "bedrock-api-key"
is_reasoning = true           # enables reasoning-effort → extended-thinking translation
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
| `AZURE_HTTP_PROXY` | Optional HTTP proxy for `/azure/v1/*` downstream traffic only; supports inline credentials (`http://user:pass@proxy:8080`). Unset = Azure reached directly. vLLM traffic is never proxied. | _(unset)_ |
| `BEDROCK_HTTP_PROXY` | Optional HTTP proxy for `/aws/v1/*` (Bedrock) downstream traffic only; same contract as `AZURE_HTTP_PROXY`. | _(unset)_ |
| `BEDROCK_INSECURE` | When `true`, disable TLS verification on the Bedrock-bound client (corporate TLS-inspecting proxy); same contract as `AZURE_INSECURE`. | `false` |
| `LANGFUSE_HOST` | Langfuse base URL (self-hosted recommended). Observability is **enabled only when HOST + PUBLIC_KEY + SECRET_KEY are all set**; otherwise it is a no-op with zero overhead. | _(unset)_ |
| `LANGFUSE_PUBLIC_KEY` | Langfuse public key (`pk-lf-…`). | _(unset)_ |
| `LANGFUSE_SECRET_KEY` | Langfuse secret key (`sk-lf-…`). | _(unset)_ |
| `LANGFUSE_CAPTURE_IO` | When `true`, also send prompt/response **content** to Langfuse (Phase 2). PII-sensitive — see Observability below. Default off sends metrics only. | `false` |
| `LANGFUSE_SAMPLE_RATE` | Fraction of billable requests recorded in Langfuse, `0.0`–`1.0` (e.g. `0.1` ≈ 10%). `0.0` records nothing; out-of-range values are clamped, invalid values fall back to `1.0`. Billing in `usage_logs` is unaffected — every request is still billed. | `1.0` |

> OAuth2 login settings (OIDC issuer, client secret, redirect URL) are configured in `deploy/.env` for oauth2-proxy. See [deploy/README.md](deploy/README.md).

### Observability (Langfuse)

Optional. When `LANGFUSE_HOST` + `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` are all set, the gateway emits one Langfuse **generation** per billable request — non-blocking, errors swallowed, and a complete no-op when unset.

- **Metrics (always, no PII):** user, model alias, endpoint, token usage, cost (computed by the gateway — Langfuse is **not** asked to re-price), latency, plus categorical scores for **client software** (`claude-code` / `roo-code` / `openai-compatible` / …, derived from `User-Agent` + endpoint), `empty_turn`, and `fallback_used`. This powers per-user / per-model / per-client analytics (filter by user in the Users view; group by model or `client` score; chart by day/month).
- **Content (opt-in, PII):** set `LANGFUSE_CAPTURE_IO=true` to also attach the request messages and assistant response to each generation (chat / messages / responses, vLLM + Azure; embeddings/rerank/score are metrics-only by design). **Governance:** capturing individual users' prompts is individual-level monitoring — restrict Langfuse project access and confirm notice/consent before enabling in production.
- **Version note:** built on the Langfuse Python SDK v4 (OTel-based); confirm SDK ↔ your Langfuse server version compatibility before rollout.

See [docs/langfuse-observability.md](docs/langfuse-observability.md) for the full design.

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

### Anthropic Messages API

`/v1/messages` accepts Anthropic-format requests and translates them on the fly to whichever backend the requested `model` maps to: vLLM by default (translated to OpenAI chat completions for any LLM/VLM downstream), or Azure OpenAI when the alias is configured under `[azure_models.*]` AND the caller has `can_use_azure` (translated to Azure Responses API). Supports streaming, tool use, and vision regardless of backend.

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://your-gateway",
    api_key="sk-your-api-key",  # the gateway API key, not Anthropic's
)

resp = client.messages.create(
    model="my-model",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}],
)
```

**Claude Code support.** Point Claude Code at the gateway to use any local LLM as the backend:

```bash
ANTHROPIC_BASE_URL=http://your-gateway \
ANTHROPIC_AUTH_TOKEN=sk-your-api-key \
claude
```

The adapter handles tool calls (`tool_use` ↔ OpenAI `tool_calls`), images, system prompts, stop reason mapping, and streaming SSE event sequencing. Downstream `reasoning_content` (vLLM `--enable-reasoning`, DeepSeek, Qwen3-thinking, etc.) is surfaced as Anthropic `thinking` content blocks — `thinking_delta` events on the stream, a `thinking` block prepended on non-stream responses. The translation is symmetric: `thinking` blocks in an assistant message's history are carried back downstream as `reasoning_content`, so reasoning is preserved across multi-turn conversations rather than dropped. For models marked `is_reasoning = true` in `config.toml`, the adapter also translates the Anthropic reasoning preference (an `effort` string or a `thinking` token budget) into OpenAI's `reasoning_effort`; non-reasoning models never receive it. While the downstream is silent (long reasoning prefill, queued batch, slow header turnaround) the gateway emits an Anthropic `event: ping` every 10 seconds so Claude Code does not time the connection out. If a streaming downstream disconnects mid-generation without ever sending a finish reason, the gateway emits a retryable `overloaded_error` instead of a normal `message_stop`, so the client retries rather than treating the truncated turn as complete. `/v1/messages/count_tokens` is forwarded to the downstream tokenizer so Claude Code's context-window indicator stays accurate.

### List Models

```bash
curl http://your-gateway/v1/models \
  -H "Authorization: Bearer sk-your-api-key"
```

### Azure OpenAI

Azure-backed deployments configured under `[azure_models.*]` are reachable two ways with the same gateway API key:

1. **Through the unified `/v1/*` surface** (recommended for users with `can_use_azure`) — pick the Azure alias by name from `/v1/models` and the gateway dispatches to Azure automatically. One base URL, both backends.
2. **Through the dedicated `/azure/v1/*` surface** — for clients that should only ever see Azure deployments (e.g. an OpenAI-shaped client whose `base_url` you want pinned to Azure).

Azure aliases appear on `/v1/models` only for users with `can_use_azure` (admins bypass) — that's how a single Claude Code base URL surfaces both backends without leaking Azure deployments to users who don't have access.

### AWS Bedrock

Bedrock models configured under `[bedrock_models.*]` follow the exact same pattern with the `/aws` prefix: reachable through the unified `/v1/*` surface (gated per-user by `can_use_bedrock`, merged into `/v1/models`) or through the dedicated `/aws/v1/*` surface (`/aws/v1/chat/completions`, `/aws/v1/messages`, `/aws/v1/messages/count_tokens`, `/aws/v1/models`). All model families (Anthropic, Nova, Llama, Mistral, …) are served via Bedrock's Converse API, so OpenAI-shaped and Anthropic-shaped clients both work against any of them, streaming included. An optional per-user `bedrock_daily_limit_usd` caps the Bedrock share of daily spend, mirroring the Azure sub-limit. Downstream auth uses a long-term Bedrock API key (`Authorization: Bearer`); IAM/SigV4 is not supported yet.

```python
# Option 1: unified base URL (sees both vLLM and Azure aliases when can_use_azure)
client = OpenAI(
    base_url="http://your-gateway/v1",
    api_key="sk-your-api-key",
)
resp = client.chat.completions.create(
    model="gpt-4o-mini-azure",   # alias from [azure_models.<alias>]
    messages=[{"role": "user", "content": "Hello!"}],
)

# Option 2: Azure-only base URL
client = OpenAI(
    base_url="http://your-gateway/azure/v1",
    api_key="sk-your-api-key",
)
```

Anthropic SDK / Claude Code can do the same — either point at the unified surface and pick an Azure alias, or pin to `/azure`:

```bash
# Unified — model picker shows vLLM + Azure aliases (when can_use_azure)
ANTHROPIC_BASE_URL=http://your-gateway \
ANTHROPIC_AUTH_TOKEN=sk-your-api-key \
claude

# Azure-only
ANTHROPIC_BASE_URL=http://your-gateway/azure \
ANTHROPIC_AUTH_TOKEN=sk-your-api-key \
claude
```

For clients that natively speak the Azure Responses API and want Responses-specific features (`previous_response_id`, `store: true`, reasoning items in `input`, etc.) there's also a direct pass-through at `/azure/v1/responses`. Body is forwarded as-is; the gateway only rewrites `body.model` from the configured alias to the Azure deployment name.

```python
import httpx
resp = httpx.post(
    "http://your-gateway/azure/v1/responses",
    headers={"Authorization": "Bearer sk-your-api-key"},
    json={"model": "gpt-4o-mini-azure", "input": "Hello"},
)
```

### Client configuration recommendations

The two paths (vLLM `/v1/*` vs Azure `/azure/v1/*`) have different tool-calling strictness, so the right client setup depends on which backend you're targeting.

#### vLLM path (`/v1/*`) — lenient, but the model itself must support tool calling

vLLM downstream calls are a straight chat-completions pass-through; the gateway does not validate tool-call/tool-result pairing. Whatever the client sends gets fed to the model and the model interprets it. This path tolerates mixed-style histories.

What it does require is a **model that supports native function calling** if you want structured tool calls — Qwen 2.5, Llama 3.1+, Hermes, Mistral Large, etc. For a plain chat model that can't emit `tool_calls`, you'll have to rely on the XML-inline style.

| Client | Model supports native function calling | Model doesn't support it |
|---|---|---|
| **Roo Code** | **OpenAI** provider + Base URL = `http://your-gateway/v1` | **OpenAI Compatible** provider + same base URL |
| **Cline / Continue.dev / Cursor** | OpenAI provider + same as above | Most lack an XML fallback — make sure the model supports tool calling |
| **Claude Code** | `ANTHROPIC_BASE_URL=http://your-gateway` against `/v1/messages` | Same; vLLM path doesn't enforce pairing either way |

#### Azure path (`/azure/v1/*`) — strict, clients cannot mix styles

Every Azure call from the gateway is translated to the **Responses API** (`/openai/v1/responses`), which strictly validates tool-call/tool-result pairing — every `function_call` must have a matching `function_call_output`. Most well-behaved clients obey this automatically, but a few have modes that mix structured tool calls with inline text results in the same conversation, and Azure 400s on the mismatch.

Misconfiguration shows up in the log as `Dropping N orphan function_call(s)` WARNINGs and falls back to a safety-net degradation so the conversation can still progress — but **the right config is the durable fix**.

| Client | Recommended setup | Gateway endpoint | Notes |
|---|---|---|---|
| **Claude Code** | `ANTHROPIC_BASE_URL=http://your-gateway/azure` | `/azure/v1/messages` | Anthropic-native format; every `tool_use` is strictly paired with a `tool_result` |
| **Anthropic Python SDK** | `Anthropic(base_url="http://your-gateway/azure")` | `/azure/v1/messages` | Same as above |
| **Roo Code "Anthropic" provider** | API base URL pointing at the gateway | `/azure/v1/messages` | Roo Code in Anthropic mode follows strict `tool_use`/`tool_result` pairing |
| **Roo Code "OpenAI" provider** (recommended) | Base URL = `http://your-gateway/azure/v1`, Custom Model ID = the configured alias | `/azure/v1/chat/completions` | Standard OpenAI shape with strict `tool_calls`/`role:"tool"` pairing — **this is the recommended setup for Roo Code against Azure** |
| **Roo Code "OpenAI Compatible"** | ⚠️ **avoid** | — | This mode mixes native `tool_calls` with inline `<environment_details>` text results in the next user message. Azure Responses API does not accept the mixed shape |
| **Cursor / Continue.dev** | `base_url=http://your-gateway/azure/v1` | `/azure/v1/chat/completions` | Standard OpenAI shape |
| **OpenAI Python SDK** | `OpenAI(base_url="http://your-gateway/azure/v1")` | `/azure/v1/chat/completions` | Same as above |
| **OpenAI Python SDK 1.40+ Responses API** | `OpenAI(base_url="http://your-gateway/azure/v1").responses.create(...)` | `/azure/v1/responses` | Direct Responses pass-through; use when you need `previous_response_id`, `store: true`, or other Responses-only features. Sampling params are not stripped on this path — the client owns them. |

#### Rule of thumb

- **Anthropic-flavour client → `/v1/messages` or `/azure/v1/messages`** (goes through the Anthropic Messages translator)
- **OpenAI-flavour client → `/v1/chat/completions` or `/azure/v1/chat/completions`** (goes through the OpenAI Chat Completions translator)
- **On the Azure path, do not use any "mixed-style" mode** (Roo Code's "OpenAI Compatible" is the headline example) — pick one tool-calling style and stick with it.
- **The vLLM path tolerates mixed styles**, but only if the model and client agree on which tool-calling mechanism they're using.

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
uv run pytest tests/ -v      # tests
uv run ruff check .          # lint
uv run ruff check . --fix    # lint + autofix
```

Tests use in-memory SQLite and mock all downstream calls. No PostgreSQL or vLLM servers required.
`config.toml` is not required either — when it is absent (fresh clone, CI checkout) `app/core/config.py` falls back to `config.toml.example`.

### CI

`.github/workflows/ci.yml` runs the linter and the full test suite on every push to `main` and on every pull request (GitHub-hosted `ubuntu-latest`, Python 3.11, dependencies from `uv.lock` via `uv sync --locked`). A pull request that leaves `uv.lock` out of sync with `pyproject.toml` fails there rather than drifting silently.

---

## Project Structure

```
llm-gateway/
├── config.toml.example        # Model routing + pricing template
├── .env.example                # Environment variables template
├── pyproject.toml              # Dependencies, ruff config (uv)
├── uv.lock                     # Locked dependency versions
├── alembic.ini                 # Alembic migration config
├── .github/workflows/ci.yml    # CI: ruff + pytest on push / PR
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
│   │   ├── v1_api.py           # /v1/* unified public API (vLLM default + Azure dispatch)
│   │   ├── azure_api.py        # /azure/v1/* Azure-only API
│   │   ├── health_api.py       # /healthz + /readyz probes (no auth)
│   │   ├── web_ui.py           # Dashboard (Jinja2)
│   │   └── admin.py            # Admin panel + API
│   ├── services/
│   │   ├── vllm_proxy.py       # vLLM proxy + fallback + logging
│   │   ├── azure_proxy.py      # Azure OpenAI proxy (shares auth/billing/observability)
│   │   ├── anthropic_adapter.py # Anthropic Messages translation (used by both backends)
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
