# Migration drill: moving the gateway into a container

[繁體中文](README.zh-TW.md)

On **another machine**, with a backup of production, walk the whole "systemd → container" move end to end: back up, test the restore, build the image, bring the containers up, run the migrations, verify, tear down. Production is only read (one extra online backup) and never stops.

The drill answers three questions:

1. Does the backup really restore?
2. Does the containerized gateway run with production's data and settings?
3. How long will the real switch take?

Every step's outcome goes to `drill.log`, which serves as the audit record (ISO 27001 A.8.13 backup testing, A.8.32 change management).

## Two machines

| | Production host | Drill machine |
|---|---|---|
| Directory | `~/opt/llm-gateway` (the existing install) | a clean checkout, e.g. `~/llm-gateway-drill` |
| Runs | `1-bundle.sh` only | `2` to `6` |
| What changes | one more backup, plus the bundle | the drill environment (`6-teardown.sh` removes all of it) |

The drill machine needs:

- Docker (with compose), `gpg` and `curl`.
- A PostgreSQL image of production's major version (default `postgres:16`).
- A checkout of this repository **at the version you intend to deploy**: the drill's migrations and image come from it.
- The private key that decrypts the backup.

The drill machine **must not be the production host**. Steps `2` to `6` refuse to run when they find any of these:

- a running `llm-gateway` systemd service;
- an `llm-gateway-pg` / `llm-gateway` container the drill did not create;
- an existing `.env`, `config.toml` or `deploy/pgdata`.

## Steps

| # | Where | Command | What it does | Passes when |
|---|---|---|---|---|
| 1 | production | `bash deploy/migration-drill/1-bundle.sh` | Online database backup; packs `config.toml`, the public key and a cleaned `.env`; records a snapshot | an encrypted bundle exists |
| 2 | drill machine | `bash deploy/migration-drill/2-restore-test.sh` | Checks the file hashes; restores the backup into a throwaway container and inspects it | the restore succeeds, with its rows and triggers |
| 3 | drill machine | `bash deploy/migration-drill/3-image.sh` | Builds the gateway image (or loads one built elsewhere) | the image loads the code and lists the migrations |
| 4 | drill machine | `bash deploy/migration-drill/4-up.sh` | Unpacks the settings → starts PostgreSQL → restores → migrates → starts the gateway container | the gateway is ready |
| 5 | drill machine | `bash deploy/migration-drill/5-verify.sh` | Checks everything against the snapshot; estimates the downtime | every line is ✔ |
| 6 | drill machine | `bash deploy/migration-drill/6-teardown.sh` | Deletes all data in the drill environment | nothing is left |

A failing step stops there; nothing continues on its own. Fix the cause and rerun that step; to redo `4`, run `6` first.

### 1. Bundle, on the production host

