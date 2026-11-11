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
Browser / API client → Nginx (:80)   → 308 to https:// (method and body kept); /healthz, /readyz optionally served on HTTP
Browser / API client → Nginx (:443, TLS)
               ├─ /oauth2/*            → oauth2-proxy (:4180)  ← handles login/logout
               ├─ /v1/*, = /me         → Gateway               ← API key auth (unified dispatch: vLLM / Azure / Bedrock)
               ├─ /azure/*             → Gateway               ← API key auth (Azure OpenAI backend)
               ├─ /aws/*               → Gateway               ← API key auth (AWS Bedrock backend)
               ├─ /healthz, /readyz    → Gateway               ← no auth (probes)
               ├─ /static/             → files on disk
               └─ /*                   → auth_request → oauth2-proxy validation
                                       → Gateway               ← JWT header auth
```

Everything is served over HTTPS. The example nginx config (`deploy/llm-gateway-example.nginx.conf`) puts every location in the `:443` server; the `:80` server only redirects with **308**, which keeps the method and body (a 301 makes many clients turn a POST into a GET). The API locations (`/v1/`, `/azure/`, `/aws/`, `= /me`) bypass oauth2-proxy and forward the client's `Authorization: Bearer <api_key>` header as is. The login cookie is `Secure` (`OAUTH2_PROXY_COOKIE_SECURE`, default `true` in `docker-compose.yml`), so it is never sent over plain HTTP.

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
2. **`deploy/.env`**: prompts for the PostgreSQL user/password/DB/port, OIDC issuer URL, OAuth2 client ID/secret and domain (the callback becomes `https://<domain>/oauth2/callback`; allow it on the AuthCenter client); generates the cookie secret
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
bash deploy/backup-db.sh  # back up the database first (online, see Database Backup)
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
| Client-rule mail | every 10 min | Sends client-rule mail in batches (20 a run, 2 s apart by default): accounts an admin queued, then those due by a rule's automatic mail and its weekly / monthly reminder. Does nothing while mail is not configured. | on | `CLIENT_RULE_MAIL_INTERVAL_S` (`0` = off), `CLIENT_RULE_MAIL_BATCH`, `CLIENT_RULE_MAIL_PAUSE_S` |
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

## Security Settings

- **Audit log.** Every admin / dashboard change, every full API key shown and every export is a row in `audit_log` (admin *Security* → *Audit Log*, CSV download). It is append-only: PostgreSQL triggers refuse UPDATE, DELETE and TRUNCATE, so removing entries means a DBA dropping the trigger on purpose. Each entry is also logged as `Audit | {...}` (usernames, addresses, submitted fields; secrets redacted).
- **Failed-auth throttle.** `AUTH_FAIL_DISTINCT_KEYS` / `AUTH_FAIL_WINDOW_S` / `AUTH_LOCKOUT_S` (default 10 distinct invalid keys in 600 s → 429 for 900 s). It keys on the client address, so uvicorn must trust nginx's forwarded headers; loopback is never locked, which is what you would see if it didn't.
- **Dependency and secret scanning** run in GitHub Actions (`.github/workflows/security.yml`). For Dependabot to open security-fix PRs, turn on *Dependabot security updates* in the repository's Settings → Code security.

## Database Backup

`deploy/backup-db.sh` takes an online backup: `pg_dump` reads the whole database in one snapshot, so the dump is consistent and the gateway keeps serving. It only blocks schema changes, so never run it alongside `alembic upgrade` / `setup.sh` (the script refuses while an `alembic upgrade` is running).

```bash
bash ~/opt/llm-gateway/deploy/backup-db.sh                  # → ~/backup/llm-gateway
bash ~/opt/llm-gateway/deploy/backup-db.sh /mnt/nas/llmgw   # another directory
bash ~/opt/llm-gateway/deploy/backup-db.sh --check FILE     # is this dump readable? (.gpg needs the private key)
bash ~/opt/llm-gateway/deploy/restore-drill.sh              # restore the latest backup into a throwaway container
```

It reads `PG_USER` / `PG_DB` from `deploy/.env`, checks free space against the database size, writes `daily/llm-gateway-<date>_<time>.dump` (`pg_dump -Fc`) through a temporary file, verifies it with `pg_restore --list` before keeping it, copies the first backup of each month to `monthly/`, and prunes to `KEEP_DAILY` (30) and `KEEP_MONTHLY` (12). Files are `600`, directories `700`: a dump holds every API key. Set `BACKUP_COPY_TO=user@host:/path/` to rsync each backup off the host; without it the script warns that the backup only lives on this machine. Exit code 0 on success; a failure leaves no partial file.

### Encryption

Set `BACKUP_GPG_RECIPIENT_FILE` to a GnuPG **public** key and every backup is written as `llm-gateway-<date>_<time>.dump.gpg`. Only the public key lives on the gateway host; the private key is kept offline by whoever owns the backups, so a stolen disk, NAS or offsite copy yields nothing readable. The dump is verified before it is encrypted, and the plaintext temporary file is deleted right after (it sits for seconds in the `700` backup directory, on the same disk as the database itself); only the encrypted file is kept, copied to `monthly/` and sent to `BACKUP_COPY_TO`. A key that cannot encrypt (wrong file, expired) fails the run before the dump starts. Without a key the script still backs up, and warns that the backup is not encrypted.

Create the key pair once, on the key owner's machine, not on the gateway host:

```bash
gpg --quick-gen-key "LLM Gateway backup <dba@your-company.com>" default default never   # set a passphrase
gpg --armor --export dba@your-company.com > llm-gateway-backup.pub.asc                     # → the gateway host
gpg --armor --export-secret-keys dba@your-company.com > llm-gateway-backup.key.asc          # → offline storage
```

Copy the `.pub.asc` to the gateway host (for example `~/opt/llm-gateway/keys/backup.pub.asc`) and add it to the cron line. `BACKUP_GPG_RECIPIENT=<fingerprint>` works too when the key is already in that user's keyring. Losing the private key means losing every encrypted backup: keep two copies in separate places. Decrypt by hand with `gpg --decrypt FILE.dump.gpg > FILE.dump`.

Run it before every upgrade, and daily from cron at an off-peak hour. A database backup is a host task, not one of the gateway's in-process jobs (those never touch Docker):

```bash
30 2 * * * BACKUP_GPG_RECIPIENT_FILE=$HOME/opt/llm-gateway/keys/backup.pub.asc BACKUP_COPY_TO=user@nas:/backup/llmgw/ bash $HOME/opt/llm-gateway/deploy/backup-db.sh >> $HOME/backup/llm-gateway/backup.log 2>&1
```

### Restore drill

`pg_restore --list` proves a file is readable, not that it restores. `deploy/restore-drill.sh` does the real thing: run it quarterly and after any change to how backups are taken. It never connects to the production database.

```bash
bash ~/opt/llm-gateway/deploy/restore-drill.sh                     # latest file in <BACKUP_DIR>/daily
bash ~/opt/llm-gateway/deploy/restore-drill.sh FILE                # a specific .dump or .dump.gpg
DRILL_GPG_KEY=/media/usb/llm-gateway-backup.key.asc \
    bash ~/opt/llm-gateway/deploy/restore-drill.sh                 # encrypted: bring the private key
```

It starts a throwaway container from the **same image** as `llm-gateway-pg` (or `DRILL_IMAGE`) with no network, decrypts and restores with `pg_restore --exit-on-error`, then checks the schema version, the row counts of `users` / `usage_logs` / `usage_daily` / `audit_log`, the newest usage row, and that `audit_log`'s append-only triggers came back. The container is removed with its data volume whatever the outcome. `DRILL_GPG_KEY` is imported into a temporary GnuPG directory that is deleted on exit, so the private key does not stay on the host; without it the user's own keyring is used. gpg asks for the passphrase on the terminal.

Every run, passed or failed, appends one line to `<BACKUP_DIR>/restore-drill.log` (`DRILL_LOG`): time, result, file and its SHA-256 prefix, who ran it, restore and total time, schema version and counts. That file is the evidence an auditor asks for (ISO 27001 A.8.13), and the restore time is a measured figure for your RTO. Exit code 0 only when the restore and every check passed.

To restore after a real loss, into a fresh database (decrypt first: `gpg --decrypt FILE.dump.gpg | ...`):

```bash
docker exec -i llm-gateway-pg pg_restore -U "$PG_USER" --create -d postgres < FILE
```

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
    ├── backup-db.sh          # Online database backup (pg_dump, verify, encrypt, retention)
    ├── restore-drill.sh      # Restore a backup into a throwaway container, log the result
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

The example's `:443` block points at Let's Encrypt paths (`/etc/letsencrypt/live/<domain>/`) and `nginx -t` fails until those files exist. Every page is served from that block, so get a certificate before installing: Let's Encrypt, a company CA, or `bash deploy/gen-selfsigned-cert.sh` (airgapped hosts; writes `/etc/nginx/ssl/llm-gateway.{crt,key}` by default and prints the edits to make; browsers warn about a self-signed certificate).

To adjust the installed site by hand:

```bash
sudo nano /etc/nginx/sites-available/llm-gateway
sudo nginx -t && sudo systemctl reload nginx
```

Let's Encrypt certificate (optional):

```bash
sudo certbot --nginx -d your-domain.com
```

### Moving an existing HTTP deployment to HTTPS

Earlier versions served the Web UI over HTTP and only the API on `:443`. To switch an existing install:

> **Updating the code before switching?** `docker-compose.yml` now makes the SSO cookie `Secure`, which a browser never sends over plain HTTP: sign-in to the Web UI stops working. Until step 5, add `OAUTH2_COOKIE_SECURE=false` to `deploy/.env` before running `setup.sh`, and remove it when you switch.


1. **Certificate.** The whole site now needs one the browsers trust: Let's Encrypt, a company CA, or `deploy/gen-selfsigned-cert.sh` (self-signed makes every browser warn; prefer a company CA on an intranet).
2. **AuthCenter.** Allow `https://<domain>/oauth2/callback` on the OIDC client. Keep the old `http://` callback until the switch is done.
3. **`deploy/.env`.** Edit `OAUTH2_REDIRECT_URL` to `https://<domain>/oauth2/callback` by hand: `setup.sh` never overwrites an existing `deploy/.env`.
4. **Nginx.** Merge the new example into your `deploy/llm-gateway.nginx.conf` (all locations under `:443`, `:80` = 308), then run `setup.sh` or `sudo nginx -t && sudo systemctl reload nginx`. If `:443` carries `allow` / `deny` lines, they now gate the Web UI and login too.
5. **oauth2-proxy.** `docker compose -f ~/opt/llm-gateway/deploy/docker-compose.yml up -d` recreates it with the https callback and the `Secure` cookie. Everyone signs in once more: the old cookie was not `Secure`.
6. **Clients.** Change every base URL to `https://`: Claude Code `ANTHROPIC_BASE_URL`, SDK `base_url`, and `GATEWAY_URL` in `setup/install-claude-code.bat`. The 308 is only a safety net: some clients drop the `Authorization` header or the body when redirected.
7. **Optional transition.** The example's commented `:80` block keeps the API on HTTP with its own access log for a while, so you can see who still calls `http://` before turning them over to the 308.
8. **HSTS, later.** Once everything works over https, uncomment the `Strict-Transport-Security` line with a short `max-age` and raise it after a while.

Check:

```bash
curl -sI http://your-domain.com/dashboard | head -3      # expect 308 and Location: https://...
curl -s https://your-domain.com/v1/models -H "Authorization: Bearer <key>" | head -c 200
```

## Proxy Configuration

`setup.sh` does not prompt for a proxy. It exports `http_proxy` from your shell to the commands it runs (e.g. `uv sync`) with `no_proxy=localhost,127.0.0.1,*.company.local`; the Docker daemon needs its own proxy configuration to pull images:

```bash
export http_proxy=http://proxy.example.com:8080
```

The commented `HTTP_PROXY` / `HTTPS_PROXY` lines in `llm-gateway.service` do **not** reach the gateway's downstream traffic: its httpx clients are built with an explicit transport, so httpx ignores proxy environment variables. Use the per-cloud settings below.

### Cloud-only HTTP proxy (Azure, Bedrock)

If Azure OpenAI or AWS Bedrock can only be reached through a corporate HTTP proxy while internal vLLM downstreams must stay direct, set `AZURE_HTTP_PROXY` and/or `BEDROCK_HTTP_PROXY` in the Gateway's `.env`. Only that cloud's downstream calls (from `/azure/v1/*`, `/aws/v1/*` and the `/v1/*` aliases that route there) go through it; vLLM traffic always goes direct. Inline credentials are supported (`AZURE_HTTP_PROXY=http://user:pass@proxy.company.local:8080`). If a TLS-inspecting proxy re-signs the cloud's certificate with a CA the host doesn't trust, `AZURE_INSECURE=true` / `BEDROCK_INSECURE=true` turn off certificate verification for that cloud's client only.
