# LLM Gateway Deployment Guide

[中文版](README.zh-TW.md)

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- Docker (PostgreSQL + oauth2-proxy)
- rsync, openssl (checked by `setup.sh`)
- Nginx (the example's `http2 on;` needs nginx ≥ 1.25.1; on older versions use `listen 443 ssl http2;`)
- sudo, for lingering and the nginx install
- AuthCenter RS256 public key (`keys/public.pem`)

## Architecture

```
Browser    → Nginx (:80)
               ├─ /oauth2/*            → oauth2-proxy (:4180)  ← handles login/logout
               ├─ /v1/*, = /me         → Gateway               ← API key auth (unified dispatch: vLLM / Azure / Bedrock)
               ├─ /azure/*             → Gateway               ← API key auth (Azure OpenAI backend)
               ├─ /aws/*               → Gateway               ← API key auth (AWS Bedrock backend)
               ├─ /healthz, /readyz    → Gateway               ← no auth (probes)
               ├─ /static/             → files on disk
               └─ /*                   → auth_request → oauth2-proxy validation
                                       → Gateway               ← JWT header auth
API client → Nginx (:443, TLS)  only /v1/, /azure/, /aws/, = /me, /healthz, /readyz; everything else 404
```

The example nginx config (`deploy/llm-gateway-example.nginx.conf`) defines parallel `/v1/`, `/azure/`, `/aws/` and `= /me` blocks in both the HTTP and HTTPS server contexts. All of them bypass oauth2-proxy and forward straight to the gateway with the client's `Authorization: Bearer <api_key>` header preserved. The Web UI is served over HTTP only, so oauth2-proxy's cookie settings (`COOKIE_SECURE=false`) need no HTTPS variant.

- **Gateway**: user-level systemd service (Python/FastAPI, `127.0.0.1:8050`, 2 workers). Its periodic jobs (usage rollup, anomaly scan, optional usage-log retention) run inside it: no timers
- **PostgreSQL + oauth2-proxy**: Docker Compose (`deploy/docker-compose.yml`, both bound to `127.0.0.1`)
- **Nginx**: system service, reverse proxy + auth_request

---

## First Deployment

```bash
git clone https://github.com/bwinken/llm-gateway.git
cd llm-gateway
cp deploy/llm-gateway-example.nginx.conf deploy/llm-gateway.nginx.conf   # your site copy (gitignored); see Nginx Configuration
bash deploy/setup.sh
```

The script interactively guides you through all setup steps. It starts with **pre-flight checks** (uv, docker, rsync, openssl, a running Docker daemon; `http_proxy` is picked up if set), then:

1. **rsync code** to `~/opt/llm-gateway` (config, keys, `.env` files and pgdata are excluded)
2. **`deploy/.env`**: prompts for the PostgreSQL user/password/DB/port, OIDC issuer URL, OAuth2 client ID/secret and domain; generates the cookie secret
3. **docker compose up**: starts PostgreSQL + oauth2-proxy and waits for PostgreSQL (checkpoint: you may stop here)
4. **uv sync** `--frozen --no-dev`: installs Python dependencies
5. **App config**: copies `config.toml.example` → `config.toml` and `.env.example` → `.env`, filling in `DATABASE_URL` and `AUTH_BASE_URL` from `deploy/.env`
6. **AuthCenter public key**: paste the PEM now or place it later
7. **alembic upgrade head**: if the migration fails, the script stops **without** restarting the gateway, so the old version keeps serving (checkpoint)
8. **systemd**: installs and (re)starts `llm-gateway.service`, disables and removes the scan / cleanup timers an older install left behind (the gateway runs those jobs now), enables lingering (checkpoint)
9. **Nginx**: renders `deploy/llm-gateway.nginx.conf` into `/etc/nginx/sites-available/llm-gateway`, then `nginx -t` and reload (skipped if nginx is not installed, or if that site copy is missing or empty)

It ends with a **deployment summary**: service status and remaining tasks.

> Existing `deploy/.env`, `config.toml`, `.env` and `keys/public.pem` are kept as they are, never overwritten, so re-running the script after stopping at a checkpoint is safe.

If you need a proxy:

```bash
export http_proxy=http://proxy.example.com:8080
bash deploy/setup.sh
```

## Updating Code

```bash
cd /path/to/llm-gateway   # original clone directory
git pull
bash deploy/setup.sh      # keeps existing config; updates code + deps, migrates, restarts
```

Quick update (when no new dependencies):

```bash
git pull
rsync -a --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
    --exclude='config.toml' --exclude='.env' --exclude='keys/' \
    ./ ~/opt/llm-gateway/
(cd ~/opt/llm-gateway && uv run alembic upgrade head)   # always migrate before restarting
systemctl --user restart llm-gateway
```

> `config.toml`, `.env`, `keys/`, and `deploy/.env` are never overwritten. If a systemd unit or timer under `deploy/` changed, run `setup.sh` instead, which reinstalls them.

## Service Management

```bash
# Gateway (systemd)
systemctl --user status llm-gateway     # Check status
systemctl --user restart llm-gateway    # Restart
journalctl --user -u llm-gateway -f     # View service output (app logs: ~/opt/llm-gateway/logs/)

# PostgreSQL + oauth2-proxy (Docker Compose)
docker compose -f ~/opt/llm-gateway/deploy/docker-compose.yml logs -f
docker compose -f ~/opt/llm-gateway/deploy/docker-compose.yml restart
docker compose -f ~/opt/llm-gateway/deploy/docker-compose.yml down
```

## Scheduled Jobs and Scripts

The gateway runs its periodic jobs itself; there are no systemd timers or cron entries to install. Every worker runs the loops, and the database makes sure each job runs once per interval across all workers and hosts (a claim in `scheduled_jobs`, or an advisory lock for the rollup). A job that fails is logged (`Scheduled job ... failed`, `Usage rollup failed`) and retried next interval; it never affects requests.

| Job | Schedule | What it does | Default | Setting (`.env`) |
|---|---|---|---|---|
| Usage rollup | every 10 min | Sums closed Asia/Taipei days of `usage_logs` into `usage_daily`, which the admin page, dashboard and exports read. Backfills on first start after the migration. | on | `USAGE_ROLLUP_INTERVAL_S` (`0` = off) |
| Anomaly scan | hourly | Flags unusual usage into `anomaly_events` for the admin panel. Detection only, never blocks anyone. | on | `ANOMALY_SCAN_INTERVAL_S` (`0` = off) |
| Usage-log retention | daily | **Deletes** `usage_logs` rows older than N days, whole days only, and never a day not yet in `usage_daily` (reports keep it) | **off** | `USAGE_LOG_RETENTION_DAYS` (unset = keep forever) |

`usage_logs` is the billing and audit record, and PostgreSQL holds years of it comfortably, so retention ships off on purpose: deleting it is a governance decision. Deleting a user keeps their usage rows too. `scheduled_jobs` shows each job's last start, finish and outcome. The scripts run the same code by hand and are dry runs without `--execute`:

```bash
cd ~/opt/llm-gateway
uv run python scripts/rollup_usage_daily.py              # rollup watermark and pending days
uv run python scripts/cleanup_usage_logs.py              # retention preview: cutoff and row count
uv run python scripts/cleanup_usage_logs.py --days 180   # custom retention
uv run python scripts/scan_anomalies.py                  # print findings only
```

`scripts/rollup_usage_daily.py --recompute FROM TO --execute` re-rolls days after a data correction. See [docs/architecture.md](../docs/architecture.md#daily-usage-rollup-appservicesusage_rolluppy).

Upgrading an install that still has the old `llm-gateway-scan` / `llm-gateway-cleanup` timers: re-running `setup.sh` removes them, or by hand:

```bash
systemctl --user disable --now llm-gateway-scan.timer llm-gateway-cleanup.timer
```

Other scripts, run by hand from `~/opt/llm-gateway`: `scripts/migrate_sqlite_to_pg.py` (one-off SQLite → PostgreSQL data migration, `--dry-run` / `--sync`) and `scripts/azure_latency_probe.py` (splits Azure latency into phases, direct vs. through the gateway; `--help`).

## Health Probes

Two unauthenticated endpoints, served by the Gateway itself:

| Path | Meaning | Codes |
|---|---|---|
| `/healthz` | Liveness — the process is up and the event loop is turning. Does no I/O. | always `200` |
| `/readyz` | Readiness — the database answers `SELECT 1`. Also reports the vLLM health-cache counts for humans to read. | `200` / `503` |

```bash
curl -s localhost:8050/healthz
# {"status":"ok","app":"LLM Gateway"}

curl -s localhost:8050/readyz
# {"status":"ok","database":"ok","downstreams":{"alive":3,"total":4}}
```

`/readyz` deliberately does **not** fail when downstream vLLM servers are down. The health cache is per-worker and empty for the first ~30 s after boot (a fresh worker would report itself unready), and a fleet-wide downstream outage would otherwise pull every Gateway instance out of the load balancer at once — turning a degraded service (Azure/Bedrock still route, clients still get a real error) into a total outage.

The example nginx config exposes both paths without `auth_request`, so an external monitor can poll them; probes local to the host should hit `127.0.0.1:8050` directly and skip nginx entirely.

## Prometheus Metrics (optional)

Off until `METRICS_ENABLED=true` is set in `.env`, then on after a restart (`systemctl --user restart llm-gateway`): `GET /metrics` serves request counts, tokens, spend, latency / TTFT, 429 rejections, in-flight requests and on-prem server health, summed over all workers. Optional `METRICS_DIR` (default `<tmp>/llm-gateway-metrics`) holds the per-worker files.

Scrape it on the host, not through nginx (the example config returns 404 for `/metrics`):

```yaml
scrape_configs:
  - job_name: llm-gateway
    static_configs:
      - targets: ["127.0.0.1:8050"]
```

## Log Privacy

Two settings decide whether user prompts reach durable storage:

| Setting | Default | Effect when on |
|---|---|---|
| `LANGFUSE_CAPTURE_IO` | off | Prompt/response content is sent to Langfuse per request |
| `LOG_REQUEST_BODIES` | off | An Azure/Bedrock 4xx/5xx logs the raw request body under `LOG_DIR` |

With both off, a failed request logs the **shape** of the body — fields present, message roles, content-block types, string lengths — which is what diagnoses a downstream 400, without the prose. Turn `LOG_REQUEST_BODIES` on to chase a specific bug and turn it back off; anything it writes stays for `LOG_RETENTION` (14 days by default) and is readable by anyone with host access.

## PostgreSQL Data Migration (Volume Change)

When moving PG data to a different volume (e.g., wrong disk mount, capacity expansion), using `pg_dumpall` logical backup is safest — unaffected by filesystem or permission differences.

```bash
# 1. Export entire database (while container is still running)
docker exec llm-gateway-pg pg_dumpall -U "$PG_USER" > /tmp/pg_backup.sql

# 2. Stop all services
docker compose -f deploy/docker-compose.yml down

# 3. Update deploy/.env, point PGDATA_DIR to new volume
#    e.g.: PGDATA_DIR=/mnt/new-volume/pgdata

# 4. Start PG (initializes empty database on new volume)
docker compose -f deploy/docker-compose.yml up -d postgres

# 5. Import backup after PG is ready
docker exec -i llm-gateway-pg psql -U "$PG_USER" -d "$PG_DB" < /tmp/pg_backup.sql

# 6. After verifying data, start remaining services
docker compose -f deploy/docker-compose.yml up -d
```

> **Note**: Ensure the new volume is mounted and has sufficient space before proceeding. `$PG_USER` / `$PG_DB` correspond to values in `deploy/.env`.

## Directory Structure

```
~/opt/llm-gateway/
├── app/                      # Application code
├── scripts/                  # Anomaly scan, usage-log cleanup, one-off tools
├── .venv/                    # uv-managed virtual environment
├── config.toml               # Model routing config
├── .env                      # Application env vars
├── keys/public.pem           # AuthCenter public key
├── logs/                     # App log files, one per worker (LOG_DIR)
└── deploy/
    ├── docker-compose.yml    # PG + oauth2-proxy
    ├── .env                  # Docker services env vars (template: .env.example)
    ├── pgdata/               # PostgreSQL data
    ├── setup.sh              # Deployment script
    ├── gen-selfsigned-cert.sh               # Self-signed TLS cert for :443
    ├── llm-gateway.service                  # Gateway systemd unit
    ├── llm-gateway-example.nginx.conf       # Nginx template
    └── llm-gateway.nginx.conf               # Your site copy (gitignored)
```

## Config Files

| File | Purpose |
|---|---|
| `deploy/.env` | Docker services: PG credentials/port/`PGDATA_DIR`, OIDC issuer, oauth2-proxy client, redirect URL, cookie |
| `.env` | Gateway app environment; see below |
| `config.toml` | Model routing (vLLM, Azure, Bedrock), pricing, fallback, `[app]` runtime settings |

`.env` is read by the systemd unit (`EnvironmentFile`); restart the gateway after editing it. [`.env.example`](../.env.example) documents every variable, and the root [README](../README.md#env-environment) has a table with defaults. In short:

- **Core**: `DATABASE_URL`, `APP_TITLE`, and the web-UI JWT settings `AUTH_BASE_URL`, `AUTH_CENTER_APP_ID`, `AUTH_CENTER_PUBLIC_KEY_PATH`
- **Cloud network**: `AZURE_HTTP_PROXY` / `BEDROCK_HTTP_PROXY` and `AZURE_INSECURE` / `BEDROCK_INSECURE` (see Proxy Configuration)
- **Bedrock shared credentials**: `BEDROCK_REGION`, `BEDROCK_AWS_ACCESS_KEY_ID`, `BEDROCK_AWS_SECRET_ACCESS_KEY`, used by every `[bedrock_models.*]` entry that doesn't set its own. Kept here so the secret never reaches `config.toml` or the admin page.
- **Budgets**: `ENFORCE_DAILY_LIMIT=false` is a gateway-wide soft mode (overruns are logged, not rejected). There is no per-user "unlimited": a user limit of `0` is a $0 budget.
- **Logging**: `LOG_DIR`, `LOG_LEVEL`, `LOG_ROTATION`, `LOG_RETENTION`, `SLOW_REQUEST_WARN_S`, `LOG_REQUEST_BODIES` (see Log Privacy)
- **Langfuse** (optional): `LANGFUSE_HOST` + `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` (all three, or it stays off), `LANGFUSE_SAMPLE_RATE`, `LANGFUSE_CAPTURE_IO`
- **Connection pool**: `DOWNSTREAM_MAX_CONNECTIONS` (default 1000; each open stream holds one connection)

## Nginx Configuration

`setup.sh` builds the site from **`deploy/llm-gateway.nginx.conf`**, your copy of `deploy/llm-gateway-example.nginx.conf`, replacing `__APP_DIR__` and `llm-gateway.your-domain.com`. Create that copy before step 9, and keep your edits there: every run that reaches step 9 rewrites `/etc/nginx/sites-available/llm-gateway`.

The example's `:443` block points at Let's Encrypt paths (`/etc/letsencrypt/live/<domain>/`) and `nginx -t` fails until those files exist. Before installing, either obtain a certificate, run `bash deploy/gen-selfsigned-cert.sh` (airgapped hosts; writes `/etc/nginx/ssl/llm-gateway.{crt,key}` by default and prints the edits to make), or delete the `:443` block.

To adjust the installed site by hand:

```bash
sudo nano /etc/nginx/sites-available/llm-gateway
sudo nginx -t && sudo systemctl reload nginx
```

Let's Encrypt certificate (optional):

```bash
sudo certbot --nginx -d your-domain.com
```

## Proxy Configuration

`setup.sh` does not prompt for a proxy. It exports `http_proxy` from your shell to the commands it runs (e.g. `uv sync`) with `no_proxy=localhost,127.0.0.1,*.company.local`; the Docker daemon needs its own proxy configuration to pull images:

```bash
export http_proxy=http://proxy.example.com:8080
```

The commented `HTTP_PROXY` / `HTTPS_PROXY` lines in `llm-gateway.service` do **not** reach the gateway's downstream traffic: its httpx clients are built with an explicit transport, so httpx ignores proxy environment variables. Use the per-cloud settings below.

### Cloud-only HTTP proxy (Azure, Bedrock)

If Azure OpenAI or AWS Bedrock can only be reached through a corporate HTTP proxy while internal vLLM downstreams must stay direct, set `AZURE_HTTP_PROXY` and/or `BEDROCK_HTTP_PROXY` in the Gateway's `.env`. Only that cloud's downstream calls (from `/azure/v1/*`, `/aws/v1/*` and the `/v1/*` aliases that route there) go through it; vLLM traffic always goes direct. Inline credentials are supported (`AZURE_HTTP_PROXY=http://user:pass@proxy.company.local:8080`). If a TLS-inspecting proxy re-signs the cloud's certificate with a CA the host doesn't trust, `AZURE_INSECURE=true` / `BEDROCK_INSECURE=true` turn off certificate verification for that cloud's client only.