```bash
cd ~/opt/llm-gateway
BACKUP_GPG_RECIPIENT_FILE=~/opt/llm-gateway/keys/backup.pub.asc bash deploy/migration-drill/1-bundle.sh
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

The bundle holds every API key and downstream secret, so it **must be encrypted** (same public key as the backups, see [deploy/README](../README.md#encryption)); without a key the script refuses to run.

Carry the whole directory to the drill machine:

```bash
rsync -a ~/backup/llm-gateway/migration-bundle-<time> user@drill-host:~/
```

### 2 to 5. On the drill machine

Settings go in `drill.env` (copy `drill.env.example`) or in the environment; a variable given on the command line wins.

```bash
cd ~/llm-gateway-drill
cp deploy/migration-drill/drill.env.example deploy/migration-drill/drill.env
# edit drill.env: BUNDLE=<bundle directory>, DRILL_GPG_KEY=<private key file>
```

```bash
bash deploy/migration-drill/2-restore-test.sh
```

```bash
bash deploy/migration-drill/3-image.sh
```

```bash
bash deploy/migration-drill/4-up.sh
```

```bash
bash deploy/migration-drill/5-verify.sh
```

- **The private key** (`DRILL_GPG_KEY`) is imported into a temporary directory for the run and deleted afterwards; it never enters the drill machine's keyring. gpg asks for its passphrase on the terminal.
- **No internet on the drill machine:** `docker build` and `docker save` elsewhere, bring the file, and run step 3 with `IMAGE_TAR=<file>`. For an internal mirror, set `PYTHON_IMAGE` / `UV_IMAGE`.
- **The image that passed the drill is the image to deploy:** take the same tag (or the same `docker save` file) to production; do not rebuild.
- Step 4 uses the same `deploy/docker-compose.yml` as production (compose project `llmgw-drill`), so what is rehearsed is what will run.
- If `5432` / `8050` are taken on the drill machine, set `PG_PORT` / `GATEWAY_PORT`.

### What step 5 checks

| Area | Checks |
|---|---|
| Container | running, `/readyz`, `/healthz`, host networking, non-root, listening on `127.0.0.1` only |
| Data | schema is the image's latest; each table restored at least as many rows as the snapshot; `audit_log`'s append-only triggers are present |
| Settings | `config.toml` has production's hash; the gateway reads the mounted file (not the built-in example); the config directory accepts a write and a rename (how the admin page saves); logs reach the host; the public key is readable |
| No outside effects | no mail / Langfuse / retention settings in the container; mail is off |
| API | a temporary account's key authenticates; `/v1/models` lists models; a wrong key gets 401; the gateway sees the `X-Forwarded-For` client address; the web UI answers 401 without a login |
| Model (optional) | `DRILL_TEST_MODEL=<alias>`: one `max_tokens=8` request must return 200 and appear in `usage_logs` |

Row counts are compared as "at least", because the snapshot is taken just before the backup starts while production keeps serving requests.

`DRILL_TEST_MODEL` is unset by default: the drill machine may not reach vLLM. To use it, give a local (vLLM) model's alias and make sure the drill machine can reach that server.

**Not covered by the drill**; confirm these after the real switch:

- the nginx and oauth2-proxy login flow (the drill machine has neither);
- Azure / Bedrock models (they cost money; the drill does not call them);
- behavior under real load.

### Reading the downtime

Step 5 ends with two figures:

- **Switching in place** (the database stays; only the gateway moves from systemd to a container): migration + start. This is the usual case.
- **Moving to a new host** (data moves too): backup + restore + migration + start, plus the time to copy the files.

Both are measured with production's data volume, so they can go straight into the change request.

### 6. Tear down

```bash
bash deploy/migration-drill/6-teardown.sh
```

The drill environment is a full copy of production's data (every API key): **always run this when done**. It removes the containers, volumes, the restored database (`deploy/pgdata`), `config/`, `keys/`, `logs/`, `.env` and `deploy/.env`, then confirms nothing is left. `--image` removes the image too.

Only the records stay (`drill.log`, `restore-drill.log`); they hold no data and no secrets. **The bundle and the private key file live outside this directory: delete them separately.**

## Records

`drill.log` has one tab-separated line per step, failed ones included:

```
2026-11-11 15:08:46  step=4-up      result=OK  elapsed=11s  by=alice@drill-host  bundle=migration-bundle-2026-11-11_150722 restore=1s migrate=1s start=5s schema=07899844c700->c1a7e4b9d2f6 ...
2026-11-11 15:08:50  step=5-verify  result=OK  elapsed=4s   by=alice@drill-host  passed=29 failed=0 downtime_inplace=6s downtime_move=8s ...
```

`1-bundle.sh` leaves its line in the `drill.log` on the production host. File the log together with this sheet:

| Item | |
|---|---|
| Date / run by | |
| Code version rehearsed (commit) | |
| Image (tag and the id `3-image.sh` recorded) | |
| Bundle (directory name) | |
| Schema: at backup → after migration | |
| Step 5 result (passed / failed checks) | |
| Estimated downtime (in place / new host) | |
| Problems found and what was done | |
| Step 6 run; bundle and private key deleted | |

## After a passing drill: the real switch and rollback

The switch itself is in [deploy/README, "Running the gateway in a container"](../README.md#running-the-gateway-in-a-container), and so is the rollback: stop the container and re-enable the systemd service; both read the same `config/config.toml`.

Before switching, confirm that:

- the image is the one that passed the drill;
- `deploy/backup-db.sh` ran just before;
- `GATEWAY_UID` / `GATEWAY_GID` in `deploy/.env` are the user owning `config/` and `logs/`;
- the announced downtime is at least what the drill measured.

## Troubleshooting

| Message | Cause and fix |
|---|---|
| `看起來是正式主機，拒絕執行` | This machine has production's containers, systemd service or settings. Use another machine, or a clean checkout |
| `演練包的檔案和 SHA256SUMS 不符` | Damaged or changed in transit: copy it again from production |
| `無法解開 state.tar.gz.gpg` / `解密失敗` | No private key: set `DRILL_GPG_KEY` |
| `PostgreSQL 主版本不同` | Use an image of production's major version |
| `127.0.0.1:5432 已經有服務在聽` | Set `PG_PORT` / `GATEWAY_PORT` |
| `migration 失敗` | See `deploy/migration-drill/.state/migrate.log`. This is what the drill is for: fix it before upgrading production |
| `演練環境已經存在` | Run `6-teardown.sh` first |

The scripts print their messages in Traditional Chinese, like the other scripts under `deploy/`.
