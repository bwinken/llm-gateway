# LLM Gateway

[![CI](https://github.com/bwinken/llm-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/bwinken/llm-gateway/actions/workflows/ci.yml)

[中文版](README.zh-TW.md)

One OpenAI- and Anthropic-compatible endpoint in front of your on-prem [vLLM](https://github.com/vllm-project/vllm) servers, Azure OpenAI and AWS Bedrock. Every user gets their own API key, a daily budget and a usage dashboard.

```
                                  ┌──▶ vLLM servers   (LLM, VLM, embedding, reranker, System One)
Clients ──▶ LLM Gateway ──────────┼──▶ Azure OpenAI   (per-user access)
 (OpenAI SDK, Anthropic SDK,      └──▶ AWS Bedrock    (per-user access)
  Claude Code, Roo Code, …)
```

| Dashboard: budget, usage, live model status | Admin panel: spend, accounts, limits |
|---|---|
| ![Dashboard](docs/screenshots/dashboard.png) | ![Admin panel](docs/screenshots/admin.png) |

## What it does

- **One base URL for every backend.** Clients call `/v1/*`; the model name picks vLLM, Azure or Bedrock.
- **OpenAI and Anthropic APIs.** Chat completions, Responses, Messages (works with Claude Code), embeddings and rerank, with streaming.
- **Stays up.** Health checks, automatic failover to another server of the same type, and keepalive pings for long reasoning turns.
- **Cost control.** Daily budget per user, a separate cap on cloud spend, concurrency limits, per-model pricing.
- **Access and security.** Cloud access per user, an optional IP allowlist, prompt screening (guardrail), and an audit log of every admin action.
- **Weekly reports by mail.** Tokens, estimated cost, latency and error rate per backend; guardrail verdicts and the accounts worth a look. Each report has its own day, time and recipients.
- **Helps users fix their setup.** Detects misconfigured Claude Code clients and gives each user one script that fixes their settings, on the dashboard and by mail.
- **Web UI for everyone.** Users see their budget, usage and model status. Admins manage accounts, models and prices without editing files.

## Quick start

You need Python 3.11, [uv](https://docs.astral.sh/uv/) and Docker (for the dev database).

```bash
git clone https://github.com/bwinken/llm-gateway.git && cd llm-gateway
uv sync
cp config.toml.example config.toml    # your model servers
cp .env.example .env                  # DATABASE_URL and AUTH_*
bash scripts/start-pg-dev.sh start    # dev PostgreSQL in Docker
uv run alembic upgrade head           # create the schema
uv run fastapi dev app/main.py
```

For web-UI login, put your [AuthCenter](https://github.com/bwinken/authcenter) RS256 public key at `keys/public.pem`. On Windows, set `PYTHONUTF8=1` if you see `UnicodeEncodeError`.

## Use it

Each user's API key is on their dashboard.

```python
from openai import OpenAI

client = OpenAI(base_url="https://your-gateway/v1", api_key="sk-your-api-key")
client.chat.completions.create(model="my-model", messages=[{"role": "user", "content": "Hello!"}])
```

```bash
# Claude Code
ANTHROPIC_BASE_URL=https://your-gateway ANTHROPIC_AUTH_TOKEN=sk-your-api-key claude
```

The **[Client Guide](docs/client-guide.md)** has every endpoint, Azure and Bedrock access, setup for Roo Code, Cursor and others, and the errors a client should handle.

## Configure it

There are two files. Most day-to-day settings can also be changed in the admin panel.

**`config.toml`: models and prices.** A minimal model:

```toml
[models.llm."my-model"]                # llm | vlm | embedding | reranker | ...
base_url   = "http://vllm-host:8000/v1"
real_model = "Qwen/Qwen3-32B"          # the name the vLLM server uses
```

Azure and Bedrock models go in `[azure_models.*]` and `[bedrock_models.*]`; prices in `[pricing]`; what to use when a model is unknown or down in `[fallback]`. Model names must be unique across all backends. [`config.toml.example`](config.toml.example) documents every key, and **Admin → Models** edits the file for you.

**`.env`: connections and secrets.** The ones you are most likely to set (every variable is listed under [Environment variables](#environment-variables)):

| Variable | What it is for |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `AUTH_BASE_URL`, `AUTH_CENTER_APP_ID`, `AUTH_CENTER_PUBLIC_KEY_PATH` | Web-UI login: JWT issuer, audience and public key |
| `AZURE_HTTP_PROXY`, `BEDROCK_HTTP_PROXY` | HTTP proxy for cloud traffic (on-prem traffic is never proxied) |
| `BEDROCK_REGION`, `BEDROCK_AWS_ACCESS_KEY_ID`, `BEDROCK_AWS_SECRET_ACCESS_KEY` | AWS settings shared by every Bedrock model |
| `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Turn on [Langfuse](https://langfuse.com) tracing (the weekly reports need it) |
| `METRICS_ENABLED` | Serve Prometheus metrics at `/metrics` |
| `EXCHANGE_*`, `COMPANY_EMAIL_DOMAIN`, `PUBLIC_BASE_URL` | Mail: to users about their client setup, and the weekly reports |
| `USAGE_LOG_RETENTION_DAYS` | Delete raw usage rows older than this (off by default; reports keep their totals) |

[`.env.example`](.env.example) lists every variable with its default: logging, the failed-login lockout, connection pools, job intervals and mail pacing. SSO itself (OIDC issuer, client secret) is configured for oauth2-proxy in `deploy/.env`; see [deploy/README.md](deploy/README.md).

## Run it as an admin

Open `https://your-gateway` and sign in through SSO. Accounts with the `admin` scope in AuthCenter get the admin panel:

| Tab | What you do there |
|---|---|
| **Overview** | See spend, requests and active users; review usage anomalies; export monthly cost reports |
| **Users** | Set budgets and cloud caps, grant Azure or AWS access, disable accounts, create service accounts, view or regenerate keys |
| **Clients** | Choose which client problems to detect, how users are told, and the settings patch they get; see who is affected and what mail went out |
| **Security** | IP allowlist, prompt guardrail, audit log |
| **Settings** | Default budgets, concurrency limit, what happens when the cloud cap is spent, weekly report mails, site links |
| **Models** | Add and edit models, prices and fallbacks; test a connection |

## Features and what each needs

Everything the gateway does, where it is switched on, and what it needs in `.env`. "Always on" needs nothing beyond the required settings (`DATABASE_URL` and `AUTH_*`). In the last column, a variable is required for that feature unless it says *optional*; [Environment variables](#environment-variables) below marks each one as required or optional.

**API and routing**

| Feature | Switched on by | `.env` |
|---|---|---|
| One `/v1/*` endpoint for vLLM, Azure and Bedrock; the model name picks the backend | Always on; models in `config.toml` or **Admin → Models** | – |
| OpenAI and Anthropic APIs: chat completions, Responses, Messages, embeddings, rerank / score, tokenize / count_tokens, with streaming | Always on | – |
| Azure OpenAI models | `[azure_models.*]` + **Azure access** on the user | optional `AZURE_HTTP_PROXY`, `AZURE_INSECURE` |
| AWS Bedrock models (Converse, and native Messages for Claude) | `[bedrock_models.*]` + **AWS access** on the user | `BEDROCK_REGION`, `BEDROCK_AWS_ACCESS_KEY_ID`, `BEDROCK_AWS_SECRET_ACCESS_KEY` (or per model in `config.toml`); optional `BEDROCK_HTTP_PROXY`, `BEDROCK_INSECURE` |
| Health checks, failover to another server of the same type, keepalive pings on long turns | Always on | optional pool sizes (`DOWNSTREAM_*`, `CLOUD_*`) |
| Fallback for an unknown or down model, and for a rate-limited cloud model | `[fallback]`, per-model `rate_limit_fallback` | – |
| Reasoning-effort mapping per model | Per model in **Admin → Models** | – |

**Accounts and access**

| Feature | Switched on by | `.env` |
|---|---|---|
| An API key, a dashboard and usage figures per user; SSO sign-in to the web UI | Always on | `AUTH_BASE_URL`, `AUTH_CENTER_APP_ID`, `AUTH_CENTER_PUBLIC_KEY_PATH` |
| Sign-in for terminal tools (`POST /auth/api-key`: an AuthCenter token for the account's key) | Always on (needs its nginx location) | the same `AUTH_*` |
| Service accounts for programs, with owners who see them on their dashboard | **Admin → Users** | – |
| Account endpoints for tools: `/v1/me`, `/v1/status`, `/v1/usage`, `/v1/service-accounts`, `/v1/notices`, `/v1/settings` | Always on | – |
| Client setup offered to tools (`/v1/settings`) | **Admin → Clients → Client setup** | – |
| Setup page and Windows installer for Claude Code | On by default | `SETUP_PAGE_ENABLED=false` turns it off |

**Cost and limits**

| Feature | Switched on by | `.env` |
|---|---|---|
| Daily budget per user | Always on; amounts in **Admin → Users** / **Settings** | `ENFORCE_DAILY_LIMIT=false` logs instead of refusing |
| One daily cap on cloud spend (Azure + AWS), with optional fallback to local models when it is spent | **Admin → Settings** | – |
| Concurrency limit per account (local and cloud counted apart) | **Admin → Settings**; off by default | – |
| Per-model pricing, usage reports and the monthly export | Always on | optional `USAGE_ROLLUP_INTERVAL_S` |
| Hourly usage-anomaly scan (detection only) | On by default | optional `ANOMALY_SCAN_INTERVAL_S` |
| Deleting old raw usage rows | Off by default | `USAGE_LOG_RETENTION_DAYS` |

**Security**

| Feature | Switched on by | `.env` |
|---|---|---|
| API IP allowlist (monitor or enforce) | **Admin → Security**; off by default | – |
| Prompt guardrail (Qwen3Guard-Gen) on local traffic | **Admin → Security**; off by default; needs a guard model server | optional `GUARDRAIL_MAX_CONNECTIONS` |
| Lockout after many different invalid API keys from one address | On by default | optional `AUTH_FAIL_DISTINCT_KEYS`, `AUTH_FAIL_WINDOW_S`, `AUTH_LOCKOUT_S` |
| Audit log of every admin and dashboard change | Always on | – |

**Clients and mail**

| Feature | Switched on by | `.env` |
|---|---|---|
| Client checks for Claude Code (auto mode, plain http, old version, unknown or unavailable model), with a dashboard notice and one fix script per user | **Admin → Clients**; the first check is on by default | – |
| Mail to users about their client setup, paced in batches | Per check in **Admin → Clients** | `EXCHANGE_SERVER`, `EXCHANGE_USER`, `EXCHANGE_PASSWORD`, `EXCHANGE_EMAIL`, `COMPANY_EMAIL_DOMAIN`; `PUBLIC_BASE_URL` for links; optional `CLIENT_RULE_MAIL_*` |

**Observability and reports**

| Feature | Switched on by | `.env` |
|---|---|---|
| Langfuse tracing: one generation per request with usage, cost, latency, client and guardrail verdict | The three settings | `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`; optional `LANGFUSE_SAMPLE_RATE`, `LANGFUSE_CAPTURE_IO` |
| **Weekly usage report** by mail: tokens by backend, estimated cost per day and over 8 weeks (local, Azure, AWS, total), p50 / p95 latency per model, error rate | **Admin → Settings → Weekly reports**: day, time and recipients; off by default | needs Langfuse tracing **and** mail (both rows above); optional `WEEKLY_REPORT_TICK_S` |
| **Weekly guardrail report** by mail: verdicts, categories, what stood out, accounts to look at | **Admin → Settings → Weekly reports**, its own day, time and recipients; off by default | the same |
| Prometheus metrics at `/metrics` | The setting | `METRICS_ENABLED=true`; optional `METRICS_DIR` |
| Log files per worker, slow-request warnings | Always on | optional `LOG_*`, `SLOW_REQUEST_WARN_S` |

A weekly report can only be turned on while Langfuse tracing is on (the three settings present and `LANGFUSE_SAMPLE_RATE` above 0): the switch is locked otherwise, and nothing is sent. Recipients must be mailboxes of `COMPANY_EMAIL_DOMAIN`. **Preview** on the card shows a report in the browser without sending it; `uv run python scripts/weekly_report.py usage` does the same from a shell.

**Operations**

| Feature | Switched on by | `.env` |
|---|---|---|
| Periodic jobs inside the gateway (usage rollup, anomaly scan, retention, mail, weekly reports): no cron or systemd timer | Always on | the interval variables above; `0` turns one off |
| Database backup and restore drill | Host cron: `deploy/backup-db.sh`, `deploy/restore-drill.sh` | see [deploy/README.md](deploy/README.md) |
| Running as a container, moving to a new server | `deploy/migration/` | see [its README](deploy/migration/README.md) |

## Environment variables

Every variable the gateway reads, in three groups: the ones you must set, the ones a feature needs (set them only if you use that feature), and the optional ones with a working default. Changes take effect on restart. [`.env.example`](.env.example) has the same variables with longer notes.

### Required

The gateway does not work without these. Their built-in values are placeholders.

| Variable | What it is for |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `AUTH_BASE_URL` | AuthCenter: the JWT issuer. Web-UI and terminal sign-in fail without it |
| `AUTH_CENTER_APP_ID` | AuthCenter: the JWT audience (placeholder `llm_gateway`) |
| `AUTH_CENTER_PUBLIC_KEY_PATH` | AuthCenter's RS256 public key (placeholder `./keys/public.pem`) |

### Required by a feature

Leave a group unset and that feature is simply off. Within a group, every variable marked **required** must be set for the feature to work.

| Feature | Variable | Required? | What it is for |
|---|---|---|---|
| **Langfuse tracing** (and the weekly reports, which read from it) | `LANGFUSE_HOST` | **required** | The Langfuse server |
| | `LANGFUSE_PUBLIC_KEY` | **required** | Project public key |
| | `LANGFUSE_SECRET_KEY` | **required** | Project secret key |
| | `LANGFUSE_SAMPLE_RATE` | optional (`1.0`) | Share of requests recorded; `0` records nothing and locks the weekly reports |
| | `LANGFUSE_CAPTURE_IO` | optional (`false`) | Also record prompts and replies. Individual-level monitoring: decide this as policy |
| **Mail** (client-setup mail to users, and the weekly reports) | `EXCHANGE_SERVER` | **required** | Exchange (EWS) server |
| | `EXCHANGE_USER` | **required** | Service account that sends |
| | `EXCHANGE_PASSWORD` | **required** | Its password |
| | `EXCHANGE_EMAIL` | **required** | Sender address |
| | `COMPANY_EMAIL_DOMAIN` | **required** | A user's mailbox is `<username>@<domain>`; the only domain mail goes to |
| | `PUBLIC_BASE_URL` | optional | Where browsers reach the gateway, for links in mail (no links without it) |
| | `CLIENT_RULE_MAIL_BATCH`, `CLIENT_RULE_MAIL_PAUSE_S`, `CLIENT_RULE_MAIL_INTERVAL_S` | optional (`20`, `2`, `600`) | Pace of client-setup mail: mails per run, seconds between them, seconds between runs (`0` = off) |
| **Weekly reports** | Langfuse tracing and Mail, above | **required** (all eight) | A report cannot be turned on, or sent, without both |
| | `WEEKLY_REPORT_TICK_S` | optional (`300`) | How often the reports are looked at, so one goes out within this many seconds of its time (`0` = off) |
| **AWS Bedrock models** | `BEDROCK_REGION` | **required**, unless each model sets `region` in `config.toml` | AWS region shared by every Bedrock model |
| | `BEDROCK_AWS_ACCESS_KEY_ID`, `BEDROCK_AWS_SECRET_ACCESS_KEY` | **required**, unless each model has its own IAM pair or API key in `config.toml` | IAM pair shared by every Bedrock model (SigV4) |
| | `BEDROCK_HTTP_PROXY` | optional | HTTP proxy for Bedrock traffic only |
| | `BEDROCK_INSECURE` | optional (`false`) | Skip TLS verification to Bedrock |
| **Azure OpenAI models** | none: endpoint and key are per model in `config.toml` | – | – |
| | `AZURE_HTTP_PROXY` | optional | HTTP proxy for Azure traffic only |
| | `AZURE_INSECURE` | optional (`false`) | Skip TLS verification to Azure (TLS-inspecting proxy) |
| **Prometheus metrics** | `METRICS_ENABLED` | **required** (`true` turns it on) | Serve metrics at `/metrics` |
| | `METRICS_DIR` | optional (`<tmp>/llm-gateway-metrics`) | Where the workers share their metric values |
| **Deleting old usage rows** | `USAGE_LOG_RETENTION_DAYS` | **required** (a number of days turns it on) | Delete raw usage rows older than this; reports keep their totals |

### Optional

All have a working default; set one only to change the behaviour.

| Variable | Default | What it is for |
|---|---|---|
| `APP_TITLE` | `LLM Gateway` | Name shown in the web UI and in mail |
| `SETUP_PAGE_ENABLED` | `true` | `false` removes the Windows Setup page and installer |
| `ENFORCE_DAILY_LIMIT` | `true` | `false` logs over-budget requests instead of answering 429 |
| `AUTH_FAIL_DISTINCT_KEYS` | `10` | Different invalid keys from one address before it is locked out (`0` = off) |
| `AUTH_FAIL_WINDOW_S` | `600` | Window those keys are counted in |
| `AUTH_LOCKOUT_S` | `900` | How long the address is refused |
| `DOWNSTREAM_MAX_CONNECTIONS`, `DOWNSTREAM_MAX_KEEPALIVE_CONNECTIONS` | `1000`, `100` | Connection pool to the vLLM servers |
| `CLOUD_MAX_CONNECTIONS`, `CLOUD_MAX_KEEPALIVE_CONNECTIONS`, `CLOUD_KEEPALIVE_EXPIRY_S` | `200`, `100`, `30` | Connection pools to Azure and Bedrock |
| `GUARDRAIL_MAX_CONNECTIONS` | `200` | Connection pool to the guard model server |
| `USAGE_ROLLUP_INTERVAL_S` | `600` | Seconds between runs of the daily usage rollup (`0` = off; reports stay correct, slower) |
| `ANOMALY_SCAN_INTERVAL_S` | `3600` | Seconds between usage-anomaly scans (`0` = off) |
| `LOG_DIR`, `LOG_LEVEL`, `LOG_ROTATION`, `LOG_RETENTION` | `./logs`, `WARNING`, `100 MB`, `14 days` | Log files (one per worker) |
| `LOG_REQUEST_BODIES` | `false` | Log raw request bodies on a downstream error. Debugging only: it writes prompts to disk |
| `SLOW_REQUEST_WARN_S` | `120` | Warn in the log about requests slower than this (`0` = off) |
| `CONFIG_DIR` | project root | Directory holding `config.toml` and `client_settings.json`; set by the container deployment |

Not in this file: SSO itself (OIDC issuer, client secret) and the PostgreSQL container are configured in `deploy/.env` (see [deploy/README.md](deploy/README.md)); the container deployment has its own `gateway.env` (see [deploy/migration/README.md](deploy/migration/README.md)). Model servers, prices, limits and the guardrail live in `config.toml` or the admin panel, not in `.env`.

## Deploy it

Production runs as a user-level systemd service behind nginx and oauth2-proxy, with PostgreSQL in Docker: see [deploy/README.md](deploy/README.md). On every upgrade, apply schema changes before restarting:

```bash
uv run alembic upgrade head
```

Coming from the old SQLite database? See the [migration guide](docs/migration-guide.md).

## Develop

```bash
uv run pytest tests/ -q     # in-memory SQLite, mocked downstreams, no config.toml needed
uv run ruff check .
```

CI runs both on every pull request, plus a dependency audit and a secret scan. [CLAUDE.md](CLAUDE.md) is the contributor map: where things live and the rules that are easy to break.

## More documentation

| | |
|---|---|
| [Client Guide](docs/client-guide.md) | Connecting clients, endpoints, error handling |
| [Architecture](docs/architecture.md) | How every subsystem behaves, in detail |
| [Deployment](deploy/README.md) | Production setup, backups, upgrades |
| [Langfuse](docs/langfuse-observability.md) | Observability design |
| [Migrations](alembic/README.md) | Database schema changes |
| Package notes | [core](app/core/README.md) · [routers](app/routers/README.md) · [models](app/models/README.md) · [templates](app/templates/README.md) · [tests](tests/README.md) |
