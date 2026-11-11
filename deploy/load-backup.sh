#!/bin/bash
# 把一份備份載入「這台主機」的 PostgreSQL 容器，跑 migration，啟動 gateway 容器
#
# 用法:
#   bash deploy/load-backup.sh FILE          # FILE：backup-db.sh 產生的 .dump 或 .dump.gpg
#   bash deploy/load-backup.sh FILE --yes    # 不詢問（腳本裡用）
#
# 用途：搬到新主機時的最後一步。新主機上的容器（PostgreSQL、gateway 等）先全部開好、
# 確認沒問題，資料最後才搬過來：
#   舊主機：停掉 gateway → bash deploy/backup-db.sh → 把備份檔複製到新主機
#   新主機：bash deploy/load-backup.sh <備份檔>
# 流程見 deploy/README 的「搬到新主機」；deploy/migration-drill/ 用同一支腳本演練。
#
# ⚠ 這會「取代」這台主機上的資料庫：現有的資料庫整個刪除，換成備份的內容。
# 所以：
#   - 先列出現有資料庫的內容，要你輸入資料庫名稱確認（或 --yes）
#   - 現有資料庫不是空的時，先另存一份到 <BACKUP_DIR>/before-load/（可以還原回去）
#   - 這台主機有執行中的 llm-gateway systemd 服務時拒絕執行
#
# 依序做，並印出每一段花的時間：
#   1. 停掉 gateway 容器（還原期間不能有連線，也不該有新的寫入）
#   2. 刪除並重建資料庫
#   3. pg_restore（.gpg 先解密），清掉舊主機留下的並發 lease
#   4. migration：compose 的 migrate 服務（alembic upgrade head）
#   5. 啟動 gateway 容器，等 /readyz 通過
#
# 可用環境變數：
#   BACKUP_GPG_KEY   解密用的私鑰檔；匯入暫時的 GnuPG 目錄，結束即刪除。不設時用目前使用者的 keyring
#   BACKUP_DIR       另存現有資料庫的位置（預設 ~/backup/llm-gateway），紀錄檔 load-backup.log 也在這裡
#   PG_CONTAINER     PostgreSQL 容器名稱（預設 llm-gateway-pg）
#   ENV_FILE         讀取 PG_USER / PG_DB / GATEWAY_PORT 的檔案（預設 deploy/.env）
#   COMPOSE_PROJECT_NAME   compose project 名稱（docker compose 自己會讀；演練用）
#   LOAD_SUMMARY_FILE      把結果寫成 KEY=value 的檔案（演練用）
#
# 結束碼：0 成功（gateway 已就緒）；非 0 失敗。還原失敗時 gateway 不會被啟動。
set -euo pipefail

if [ -t 1 ]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; CYAN=''; NC=''
fi
ts()   { date '+%Y-%m-%d %H:%M:%S'; }
info() { echo -e "$(ts) ${CYAN}▸${NC} $*"; }
ok()   { echo -e "$(ts) ${GREEN}✔${NC} $*"; }
warn() { echo -e "$(ts) ${YELLOW}⚠${NC} $*"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PG_CONTAINER="${PG_CONTAINER:-llm-gateway-pg}"
GW_CONTAINER="llm-gateway"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backup/llm-gateway}"
LOAD_LOG="$BACKUP_DIR/load-backup.log"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/.env}"
TABLES="users usage_logs usage_daily audit_log app_owners ip_allow_rules client_violations anomaly_events"

FILE=""; YES=0
for a in "$@"; do
    case "$a" in
        --yes) YES=1 ;;
        -*) echo "不認得的參數: $a" >&2; exit 2 ;;
        *) FILE="$a" ;;
    esac
done

