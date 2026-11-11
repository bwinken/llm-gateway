#!/bin/bash
# 搬遷演練共用函式（由各步驟 source，不單獨執行）
#
# 目錄的角色：
#   正式主機：REPO_ROOT 就是 ~/opt/llm-gateway，只跑 1-bundle.sh，唯讀（除了多一份備份）。
#   演練機器：REPO_ROOT 是一份乾淨的 checkout，2 ~ 7 會在裡面建立 config/、keys/、logs/、
#             .env、deploy/.env、deploy/pgdata（全部被 .gitignore 忽略），7-teardown.sh 全部刪除。
set -euo pipefail

DRILL_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DRILL_HOME/../.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/deploy"

# 演練設定（選用）：drill.env.example 複製成 drill.env。
# 指令列上給的環境變數優先於檔案（BUNDLE=... bash 4-up.sh 要真的用到那個 BUNDLE）：
# 先記下現有的環境，讀完檔案再蓋回去。
if [ -f "$DRILL_HOME/drill.env" ]; then
    _preset_env="$(export -p)"
    set -a; . "$DRILL_HOME/drill.env"; set +a
    eval "$_preset_env"
    unset _preset_env
fi

MIGRATION_DRILL_LOG="${MIGRATION_DRILL_LOG:-$DRILL_HOME/drill.log}"
STATE_DIR="$DRILL_HOME/.state"                 # 演練中間結果（還原後筆數、各步驟秒數）
MARKER="$REPO_ROOT/.migration-drill"           # 這個目錄是演練環境的記號，內容是演練建立的路徑
DRILL_PROJECT="llmgw-drill"                    # compose project 名稱；用來分辨容器是不是演練建立的
PG_CONTAINER="llm-gateway-pg"
GW_CONTAINER="llm-gateway"
GATEWAY_IMAGE="${GATEWAY_IMAGE:-llm-gateway:drill}"

# 帶到演練機器前就要拿掉的設定：會對外產生副作用，或只對正式主機有意義
#   EXCHANGE_*   有了它 client-rule 的信會真的寄給使用者
#   LANGFUSE_*   演練的請求會混進正式的觀測資料
#   USAGE_LOG_RETENTION_DAYS  保留期工作會刪掉演練資料庫的資料，筆數就對不起來
#   DATABASE_URL / 路徑        演練環境自己產生
ENV_STRIP_RE='^[[:space:]]*(export[[:space:]]+)?(EXCHANGE_[A-Z0-9_]*|LANGFUSE_[A-Z0-9_]*|USAGE_LOG_RETENTION_DAYS|DATABASE_URL|CONFIG_DIR|LOG_DIR|METRICS_DIR|AUTH_CENTER_PUBLIC_KEY_PATH)='

# 快照與比對用的資料表
SNAPSHOT_TABLES="users usage_logs usage_daily audit_log app_owners ip_allow_rules client_violations anomaly_events"

if [ -t 1 ]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; CYAN=''; BOLD=''; NC=''
fi
ts()   { date '+%Y-%m-%d %H:%M:%S'; }
info() { echo -e "$(ts) ${CYAN}▸${NC} $*"; }
ok()   { echo -e "$(ts) ${GREEN}✔${NC} $*"; }
warn() { echo -e "$(ts) ${YELLOW}⚠${NC} $*"; }
die()  { echo -e "$(ts) ${RED}✘${NC} $*" >&2; STEP_ERROR="$*"; exit 1; }

# ── 步驟紀錄：每個步驟結束時（成功或失敗）在 drill.log 留一行 ──
STEP_NAME=""; STEP_START=0; STEP_DETAIL=""; STEP_ERROR=""
CLEANUPS=()
note() { STEP_DETAIL="${STEP_DETAIL:+$STEP_DETAIL }$*"; }      # note key=value

_step_exit() {
    local code=$?
    local c
    for c in "${CLEANUPS[@]:-}"; do [ -n "$c" ] && eval "$c" || true; done
    [ -n "$STEP_NAME" ] || exit "$code"
    local result=OK
    [ "$code" -eq 0 ] || result=FAIL
    [ "$code" -eq 0 ] || [ -z "$STEP_ERROR" ] || STEP_DETAIL="${STEP_DETAIL:+$STEP_DETAIL }error=${STEP_ERROR//$'\t'/ }"
    mkdir -p "$(dirname "$MIGRATION_DRILL_LOG")"
    printf '%s\tstep=%s\tresult=%s\telapsed=%ss\tby=%s@%s\t%s\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$STEP_NAME" "$result" "$(( $(date +%s) - STEP_START ))" \
        "$(id -un)" "$(hostname)" "$STEP_DETAIL" >> "$MIGRATION_DRILL_LOG" || true
    exit "$code"
}

step_begin() {
    STEP_NAME="$1"; STEP_START=$(date +%s)
    trap _step_exit EXIT
    echo -e "${BOLD}== $1 ==${NC}"
}

need() { local c; for c in "$@"; do command -v "$c" >/dev/null || die "找不到 $c 指令"; done; }

