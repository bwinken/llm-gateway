#!/bin/bash
# LLM Gateway 資料庫線上備份（一鍵執行，可放進 cron）
#
# 用法:
#   bash deploy/backup-db.sh                 # 備份到 ~/backup/llm-gateway
#   bash deploy/backup-db.sh /mnt/nas/llmgw  # 備份到指定目錄
#   bash deploy/backup-db.sh --check FILE    # 只檢查某個備份檔能否讀取
#
# 線上備份：pg_dump 在一個快照交易裡讀完整個資料庫，備份內容一致，
# 不會擋住 gateway 的一般讀寫，服務不用停。它只會擋住改表結構的動作，
# 所以不要和 `alembic upgrade` / `deploy/setup.sh` 同時執行。
#
# 產出：
#   <備份目錄>/daily/llm-gateway-YYYY-MM-DD_HHMMSS.dump   （pg_dump -Fc 壓縮格式）
#   <備份目錄>/monthly/...                                （每月第一份另存一份）
# 保留：最新 KEEP_DAILY 份每日備份、KEEP_MONTHLY 份每月備份，其餘刪除。
#
# 可用環境變數覆蓋：
#   BACKUP_DIR      備份目錄（預設 ~/backup/llm-gateway；第一個參數優先）
#   KEEP_DAILY      保留幾份每日備份（預設 30）
#   KEEP_MONTHLY    保留幾份每月備份（預設 12）
#   PG_CONTAINER    PostgreSQL 容器名稱（預設 llm-gateway-pg）
#   ENV_FILE        讀取 PG_USER / PG_DB 的檔案（預設 deploy/.env）
#   BACKUP_COPY_TO  備份完成後再用 rsync 複製到此（例如 user@nas:/backup/llmgw/），留空則不複製
#
# 還原（到一個新的資料庫，詳見 deploy/README.md 的「資料庫備份」）：
#   docker exec -i llm-gateway-pg pg_restore -U <PG_USER> --create -d postgres < FILE
#
# 結束碼：0 成功；非 0 失敗（失敗時不會留下不完整的備份檔）。
set -euo pipefail

