#!/bin/bash
# LLM Gateway 備份還原演練：把一份備份真的還原到一次性的容器，檢查內容，留下紀錄
#
# 用法:
#   bash deploy/restore-drill.sh                 # 演練備份目錄裡最新的一份
#   bash deploy/restore-drill.sh FILE            # 演練指定的備份檔（.dump 或 .dump.gpg）
#
# 為什麼要做：backup-db.sh 的 pg_restore --list 只證明檔案讀得出來，不證明還原得回來。
# 這支腳本才是「備份可用」的證據（ISO 27001 A.8.13 要求定期測試備份），建議每季一次，
# 以及每次改動備份方式之後。
#
# 做法：
#   1. 用和正式 PostgreSQL 容器相同的映像，起一個沒有網路（--network none）的一次性容器
#   2. 解密（.gpg）後以 pg_restore --exit-on-error 還原，記下花費時間（RTO 的參考）
#   3. 檢查：schema 版本、主要資料表筆數、最新一筆用量時間、audit_log 的防竄改觸發器
#   4. 刪除容器與它的資料 volume（不留任何還原出來的資料），結果附加到演練紀錄檔
# 不會連線或改動正式資料庫。
#
# 加密備份需要私鑰：
#   DRILL_GPG_KEY=/path/private.asc  私鑰檔（例如負責人帶來的隨身碟）。腳本把它匯入一個
#                                    暫時的 GnuPG 目錄，結束時連同目錄刪除，不會留在本機。
#   不設時用目前使用者 keyring 裡的私鑰。有密碼時 gpg 會在終端機詢問。
#
# 可用環境變數覆蓋：
#   BACKUP_DIR      備份目錄（預設 ~/backup/llm-gateway），沒給 FILE 時從 daily/ 找最新一份
#   DRILL_LOG       演練紀錄檔（預設 <BACKUP_DIR>/restore-drill.log）
#   PG_CONTAINER    正式 PostgreSQL 容器名稱，用來取得相同映像（預設 llm-gateway-pg）
#   DRILL_IMAGE     直接指定映像（正式容器不在這台主機時用，例如 postgres:16）
#
# 結束碼：0 演練成功；非 0 失敗（失敗也會寫進紀錄檔）。
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

PG_CONTAINER="${PG_CONTAINER:-llm-gateway-pg}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backup/llm-gateway}"
DRILL_LOG="${DRILL_LOG:-$BACKUP_DIR/restore-drill.log}"

FILE=""
CONTAINER=""
GNUPG_TMP=""
DRILL_START=$(date +%s)

# 紀錄檔：一行一次演練，失敗也記（稽核要看到「有做、結果如何」）
record() {
    local result="$1" detail="$2"
    mkdir -p "$(dirname "$DRILL_LOG")"
    local sum="-"
    [ -n "$FILE" ] && [ -f "$FILE" ] && sum="$(sha256sum "$FILE" | cut -c1-16)"
    printf '%s\tresult=%s\tfile=%s\tsha256=%s\tby=%s@%s\t%s\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$result" "${FILE:--}" "$sum" \
        "$(id -un)" "$(hostname)" "$detail" >> "$DRILL_LOG"
}

die() {
    echo -e "$(ts) ${RED}✘${NC} $*" >&2
    record FAIL "elapsed=$(( $(date +%s) - DRILL_START ))s error=$*" || true
    exit 1
}

cleanup() {
    # -v：連同映像宣告的資料 volume 一起刪；docker rm -f 不加 -v 時，--rm 也不會刪它，
    # 還原出來的整份資料庫就會留在主機上
    [ -n "$CONTAINER" ] && docker rm -f -v "$CONTAINER" >/dev/null 2>&1 || true
    if [ -n "$GNUPG_TMP" ]; then
        gpgconf --homedir "$GNUPG_TMP" --kill gpg-agent >/dev/null 2>&1 || true
        rm -rf "$GNUPG_TMP"
    fi
}
trap cleanup EXIT

umask 077
command -v docker >/dev/null || die "找不到 docker 指令"

# ── 要演練哪一份 ──
if [ -n "${1:-}" ]; then
    FILE="$1"
else
    FILE="$(ls -1 "$BACKUP_DIR"/daily/llm-gateway-*.dump "$BACKUP_DIR"/daily/llm-gateway-*.dump.gpg 2>/dev/null \
        | sort | tail -n1 || true)"
    [ -n "$FILE" ] || die "在 $BACKUP_DIR/daily 找不到備份（給 FILE 參數，或設 BACKUP_DIR）"
fi
[ -f "$FILE" ] || die "找不到備份檔: $FILE"
[ -s "$FILE" ] || die "備份檔是空的: $FILE"

# ── 解密準備 ──
if [[ "$FILE" == *.gpg ]]; then
    command -v gpg >/dev/null || die "找不到 gpg 指令，無法解密 $FILE"
    if [ -n "${DRILL_GPG_KEY:-}" ]; then
        [ -r "$DRILL_GPG_KEY" ] || die "讀不到 DRILL_GPG_KEY: $DRILL_GPG_KEY"
        GNUPG_TMP="$(mktemp -d)"
        gpg --homedir "$GNUPG_TMP" --batch --quiet --import "$DRILL_GPG_KEY" 2>/dev/null \
            || die "無法匯入 DRILL_GPG_KEY（不是私鑰檔？）"
        export GNUPGHOME="$GNUPG_TMP"
    fi
    [ -t 0 ] && export GPG_TTY="$(tty)"
