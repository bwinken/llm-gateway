# Migration: running the gateway in a container, on a new server

[繁體中文](README.zh-TW.md)

This folder holds everything needed to run the gateway as a container and move it to a new server: the image, the compose file, the script that moves the data, and a drill that walks the whole move on another machine first.

The approach is **bring every container up on the new server first, and move the data last**. The old server keeps serving until the moment of the switch.

## Existing deployments are not affected

The files an existing host (gateway under systemd) uses are unchanged:

- `deploy/docker-compose.yml`, `deploy/.env.example` and `.env.example` are exactly as before.
- The gateway container is defined here, in `docker-compose.gateway.yml`, and only `compose.sh` loads it. The usual `docker compose -f deploy/docker-compose.yml ...` on an existing host never sees it.
- The only step that runs on the existing host is `1-bundle.sh`: it reads, takes one online backup, and causes no downtime.

Two related changes live outside this folder; both keep their default behavior:

- `CONFIG_DIR` (application): the directory holding `config.toml`. Unset, it is the project root as before.
- `deploy/backup-db.sh`: encrypts backups only when a public key is set. Unset, it behaves as before (see [deploy/README](../README.md#encryption)).

## Files

| File | Purpose |
|---|---|
| `Dockerfile`, `Dockerfile.dockerignore` | The gateway image, and what stays out of the build context (secrets, data, tests) |
| `docker-compose.gateway.yml` | The `migrate` (alembic) and `gateway` services, layered on the original compose file |
| `compose.sh` | `docker compose` with the original compose file and the one above |
| `gateway.env.example` | Settings to add to `deploy/.env` on a host that runs the container |
| `wait_for_db.py` | Used by `migrate`: waits for the database (copied into the image) |
| `load-backup.sh` | Moves the data: loads a backup into this host's database, migrates, starts the gateway |
| `1-bundle.sh` to `7-teardown.sh`, `lib.sh`, `drill.env.example` | The drill |

## Running the gateway in a container

**Always go through `compose.sh`.** It is `docker compose -f deploy/docker-compose.yml -f deploy/migration/docker-compose.gateway.yml`, and takes the same arguments:

```bash
bash deploy/migration/compose.sh up -d
```

```bash
bash deploy/migration/compose.sh logs -f gateway
```

On a host that runs the gateway container, stop using commands that name only the original file: `docker compose -f deploy/docker-compose.yml down` stops PostgreSQL and leaves the gateway container running with no database.

**Host networking.** The gateway container uses `network_mode: host`, so it sees the same network the systemd service did:

- nginx keeps proxying to `127.0.0.1:8050`;
- the `.env` `DATABASE_URL` (`localhost:5432`) and proxy settings keep working;
- requests arrive from `127.0.0.1`, the one address uvicorn trusts for `X-Forwarded-For`. On a bridge network every request would appear to come from Docker's gateway address, which breaks the IP allowlist, the failed-auth lockout (one bad client locks out everyone), the audit log's client column and the `CC002` plain-http check.

The container binds `127.0.0.1` only. It keeps the host's hostname, so concurrency leases left by a previous container are swept at startup.

**What stays on the host** (under the install directory, mounted into the container):

| Host path | In the container | Notes |
|---|---|---|
| `config/config.toml` | `/app/config` (`CONFIG_DIR`) | The admin page rewrites it with a temp file and a rename, so the **directory** is mounted; a single-file mount would make every admin save fail |
| `keys/public.pem` | `/app/keys` (read-only) | |
| `logs/` | `/app/logs` (`LOG_DIR`) | `docker logs llm-gateway` has the same lines |
| `.env` | environment | Compose overrides `CONFIG_DIR`, `LOG_DIR`, `AUTH_CENTER_PUBLIC_KEY_PATH` and `METRICS_DIR` with the container's paths, so host paths in `.env` don't matter |

The container runs as `GATEWAY_UID:GATEWAY_GID` (`deploy/.env`, default `1000:1000`), which must own `config/` and `logs/`. Create both before the first start: a missing directory is created by Docker as root, and admin saves would then fail.

Every `up` runs `migrate` first (it waits for the database, then `alembic upgrade head`), and `gateway` starts only when that succeeded. The health check polls `/readyz`.

**Building the image** (from the repository root):

```bash
docker build -f deploy/migration/Dockerfile -t llm-gateway:2026.11.1 .
```

- The image takes exactly what `uv.lock` pins, runs as a non-root user, and carries no secrets.
- Behind a proxy, pass `--build-arg HTTPS_PROXY=...`. For an internal mirror: `--build-arg PYTHON_IMAGE=... --build-arg UV_IMAGE=...`.
- For a host without internet access, build elsewhere and carry the image over:

```bash
docker save llm-gateway:2026.11.1 | gzip > llm-gateway-2026.11.1.tar.gz
```

```bash
docker load < llm-gateway-2026.11.1.tar.gz
```

## Moving to a new server (the real steps)

Run the [drill](#the-drill) below first: it performs the same steps and measures the time.

### Prepare the new server (no downtime)

1. Put the code and the image there: a checkout of the same version, then `docker build` or `docker load`.
2. Create `config/config.toml`, `keys/public.pem`, `.env` and `deploy/.env`:
   - copy `deploy/.env` from `deploy/.env.example`, set a new `PG_PASSWORD`, and append the contents of `gateway.env.example` (`GATEWAY_IMAGE`, `GATEWAY_UID` / `GATEWAY_GID`);
   - `DATABASE_URL` in `.env` uses the new password.
3. Start everything on an empty database:

```bash
bash deploy/migration/compose.sh up -d
```

4. Set up nginx and the certificate. If the hostname changes, register the new callback URL in AuthCenter.
5. Check it: `/readyz`, the login page, and that the **new server's address** can reach vLLM, Azure, Bedrock, Exchange and Langfuse. Firewalls and allowlists on those sides often name the old server.

Until the switch the new server's database is empty. **Do not load production data into it for testing while the old server is still serving:** the new server's scheduled jobs would act on it, and client-rule mail would go out from both. To test with real data, use the drill, which strips the mail settings.

### Switch (downtime starts at step 1)

```bash
# old server
systemctl --user stop llm-gateway            # 1. requests fail from here; no new writes
bash deploy/backup-db.sh                     # 2. final backup
# copy the newest backup file and config.toml (admins may have edited it) to the new server

# new server
BACKUP_GPG_KEY=/media/usb/llm-gateway-backup.key.asc \
    bash deploy/migration/load-backup.sh ~/llm-gateway-<time>.dump.gpg     # 3. replace the empty database, migrate, start
```

`load-backup.sh` shows what the current database holds and asks for its name before replacing it; a database that is not empty is saved to `<BACKUP_DIR>/before-load/` first. It then stops the gateway container, recreates the database, restores, clears the old server's concurrency leases, runs the migrations and starts the gateway, printing the time each phase took. A failed restore or migration leaves the gateway stopped. It refuses on a host with a running `llm-gateway` systemd service.

4. Check the new server: the row counts the script printed, a login, one request with an existing key.
5. Point traffic at the new server (DNS, load balancer or the upstream in nginx).

### Rollback

- **Before traffic moved:** `systemctl --user start llm-gateway` on the old server. Its data was never touched.
- **After traffic moved:** point it back and start the old gateway. Usage recorded on the new server in between is not in the old database.

Keep the old server's database until the new one has run cleanly for a while.

### Afterwards

- Move the backup cron line (`deploy/backup-db.sh`) to the new server and run `deploy/restore-drill.sh` there once.
- Delete the copy in `before-load/` (it is not encrypted).
- When the old server is retired, remove its `pgdata` and backups: they hold every API key.

### Day to day on the new server

| Operation | Result |
|---|---|
| `compose.sh up -d` | Runs `migrate`, then starts or replaces `gateway`; PostgreSQL and oauth2-proxy are left alone unless their settings changed |
| `compose.sh down` | Stops and removes all of them; open streams get up to 300 s to finish |
| `compose.sh restart` | Restarts the running containers; `migrate` is not rerun |
| Host reboot | Docker brings PostgreSQL, oauth2-proxy and the gateway back (`restart: unless-stopped`). `migrate` does not rerun. If the gateway comes up before PostgreSQL, its workers retry until the database answers |

**Updates:** build a new image tag, set `GATEWAY_IMAGE` in `deploy/.env`, run `deploy/backup-db.sh`, then `bash deploy/migration/compose.sh up -d`.

Manual scripts run inside the container: `bash deploy/migration/compose.sh exec gateway python scripts/rollup_usage_daily.py`.

Don't run `deploy/setup.sh` on the new server: it installs and starts the systemd gateway, which would compete for port 8050.

## The drill

On **another machine**, with a backup of production, walk the whole procedure above: back up, test the restore, build the image, bring the containers up, load the data, verify, tear down. Production is only read and never stops.

The drill answers three questions: does the backup really restore, does the containerized gateway run with production's data and settings, and how long will the real switch take? Every step's outcome goes to `drill.log`, which serves as the audit record (ISO 27001 A.8.13 backup testing, A.8.32 change management).

### Two machines

| | Production host | Drill machine |
|---|---|---|
| Directory | `~/opt/llm-gateway` (the existing install) | a clean checkout, e.g. `~/llm-gateway-drill` |
| Runs | `1-bundle.sh` only | `2` to `7` |
| What changes | one more backup, plus the bundle | the drill environment (`7-teardown.sh` removes all of it) |

The drill machine needs:

- Docker (with compose), `gpg` and `curl`;
- a PostgreSQL image of production's major version (default `postgres:16`);
- a checkout of this repository **at the version you intend to deploy**;
- the private key that decrypts the backup.

The drill machine **must not be the production host**. Steps `2` to `7` refuse to run when they find any of these:

- a running `llm-gateway` systemd service;
- an `llm-gateway-pg` / `llm-gateway` container the drill did not create;
- an existing `.env`, `config.toml` or `deploy/pgdata`.

### Steps

| # | Where | Command | What it does | Passes when |
|---|---|---|---|---|
| 1 | production | `bash deploy/migration/1-bundle.sh` | Online database backup; packs `config.toml`, the public key and a cleaned `.env`; records a snapshot | an encrypted bundle exists |
| 2 | drill machine | `bash deploy/migration/2-restore-test.sh` | Checks the file hashes; restores the backup into a throwaway container and inspects it | the restore succeeds, with its rows and triggers |
| 3 | drill machine | `bash deploy/migration/3-image.sh` | Builds the gateway image (or loads one built elsewhere) | the image loads the code and lists the migrations |
| 4 | drill machine | `bash deploy/migration/4-up.sh` | **Containers first:** unpacks the settings → starts PostgreSQL → creates the schema in an empty database → starts the gateway container | the gateway is ready on an empty database |
| 5 | drill machine | `bash deploy/migration/5-load-data.sh` | **Data last:** calls `load-backup.sh` | the gateway is ready with production's data |
| 6 | drill machine | `bash deploy/migration/6-verify.sh` | Checks everything against the snapshot; estimates the downtime | every line is ✔ |
| 7 | drill machine | `bash deploy/migration/7-teardown.sh` | Deletes all data in the drill environment | nothing is left |

A failing step stops there; nothing continues on its own. Fix the cause and rerun that step; `5` can be repeated, and to redo `4`, run `7` first.

Steps 4 and 5 use the same compose files and the same `load-backup.sh` as the real move (compose project `llmgw-drill`), so the drill rehearses the actual commands.

### 1. Bundle, on the production host

```bash
BACKUP_GPG_RECIPIENT_FILE=~/opt/llm-gateway/keys/backup.pub.asc bash deploy/migration/1-bundle.sh
```

It writes `~/backup/llm-gateway/migration-bundle-<time>/`:

| File | Contents |
|---|---|
| `llm-gateway-<time>.dump.gpg` | The database backup (made by `deploy/backup-db.sh`, so it is also a normal daily backup) |
| `state.tar.gz.gpg` | `config.toml`, `keys/public.pem`, the cleaned `.env` |
| `snapshot.env` | The state at backup time: row counts, schema version, PostgreSQL version, the hash of `config.toml`. No secrets |
| `SHA256SUMS` | Lets the drill machine confirm nothing was damaged in transit |

**`.env` is cleaned first.** These settings never leave production:

- `EXCHANGE_*`: with them the drill environment would **send real mail to users**;
- `LANGFUSE_*`: the drill's requests would land in production's traces;
- `USAGE_LOG_RETENTION_DAYS`: the retention job would delete rows from the drill database;
- `DATABASE_URL` and host paths: the drill generates its own.

The bundle holds every API key and downstream secret, so it **must be encrypted** (same public key as the backups); without a key the script refuses to run. If it fails partway, the half-made directory is removed.

Carry the whole directory to the drill machine:

```bash
rsync -a ~/backup/llm-gateway/migration-bundle-<time> user@drill-host:~/
```

### 2 to 6. On the drill machine

Settings go in `drill.env` (copy `drill.env.example`) or in the environment; a variable given on the command line wins. At least `BUNDLE` (the bundle directory) and `DRILL_GPG_KEY` (the private key file).

```bash
bash deploy/migration/2-restore-test.sh
```

```bash
bash deploy/migration/3-image.sh
```

```bash
bash deploy/migration/4-up.sh
```

```bash
bash deploy/migration/5-load-data.sh
```

```bash
bash deploy/migration/6-verify.sh
```

- **The private key** is imported into a temporary directory for the run and deleted afterwards; it never enters the drill machine's keyring. gpg asks for its passphrase on the terminal.
- **No internet on the drill machine:** bring a `docker save` file and run step 3 with `IMAGE_TAR=<file>`.
- **The image that passed the drill is the image to deploy:** take the same tag (or the same `docker save` file); do not rebuild.
- Between steps 4 and 5 the gateway runs on an empty database: the state of a new server that is prepared and waiting for the switch.
- If `5432` / `8050` are taken on the drill machine, set `PG_PORT` / `GATEWAY_PORT`.

### What step 6 checks

| Area | Checks |
|---|---|
| Container | running, `/readyz`, `/healthz`, host networking, non-root, listening on `127.0.0.1` only |
| Data | schema is the image's latest; each table restored at least as many rows as the snapshot; no concurrency leases left over from the old host; `audit_log`'s append-only triggers are present |
| Settings | `config.toml` has production's hash; the gateway reads the mounted file (not the built-in example); the config directory accepts a write and a rename; logs reach the host; the public key is readable |
| No outside effects | no mail / Langfuse / retention settings in the container; mail is off |
| API | a temporary account's key authenticates; `/v1/models` lists models; a wrong key gets 401; the gateway sees the `X-Forwarded-For` client address; the web UI answers 401 without a login |
| Model (optional) | `DRILL_TEST_MODEL=<alias>`: one `max_tokens=8` request must return 200 and appear in `usage_logs` |

Row counts are compared as "at least", because the snapshot is taken just before the backup starts while production keeps serving requests. `DRILL_TEST_MODEL` is unset by default: the drill machine may not reach vLLM.

**Not covered by the drill**; confirm these after the real switch: the nginx and oauth2-proxy login flow, Azure / Bedrock models (they cost money), and behavior under real load.

### Reading the downtime

Step 6 ends with the figure "backup + data load". "Data load" is the measured time step 5 took from stopping the gateway to having it ready again, covering restore, migration and start. Add the time to copy the backup file to the new server and to switch traffic. It is measured with production's data volume, so it can go straight into the change request.

### 7. Tear down

```bash
bash deploy/migration/7-teardown.sh
```

The drill environment is a full copy of production's data (every API key): **always run this when done**. It removes the containers, volumes, the restored database, `config/`, `keys/`, `logs/`, `.env` and `deploy/.env`, then confirms nothing is left. `--image` removes the image too.

Only the records stay (`drill.log`, `restore-drill.log`); they hold no data and no secrets. **The bundle and the private key file live outside this directory: delete them separately.**

### Records

`drill.log` has one tab-separated line per step, failed ones included:

```
2026-11-11 16:26:22  step=4-up         result=OK  elapsed=10s  by=alice@drill-host  bundle=migration-bundle-2026-11-11_162514 empty_migrate=2s start=5s ...
2026-11-11 16:26:33  step=5-load-data  result=OK  elapsed=10s  by=alice@drill-host  restore=1s migrate=2s start=5s total=10s schema=07899844c700->c1a7e4b9d2f6 users=200
2026-11-11 16:26:36  step=6-verify     result=OK  elapsed=4s   by=alice@drill-host  passed=30 failed=0 downtime_move=11s ...
```

`1-bundle.sh` leaves its line in the `drill.log` on the production host. File the log together with this sheet:

| Item | |
|---|---|
| Date / run by | |
| Code version rehearsed (commit) | |
| Image (tag and the id `3-image.sh` recorded) | |
| Bundle (directory name) | |
| Schema: at backup → after migration | |
| Step 6 result (passed / failed checks) | |
| Estimated downtime | |
| Problems found and what was done | |
| Step 7 run; bundle and private key deleted | |

## Alternative: switching in place

The database stays where it is; only the gateway moves from systemd to a container. **This changes the existing service**; unless there is a reason to stay on the same host, prefer the new-server procedure above.

```bash
cd ~/opt/llm-gateway
bash deploy/backup-db.sh                                   # 1. back up
mkdir -p config && mv config.toml config/                  # 2. config into its directory
echo "CONFIG_DIR=$HOME/opt/llm-gateway/config" >> .env     #    the systemd unit reads it there too (rollback)
cat deploy/migration/gateway.env.example >> deploy/.env    # 3. then set GATEWAY_UID / GATEWAY_GID to id -u / id -g
bash deploy/migration/compose.sh build gateway             # 4. build (or docker load)
systemctl --user disable --now llm-gateway                 # 5. stop systemd: both use :8050
bash deploy/migration/compose.sh up -d                     # 6. PostgreSQL and oauth2-proxy are not recreated
```

**Rollback:** `bash deploy/migration/compose.sh rm -sf gateway migrate`, then `systemctl --user enable --now llm-gateway`. Both read the same `config/config.toml`. A migration that already ran stays applied; that is fine as long as the code being rolled back to is at or after it.

## Troubleshooting

| Message | Cause and fix |
|---|---|
| `看起來是正式主機，拒絕執行` | This machine has production's containers, systemd service or settings. Use another machine, or a clean checkout |
| `演練包的檔案和 SHA256SUMS 不符` | Damaged or changed in transit: copy it again from production |
| `無法解開 state.tar.gz.gpg` / `無法解密` | No private key: set `DRILL_GPG_KEY` (`BACKUP_GPG_KEY` for `load-backup.sh`) |
| `PostgreSQL 主版本不同` | Use an image of production's major version |
| `127.0.0.1:5432 已經有服務在聽` | Set `PG_PORT` / `GATEWAY_PORT` |
| `migration 失敗` | Read the migrate output the script printed. This is what the drill is for: fix it before the real move |
| `演練環境已經存在` | Run `7-teardown.sh` first |
| `資料還沒搬進來` | Run `5-load-data.sh` first |
| `需要確認：在終端機執行，或加 --yes` | `load-backup.sh` wants the database name typed; in a script, pass `--yes` |

The scripts print their messages in Traditional Chinese, like the other scripts under `deploy/`.