# KEY=value 檔案（snapshot.env、deploy/.env）讀一個值；沒有這個 key 時是空字串，不是錯誤
kv() { { grep -E "^$2=" "$1" 2>/dev/null || true; } | tail -n1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//'; }

# ── 這台是不是正式主機？演練機器上的步驟一律先問 ──
guard_not_production() {
    if command -v systemctl >/dev/null && systemctl --user is-active --quiet llm-gateway 2>/dev/null; then
        die "這台主機有執行中的 llm-gateway systemd 服務：看起來是正式主機，演練請在另一台機器做"
    fi
    local c project
    for c in "$PG_CONTAINER" "$GW_CONTAINER"; do
        if docker inspect "$c" >/dev/null 2>&1; then
            project="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$c" 2>/dev/null || true)"
            [ "$project" = "$DRILL_PROJECT" ] \
                || die "容器 $c 已存在，而且不是演練建立的（compose project: ${project:-無}）：看起來是正式主機，拒絕執行"
        fi
    done
    if [ ! -f "$MARKER" ]; then
        local f
        for f in "$REPO_ROOT/.env" "$DEPLOY_DIR/.env" "$REPO_ROOT/config.toml" "$REPO_ROOT/config/config.toml" "$DEPLOY_DIR/pgdata"; do
            [ ! -e "$f" ] || die "$f 已存在，而這個目錄沒有演練記號：看起來是正式的安裝目錄。演練請用一份乾淨的 checkout"
        done
    fi
}

# ── 演練包 ──
BUNDLE="${BUNDLE:-}"
require_bundle() {
    [ -n "${1:-}" ] && BUNDLE="$1"
    [ -n "$BUNDLE" ] || die "請指定演練包目錄：參數，或 drill.env 的 BUNDLE（由正式主機的 1-bundle.sh 產生）"
    BUNDLE="$(cd "$BUNDLE" 2>/dev/null && pwd)" || die "找不到演練包目錄"
    [ -f "$BUNDLE/snapshot.env" ] || die "$BUNDLE 裡沒有 snapshot.env：不是 1-bundle.sh 產生的目錄？"
    [ -f "$BUNDLE/SHA256SUMS" ] || die "$BUNDLE 裡沒有 SHA256SUMS"
    ( cd "$BUNDLE" && sha256sum --quiet -c SHA256SUMS ) || die "演練包的檔案和 SHA256SUMS 不符（複製過程損毀或被改過）"
    BUNDLE_DUMP="$(ls -1 "$BUNDLE"/llm-gateway-*.dump "$BUNDLE"/llm-gateway-*.dump.gpg 2>/dev/null | head -n1 || true)"
    [ -n "$BUNDLE_DUMP" ] || die "演練包裡沒有資料庫備份"
    BUNDLE_STATE="$(ls -1 "$BUNDLE"/state.tar.gz "$BUNDLE"/state.tar.gz.gpg 2>/dev/null | head -n1 || true)"
    [ -n "$BUNDLE_STATE" ] || die "演練包裡沒有 state.tar.gz"
}

# ── 解密：DRILL_GPG_KEY（私鑰檔）匯入暫時的 GnuPG 目錄，結束時刪除 ──
GNUPG_TMP=""
gpg_prepare() {
    local need_gpg=0 f
    for f in "$@"; do [[ "$f" == *.gpg ]] && need_gpg=1; done
    [ "$need_gpg" -eq 1 ] || return 0
    need gpg
    if [ -n "${DRILL_GPG_KEY:-}" ] && [ -z "$GNUPG_TMP" ]; then
        [ -r "$DRILL_GPG_KEY" ] || die "讀不到 DRILL_GPG_KEY: $DRILL_GPG_KEY"
        GNUPG_TMP="$(mktemp -d)"
        CLEANUPS+=('gpgconf --homedir "$GNUPG_TMP" --kill gpg-agent >/dev/null 2>&1; rm -rf "$GNUPG_TMP"')
        gpg --homedir "$GNUPG_TMP" --batch --quiet --import "$DRILL_GPG_KEY" 2>/dev/null \
            || die "無法匯入 DRILL_GPG_KEY（不是私鑰檔？）"
        export GNUPGHOME="$GNUPG_TMP"
    fi
    if [ -t 0 ]; then GPG_TTY="$(tty)"; export GPG_TTY; fi
}
# 內容輸出到 stdout；.gpg 先解密
plain_stream() {
    if [[ "$1" == *.gpg ]]; then gpg --quiet --decrypt "$1"; else cat "$1"; fi
}

# ── 演練環境的 compose / psql ──
compose() { bash "$DRILL_HOME/compose.sh" -p "$DRILL_PROJECT" "$@"; }
drill_psql() {
    docker exec "$PG_CONTAINER" psql -U "$(kv "$DEPLOY_DIR/.env" PG_USER)" -d "$(kv "$DEPLOY_DIR/.env" PG_DB)" \
        -At -v ON_ERROR_STOP=1 -c "$1"
}
# 把各資料表筆數寫成 count_<table>=N（psql 函式名稱當第一個參數）
table_counts() {
    local q="$1" t
    for t in $SNAPSHOT_TABLES; do
        if [ "$($q "SELECT to_regclass('public.$t') IS NOT NULL")" = "t" ]; then
            echo "count_$t=$($q "SELECT count(*) FROM $t")"
        fi
    done
}
port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