fi
# 解密的錯誤另外收起來：失敗時要說的是「解密失敗」，而不是 pg_restore 讀到空輸入
decrypt_err=""
dump_stream() {
    if [[ "$FILE" == *.gpg ]]; then
        gpg --quiet --decrypt "$FILE" 2> "$decrypt_err"
    else
        cat "$FILE"
    fi
}

# ── 和正式環境相同的映像 ──
if [ -n "${DRILL_IMAGE:-}" ]; then
    IMAGE="$DRILL_IMAGE"
elif IMAGE="$(docker inspect -f '{{.Config.Image}}' "$PG_CONTAINER" 2>/dev/null)" && [ -n "$IMAGE" ]; then
    :
else
    die "找不到容器 $PG_CONTAINER 來取得映像（設 PG_CONTAINER，或用 DRILL_IMAGE 指定，例如 postgres:16）"
fi

# ── 一次性容器：沒有網路；結束時連同資料 volume 刪除（cleanup）──
CONTAINER="llm-gateway-restore-drill-$(date '+%Y%m%d%H%M%S')-$$"
DRILL_PASSWORD="$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')"
info "演練 $FILE"
info "啟動一次性容器 $CONTAINER（映像 $IMAGE，無網路）"
docker run -d --rm --name "$CONTAINER" --network none \
    -e POSTGRES_USER=drill -e POSTGRES_PASSWORD="$DRILL_PASSWORD" -e POSTGRES_DB=drill \
    "$IMAGE" >/dev/null || die "無法啟動容器（映像 $IMAGE）"

# 初始化期間的暫時伺服器只聽 unix socket；TCP 通了才是正式啟動完成
ready=0
for _ in $(seq 1 60); do
    if docker exec "$CONTAINER" pg_isready -h 127.0.0.1 -U drill -q 2>/dev/null; then ready=1; break; fi
    sleep 1
done
[ "$ready" -eq 1 ] || die "一次性容器 60 秒內沒有就緒"

psql_drill() {
    docker exec "$CONTAINER" psql -U drill -d drill -At -v ON_ERROR_STOP=1 -c "$1"
}

# ── 還原 ──
info "還原中..."
restore_start=$(date +%s)
restore_err="$(mktemp)"
decrypt_err="$(mktemp)"
set +e
dump_stream | docker exec -i "$CONTAINER" \
    pg_restore -U drill -d drill --no-owner --no-acl --exit-on-error 2> "$restore_err"
statuses=("${PIPESTATUS[@]}")
set -e
dec_msg="$(tail -n 2 "$decrypt_err" | tr '\n' ' ')"
res_msg="$(tail -n 3 "$restore_err" | tr '\n' ' ')"
rm -f "$restore_err" "$decrypt_err"
# pg_restore 先失敗時，前面的 cat / gpg 會因為管線被關閉而非 0，所以先看 gpg 自己有沒有報錯
if [ "${statuses[0]}" -ne 0 ] && [ -n "$dec_msg" ]; then
    die "解密失敗（沒有對應的私鑰、密碼錯誤，或檔案損毀）: $dec_msg"
fi
[ "${statuses[1]}" -eq 0 ] || die "還原失敗: ${res_msg:-pg_restore 失敗}"
[ "${statuses[0]}" -eq 0 ] || die "讀取備份檔失敗（結束碼 ${statuses[0]}）"
restore_s=$(( $(date +%s) - restore_start ))
ok "還原完成（${restore_s}s）"

# ── 檢查內容 ──
table_exists() { [ "$(psql_drill "SELECT to_regclass('public.$1') IS NOT NULL")" = "t" ]; }

table_exists alembic_version || die "還原結果沒有 alembic_version：不是 gateway 的資料庫？"
schema="$(psql_drill "SELECT string_agg(version_num, ',') FROM alembic_version")"
[ -n "$schema" ] || die "alembic_version 是空的"
table_exists users || die "還原結果沒有 users 資料表"

counts=""
for t in users usage_logs usage_daily audit_log; do
    if table_exists "$t"; then
        counts="$counts $t=$(psql_drill "SELECT count(*) FROM $t")"
    else
        counts="$counts $t=missing"
    fi
done
counts="${counts# }"

latest="-"
table_exists usage_logs && latest="$(psql_drill "SELECT coalesce(max(created_at)::text, '-') FROM usage_logs")"

# audit_log 防竄改觸發器要跟著還原回來（少了就等於還原出一個可以改的稽核紀錄）
triggers="-"
if table_exists audit_log; then
    triggers="$(psql_drill "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'audit_log'::regclass AND NOT tgisinternal")"
    [ "$triggers" -ge 2 ] || die "audit_log 的防竄改觸發器沒有還原（只有 $triggers 個）"
fi

users="$(psql_drill "SELECT count(*) FROM users")"
[ "$users" -gt 0 ] || warn "users 是 0 筆：確認這份備份是正式環境的"

elapsed=$(( $(date +%s) - DRILL_START ))
detail="elapsed=${elapsed}s restore=${restore_s}s schema=$schema $counts latest_usage=${latest// /T} audit_triggers=$triggers image=$IMAGE"
record OK "$detail"

ok "演練成功：schema=$schema，$counts"
ok "最新一筆用量：$latest；audit_log 觸發器：$triggers 個"
ok "還原 ${restore_s}s，整體 ${elapsed}s；紀錄已寫入 $DRILL_LOG"