START=$(date +%s); GNUPG_TMP=""; ERR_FILE=""; DETAIL=""
record() {
    mkdir -p "$BACKUP_DIR" 2>/dev/null || return 0
    printf '%s\tresult=%s\tfile=%s\tby=%s@%s\telapsed=%ss\t%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" \
        "${FILE:--}" "$(id -un)" "$(hostname)" "$(( $(date +%s) - START ))" "$2" >> "$LOAD_LOG" || true
}
die() { echo -e "$(ts) ${RED}✘${NC} $*" >&2; record FAIL "${DETAIL:+$DETAIL }error=$*"; exit 1; }
cleanup() {
    [ -z "$ERR_FILE" ] || rm -f "$ERR_FILE"
    if [ -n "$GNUPG_TMP" ]; then
        gpgconf --homedir "$GNUPG_TMP" --kill gpg-agent >/dev/null 2>&1 || true
        rm -rf "$GNUPG_TMP"
    fi
}
trap cleanup EXIT

[ -n "$FILE" ] || { echo "用法: bash deploy/load-backup.sh FILE [--yes]" >&2; exit 2; }
umask 077
[ -f "$FILE" ] && [ -s "$FILE" ] || die "找不到備份檔，或檔案是空的: $FILE"
command -v docker >/dev/null || die "找不到 docker 指令"
command -v curl >/dev/null || die "找不到 curl 指令"
[ -f "$ENV_FILE" ] || die "找不到 $ENV_FILE"
env_value() { { grep -E "^$1=" "$ENV_FILE" || true; } | tail -n1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//'; }
PG_USER="$(env_value PG_USER)"; PG_DB="$(env_value PG_DB)"
GATEWAY_PORT="$(env_value GATEWAY_PORT)"; GATEWAY_PORT="${GATEWAY_PORT:-8050}"
[ -n "$PG_USER" ] && [ -n "$PG_DB" ] || die "$ENV_FILE 裡缺少 PG_USER 或 PG_DB"
case "$PG_DB$PG_USER" in *[!A-Za-z0-9_]*) die "PG_USER / PG_DB 只能是英數字與底線" ;; esac

compose() { docker compose -f "$SCRIPT_DIR/docker-compose.yml" "$@"; }
psql_db() { docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -At -v ON_ERROR_STOP=1 -c "$1"; }
psql_admin() { docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d postgres -At -v ON_ERROR_STOP=1 -c "$1"; }
table_exists() { [ "$(psql_db "SELECT to_regclass('public.$1') IS NOT NULL")" = "t" ]; }

docker inspect -f '{{.State.Running}}' "$PG_CONTAINER" 2>/dev/null | grep -q true \
    || die "PostgreSQL 容器 $PG_CONTAINER 沒有在執行：先把這台主機的容器開好（docker compose up -d）"
if command -v systemctl >/dev/null && systemctl --user is-active --quiet llm-gateway 2>/dev/null; then
    die "這台主機有執行中的 llm-gateway systemd 服務。這支腳本會取代資料庫，只用在以容器執行 gateway 的主機；確定要做請先停掉它"
fi
image="$(compose --profile gateway config --images 2>/dev/null | grep -v -e postgres -e oauth2 | head -n1 || true)"
[ -n "$image" ] && docker image inspect "$image" >/dev/null 2>&1 \
    || die "找不到 gateway 映像 ${image:-（compose 設定讀不到）}：先建置或 docker load（deploy/.env 的 GATEWAY_IMAGE）"

# ── 解密準備 ──
if [[ "$FILE" == *.gpg ]]; then
    command -v gpg >/dev/null || die "找不到 gpg 指令，無法解密 $FILE"
    if [ -n "${BACKUP_GPG_KEY:-}" ]; then
        [ -r "$BACKUP_GPG_KEY" ] || die "讀不到 BACKUP_GPG_KEY: $BACKUP_GPG_KEY"
        GNUPG_TMP="$(mktemp -d)"
        gpg --homedir "$GNUPG_TMP" --batch --quiet --import "$BACKUP_GPG_KEY" 2>/dev/null \
            || die "無法匯入 BACKUP_GPG_KEY（不是私鑰檔？）"
        export GNUPGHOME="$GNUPG_TMP"
    fi
    if [ -t 0 ]; then GPG_TTY="$(tty)"; export GPG_TTY; fi
    # 先確認解得開，再去動資料庫
    # （子 shell 關掉 pipefail：head 讀完 5 個位元組就關閉管線，gpg 會因此以非 0 結束）
    ( set +o pipefail; gpg --quiet --decrypt "$FILE" 2>/dev/null | head -c 5 | grep -q '^PGDMP' ) \
        || die "無法解密 $FILE，或它不是 pg_dump 的備份（沒有對應的私鑰？設 BACKUP_GPG_KEY）"