# ── 顏色（輸出到終端機時才上色，cron log 保持乾淨）──
if [ -t 1 ]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; CYAN=''; NC=''
fi
ts()   { date '+%Y-%m-%d %H:%M:%S'; }
info() { echo -e "$(ts) ${CYAN}▸${NC} $*"; }
ok()   { echo -e "$(ts) ${GREEN}✔${NC} $*"; }
warn() { echo -e "$(ts) ${YELLOW}⚠${NC} $*"; }
die()  { echo -e "$(ts) ${RED}✘${NC} $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PG_CONTAINER="${PG_CONTAINER:-llm-gateway-pg}"
KEEP_DAILY="${KEEP_DAILY:-30}"
KEEP_MONTHLY="${KEEP_MONTHLY:-12}"

command -v docker >/dev/null || die "找不到 docker 指令"
docker inspect -f '{{.State.Running}}' "$PG_CONTAINER" 2>/dev/null | grep -q true \
    || die "PostgreSQL 容器 $PG_CONTAINER 沒有在執行（docker ps 確認名稱，或設 PG_CONTAINER）"

# 備份檔可讀：pg_restore --list 能完整列出內容
check_dump() {
    docker exec -i "$PG_CONTAINER" pg_restore --list < "$1" > /dev/null
}

if [ "${1:-}" = "--check" ]; then
    [ -f "${2:-}" ] || die "用法: bash deploy/backup-db.sh --check FILE"
    check_dump "$2" && ok "備份檔可讀取: $2" || die "備份檔無法讀取或已損毀: $2"
    exit 0
fi

# ── 讀 PG_USER / PG_DB ──
if [ -z "${ENV_FILE:-}" ]; then
    for f in "$SCRIPT_DIR/.env" "$HOME/opt/llm-gateway/deploy/.env"; do
        [ -f "$f" ] && { ENV_FILE="$f"; break; }
    done
fi
[ -n "${ENV_FILE:-}" ] && [ -f "$ENV_FILE" ] || die "找不到 deploy/.env（設 ENV_FILE 指定）"
env_value() { grep -E "^$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//'; }
PG_USER="$(env_value PG_USER)"
PG_DB="$(env_value PG_DB)"
[ -n "$PG_USER" ] && [ -n "$PG_DB" ] || die "$ENV_FILE 裡缺少 PG_USER 或 PG_DB"

BACKUP_DIR="${1:-${BACKUP_DIR:-$HOME/backup/llm-gateway}}"
DAILY_DIR="$BACKUP_DIR/daily"
MONTHLY_DIR="$BACKUP_DIR/monthly"

# 備份檔含 API key 與個資：只有自己讀得到
umask 077
mkdir -p "$DAILY_DIR" "$MONTHLY_DIR"
chmod 700 "$BACKUP_DIR" "$DAILY_DIR" "$MONTHLY_DIR"

psql_value() {
    docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -At -c "$1"
}

# ── 不要和 migration 同時跑 ──
if pgrep -f "alembic upgrade" >/dev/null 2>&1; then
    die "偵測到 alembic upgrade 正在執行，請等它結束再備份"
fi

# ── 空間檢查：至少要有資料庫大小的空間（壓縮後通常小很多）──
db_bytes="$(psql_value "SELECT pg_database_size(current_database())")" \
    || die "無法連線到資料庫 $PG_DB（使用者 $PG_USER）"
free_bytes="$(( $(df -Pk "$BACKUP_DIR" | awk 'NR==2 {print $4}') * 1024 ))"
if [ "$free_bytes" -lt "$db_bytes" ]; then
    die "備份目錄剩餘空間 $((free_bytes / 1048576)) MB，小於資料庫大小 $((db_bytes / 1048576)) MB"
fi

stamp="$(date '+%Y-%m-%d_%H%M%S')"
final="$DAILY_DIR/llm-gateway-$stamp.dump"
tmp="$final.partial"
trap 'rm -f "$tmp"' EXIT

info "開始備份 $PG_DB（容器 $PG_CONTAINER，資料庫約 $((db_bytes / 1048576)) MB）"
start=$(date +%s)
docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" -Fc > "$tmp" \
    || die "pg_dump 失敗，未產生備份"
[ -s "$tmp" ] || die "pg_dump 產生的檔案是空的"
check_dump "$tmp" || die "備份檔驗證失敗（pg_restore --list 讀不了）"
mv "$tmp" "$final"
trap - EXIT
elapsed=$(( $(date +%s) - start ))

size="$(du -h "$final" | cut -f1)"
users="$(psql_value "SELECT count(*) FROM users" 2>/dev/null || echo '?')"
version="$(psql_value "SELECT version_num FROM alembic_version" 2>/dev/null || echo '?')"
ok "備份完成：$final（$size，${elapsed}s，users=$users，schema=$version）"

# ── 每月第一份另存 ──
month="$(date '+%Y-%m')"
if ! ls "$MONTHLY_DIR"/llm-gateway-"$month"-*.dump >/dev/null 2>&1; then
    cp -p "$final" "$MONTHLY_DIR/"
    ok "已另存為 $month 的每月備份"
fi

# ── 保留期：刪掉最舊的 ──
prune() {
    local dir="$1" keep="$2" old
    old="$(ls -1 "$dir"/llm-gateway-*.dump 2>/dev/null | sort | head -n -"$keep" || true)"
    [ -z "$old" ] && return 0
    echo "$old" | while read -r f; do rm -f "$f"; info "已刪除過期備份 $(basename "$f")"; done
}
prune "$DAILY_DIR" "$KEEP_DAILY"
prune "$MONTHLY_DIR" "$KEEP_MONTHLY"

# ── 異地複製（選用）──
if [ -n "${BACKUP_COPY_TO:-}" ]; then
    command -v rsync >/dev/null || die "設了 BACKUP_COPY_TO 但找不到 rsync"
    rsync -a "$final" "$BACKUP_COPY_TO" || die "複製到 $BACKUP_COPY_TO 失敗（本機備份已完成：$final）"
    ok "已複製到 $BACKUP_COPY_TO"
else
    warn "未設定 BACKUP_COPY_TO：備份只在本機，主機壞掉時會一起遺失"
fi
