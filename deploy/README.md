# LLM Gateway Deployment Guide

[中文版](README.zh-TW.md)

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- Docker (PostgreSQL + oauth2-proxy)
- Nginx
- AuthCenter RS256 public key (`keys/public.pem`)

## Architecture

```
Browser → Nginx (:80)
            ├─ /oauth2/*  → oauth2-proxy (:4180)  ← handles login/logout
            ├─ /v1/*      → Gateway                 ← API key auth (vLLM backend)
            ├─ /azure/*   → Gateway                 ← API key auth (Azure OpenAI backend)
            └─ /*         → auth_request → oauth2-proxy validation
                          → Gateway                 ← JWT header auth
```

The example nginx config (`deploy/llm-gateway-example.nginx.conf`) defines parallel `/v1/` and `/azure/` blocks in both the HTTP and HTTPS server contexts. Both bypass oauth2-proxy and forward straight to the gateway with the client's `Authorization: Bearer <api_key>` header preserved.

- **Gateway**: user-level systemd service (Python/FastAPI)
- **PostgreSQL + oauth2-proxy**: Docker Compose (`deploy/docker-compose.yml`)
- **Nginx**: system service, reverse proxy + auth_request

---

## First Deployment

```bash
git clone https://github.com/bwinken/llm-gateway.git
cd llm-gateway
bash deploy/setup.sh
```

The script interactively guides you through all setup steps:

1. **Pre-flight checks** — Verifies uv, docker, rsync, openssl are installed
2. **PostgreSQL setup** — Prompts for password (or auto-generates one)
3. **OIDC / oauth2-proxy setup** — Prompts for issuer URL, client ID/secret, domain
4. **AuthCenter public key** — Paste PEM content or specify file path
5. **rsync code** to `~/opt/llm-gateway`
6. **docker compose up** — Starts PostgreSQL + oauth2-proxy
7. **uv sync** — Installs Python dependencies
8. **Auto-creates `.env`** with DATABASE_URL
9. **alembic upgrade head** — Runs database migrations
10. **Sets up user-level systemd** service + lingering
11. **Configures nginx** reverse proxy + auth_request
12. **Deployment summary** — Shows service health status and remaining tasks

> On repeated runs, the script detects existing config files and asks before overwriting.

If you need a proxy:

```bash
export http_proxy=http://proxy.example.com:8080
bash deploy/setup.sh
```

## Updating Code

```bash
cd /path/to/llm-gateway   # original clone directory
git pull
bash deploy/setup.sh      # detects existing config, only updates code + deps + restarts
```

Quick update (when no new dependencies):

```bash
git pull
rsync -a --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
    --exclude='config.toml' --exclude='.env' --exclude='keys/' \
    ./ ~/opt/llm-gateway/
systemctl --user restart llm-gateway
```

> `config.toml`, `.env`, `keys/`, and `deploy/.env` are never overwritten.

## Service Management

```bash
# Gateway (systemd)
systemctl --user status llm-gateway     # Check status
systemctl --user restart llm-gateway    # Restart
journalctl --user -u llm-gateway -f     # View logs

# PostgreSQL + oauth2-proxy (Docker Compose)
docker compose -f ~/opt/llm-gateway/deploy/docker-compose.yml logs -f
docker compose -f ~/opt/llm-gateway/deploy/docker-compose.yml restart
docker compose -f ~/opt/llm-gateway/deploy/docker-compose.yml down
```

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

`/readyz` deliberately does **not** fail when downstream vLLM servers are
down. The health cache is per-worker and empty for the first ~30 s after
boot (a fresh worker would report itself unready), and a fleet-wide
downstream outage would otherwise pull every Gateway instance out of the
load balancer at once — turning a degraded service (Azure/Bedrock still
route, clients still get a real error) into a total outage.

The example nginx config exposes both paths without `auth_request`, so an
external monitor can poll them; probes local to the host should hit
`127.0.0.1:8050` directly and skip nginx entirely.

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
├── .venv/                    # uv-managed virtual environment
├── config.toml               # Model routing config
├── .env                      # Application env vars
├── keys/public.pem           # AuthCenter public key
└── deploy/
    ├── docker-compose.yml    # PG + oauth2-proxy
    ├── .env                  # Docker services env vars
    ├── pgdata/               # PostgreSQL data
    ├── setup.sh              # Deployment script
    ├── llm-gateway.service   # systemd unit
    └── llm-gateway-example.nginx.conf
```

## Config Files

| File | Purpose |
|---|---|
| `deploy/.env` | Docker services: PG password, OIDC issuer, oauth2-proxy client, etc. |
| `.env` | Gateway app: DATABASE_URL, AUTH_CENTER_APP_ID, optional AZURE_HTTP_PROXY |
| `config.toml` | Model routing, pricing, fallback |

## Nginx Configuration

The deployment script installs it automatically. To adjust manually:

```bash
sudo nano /etc/nginx/sites-available/llm-gateway
sudo nginx -t && sudo systemctl reload nginx
```

SSL certificate (optional):

```bash
sudo certbot --nginx -d your-domain.com
```

## Proxy Configuration

The deployment script prompts whether a proxy is needed. To configure manually:

```bash
export http_proxy=http://proxy.example.com:8080
```

For Gateway runtime proxy, uncomment the proxy lines in `llm-gateway.service`.

### Azure-only HTTP proxy

If Azure OpenAI can only be reached through a corporate HTTP proxy while internal vLLM downstreams must stay direct, set `AZURE_HTTP_PROXY` in the Gateway's `.env` instead of a global `http_proxy`. Only `/azure/v1/*` downstream calls are routed through it; vLLM traffic always goes direct. Inline credentials are supported (`AZURE_HTTP_PROXY=http://user:pass@proxy.company.local:8080`).