else
    head -c 5 "$FILE" | grep -q '^PGDMP' || die "$FILE 不是 pg_dump -Fc 的備份檔"
fi
plain_stream() { if [[ "$FILE" == *.gpg ]]; then gpg --quiet --decrypt "$FILE"; else cat "$FILE"; fi; }

# ── 現有的資料庫：讓人看清楚要被取代的是什麼 ──
cur_users=0; cur_usage=0; cur_latest="-"; cur_tables=0
if [ "$(psql_admin "SELECT count(*) FROM pg_database WHERE datname = '$PG_DB'")" = "1" ]; then
    cur_tables="$(psql_db "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")"
    if table_exists users; then cur_users="$(psql_db 'SELECT count(*) FROM users')"; fi
    if table_exists usage_logs; then
        cur_usage="$(psql_db 'SELECT count(*) FROM usage_logs')"
        cur_latest="$(psql_db "SELECT coalesce(max(created_at)::text, '-') FROM usage_logs")"
    fi
fi
echo
warn "即將取代這台主機（$(hostname)）的資料庫「$PG_DB」："
echo "    現有內容：$cur_tables 張表，users=$cur_users，usage_logs=$cur_usage，最新一筆用量 $cur_latest"
echo "    換成備份：$FILE"
echo
if [ "$YES" -ne 1 ]; then
    [ -t 0 ] || die "需要確認：在終端機執行，或加 --yes"
    read -r -p "確定的話請輸入資料庫名稱（$PG_DB）： " answer
    [ "$answer" = "$PG_DB" ] || die "輸入不符，沒有做任何變更"
fi

# ── 現有資料庫不是空的：先另存一份 ──
saved="-"
if [ "$cur_users" -gt 0 ] || [ "$cur_usage" -gt 0 ]; then
    mkdir -p "$BACKUP_DIR/before-load"; chmod 700 "$BACKUP_DIR" "$BACKUP_DIR/before-load"
    saved="$BACKUP_DIR/before-load/llm-gateway-before-load-$(date '+%Y-%m-%d_%H%M%S').dump"
    info "現有資料庫不是空的，先另存到 $saved"
    docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" -Fc > "$saved.partial" \
        || { rm -f "$saved.partial"; die "另存現有資料庫失敗，沒有做任何變更"; }
    mv "$saved.partial" "$saved"
    warn "這份另存檔沒有加密（內含 API key），確認新資料沒問題後請刪除"
fi

# ── 1. 停掉 gateway ──
t_all=$(date +%s)
if docker inspect "$GW_CONTAINER" >/dev/null 2>&1; then
    info "停止 gateway 容器"
    docker stop "$GW_CONTAINER" >/dev/null || die "無法停止 gateway 容器"
fi

# ── 2. 刪除並重建資料庫 ──
info "刪除並重建資料庫 $PG_DB"
psql_admin "DROP DATABASE IF EXISTS \"$PG_DB\" WITH (FORCE)" >/dev/null || die "無法刪除資料庫 $PG_DB"
psql_admin "CREATE DATABASE \"$PG_DB\" OWNER \"$PG_USER\"" >/dev/null || die "無法建立資料庫 $PG_DB"

# ── 3. 還原 ──
info "還原中..."
t=$(date +%s)
ERR_FILE="$(mktemp)"
set +e
plain_stream | docker exec -i "$PG_CONTAINER" \
    pg_restore -U "$PG_USER" -d "$PG_DB" --no-owner --no-acl --exit-on-error 2> "$ERR_FILE"
st=("${PIPESTATUS[@]}")
set -e
DETAIL="saved_before=$saved"
[ "${st[1]}" -eq 0 ] || die "還原失敗，資料庫不完整，gateway 沒有啟動: $(tail -n 3 "$ERR_FILE" | tr '\n' ' ')"
[ "${st[0]}" -eq 0 ] || die "讀取或解密備份失敗，資料庫不完整，gateway 沒有啟動"
restore_s=$(( $(date +%s) - t ))
table_exists alembic_version || die "還原結果沒有 alembic_version：不是 gateway 的備份？gateway 沒有啟動"
schema_before="$(psql_db "SELECT string_agg(version_num, ',') FROM alembic_version")"
# 舊主機的 worker 留下的並發 lease：留著會讓使用者在前三分鐘被誤判超過上限
if table_exists concurrency_leases; then psql_db "DELETE FROM concurrency_leases" >/dev/null; fi
counts=""
for tb in $TABLES; do
    if table_exists "$tb"; then counts="$counts count_$tb=$(psql_db "SELECT count(*) FROM $tb")"; fi
done
counts="${counts# }"
ok "還原完成（${restore_s}s，schema $schema_before）：${counts//count_/}"

# ── 4. Migration ──
info "執行 migration（alembic upgrade head）"
t=$(date +%s)
compose --profile gateway up --no-deps --exit-code-from migrate migrate > "$ERR_FILE" 2>&1 \
    || { tail -n 15 "$ERR_FILE" >&2; die "migration 失敗，gateway 沒有啟動（上面是 migrate 的輸出）"; }
migrate_s=$(( $(date +%s) - t ))
schema_after="$(psql_db "SELECT string_agg(version_num, ',') FROM alembic_version")"
if [ "$schema_before" = "$schema_after" ]; then
    ok "migration 完成（${migrate_s}s）：schema 已是最新 $schema_after"
else
    ok "migration 完成（${migrate_s}s）：schema $schema_before → $schema_after"
fi

# ── 5. Gateway ──
info "啟動 gateway 容器"
t=$(date +%s)
compose --profile gateway up -d gateway >/dev/null 2>&1 || die "無法啟動 gateway（docker compose up gateway）"
ready=0
for _ in $(seq 1 90); do
    if curl -fsS -m 3 -o /dev/null "http://127.0.0.1:$GATEWAY_PORT/readyz" 2>/dev/null; then ready=1; break; fi
    state="$(docker inspect -f '{{.State.Status}}' "$GW_CONTAINER" 2>/dev/null || echo missing)"
    if [ "$state" = exited ] || [ "$state" = missing ]; then
        docker logs --tail 20 "$GW_CONTAINER" >&2 2>/dev/null || true
        die "gateway 容器啟動後就結束了（狀態 $state）"
    fi
    sleep 1
done
[ "$ready" -eq 1 ] || { docker logs --tail 20 "$GW_CONTAINER" >&2 || true; die "gateway 90 秒內沒有就緒"; }
start_s=$(( $(date +%s) - t ))
total_s=$(( $(date +%s) - t_all ))

if [ -n "${LOAD_SUMMARY_FILE:-}" ]; then
    {
        echo "schema_version=$schema_before"
        echo "schema_after=$schema_after"
        for kvp in $counts; do echo "$kvp"; done
        echo "restore_seconds=$restore_s"
        echo "migrate_seconds=$migrate_s"
        echo "start_seconds=$start_s"
        echo "load_seconds=$total_s"
    } > "$LOAD_SUMMARY_FILE"
fi
DETAIL="restore=${restore_s}s migrate=${migrate_s}s start=${start_s}s total=${total_s}s schema=$schema_before->$schema_after $counts saved_before=$saved"
record OK "$DETAIL"
ok "完成：gateway 已就緒（http://127.0.0.1:$GATEWAY_PORT）。這台主機上從停止到就緒共 ${total_s}s"
info "還原 ${restore_s}s ／ migration ${migrate_s}s ／ 啟動 ${start_s}s；紀錄：$LOAD_LOG"
