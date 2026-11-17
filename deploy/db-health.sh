#!/bin/bash
# LLM Gateway 資料庫健康檢查（只讀，服務不用停）
#
# 用法:
#   bash deploy/db-health.sh
#
# 把用量全部列出來，再指出要注意的地方：
#   磁碟    資料目錄的每個子目錄、每個資料庫、每張資料表、每個索引各佔多少
#   記憶體  PostgreSQL 的記憶體設定、每個容器、gateway 服務和主機目前用了多少
#   狀態    有沒有東西卡住空間回收（閒置的 replication slot、開很久的交易）、
#           usage_logs 有多大、id 用了多少、gateway 自己的背景工作有沒有在跑
#
# 不會影響服務：
#   - 只送 SELECT，不寫入、不清理、不改任何設定。連線另外開在唯讀模式
#     （default_transaction_read_only=on）當第二道保護：新增、修改、刪除資料
#     和改結構的指令會被 PostgreSQL 拒絕。它擋不住 VACUUM 這類維護指令，
#     所以往這支腳本加東西時，只加 SELECT。
#   - 每個查詢最多跑 15 秒、等鎖最多 2 秒，超過就放棄那一項，不會排隊擋住別人。
#   - 不掃大表：筆數用 PostgreSQL 的統計估計值，大小讀檔案資訊，
#     usage_logs 最舊和最新的時間各是一次索引查找。
#   - 只開幾條短連線，一條接一條，用完即關。
#   - 不寫任何檔案，結果只印在畫面上。要留存就自己導向：
#       bash deploy/db-health.sh | tee db-health-$(date +%F).txt
#
# 可用環境變數覆蓋：
#   PG_CONTAINER    PostgreSQL 容器名稱（預設 llm-gateway-pg）
#   ENV_FILE        讀取 PG_USER / PG_DB 的檔案（預設 deploy/.env）
#
# 結束碼：0 沒有要注意的事；1 有（列在最後的「要注意」）；2 連不上資料庫。
set -uo pipefail   # 不用 -e：一項檢查失敗時要繼續做下一項

# ── 判定門檻 ──
WAL_WARN_BYTES=$((5 * 1024 ** 3))          # pg_wal 超過這個，而且超過 max_wal_size 的兩倍（正常不會超過 max_wal_size 多少）
SLOT_WARN_BYTES=$((1024 ** 3))             # 一個 slot 保留的 WAL
XACT_WARN_S=3600                           # 開著的交易
IDLE_XACT_WARN_S=600                       # idle in transaction
DEAD_WARN_ROWS=100000                      # 過期的列比有效的列多，且超過這個數
FAT_TABLE_BYTES=$((1024 ** 3))             # 「很大但沒幾筆」的表：大小超過這個
FAT_ROW_BYTES=20000                        #   而且平均每筆超過這個
DISK_WARN_PCT=85
ID_WARN_PCT=50
CONN_WARN_PCT=80
XID_AGE_WARN=1000000000
ROLLUP_LAG_WARN_DAYS=2

if [ -t 1 ]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; CYAN=''; BOLD=''; NC=''
fi
header() { echo -e "\n${BOLD}── $* ──${NC}"; }
info()   { echo -e "  $*"; }
ok()     { echo -e "  ${GREEN}✔${NC} $*"; }
WARNINGS=()
warn()   { echo -e "  ${YELLOW}⚠${NC} $*"; WARNINGS+=("$*"); }
die()    { echo -e "${RED}✘${NC} $*" >&2; exit 2; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PG_CONTAINER="${PG_CONTAINER:-llm-gateway-pg}"

command -v docker >/dev/null || die "找不到 docker 指令"
docker inspect -f '{{.State.Running}}' "$PG_CONTAINER" 2>/dev/null | grep -q true \
    || die "PostgreSQL 容器 $PG_CONTAINER 沒有在執行（docker ps 確認名稱，或設 PG_CONTAINER）"

# ── 讀 PG_USER / PG_DB（和 backup-db.sh 同一種找法）──
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

# 每條連線都是唯讀、有時間上限的
SAFE_OPTS="-c default_transaction_read_only=on -c statement_timeout=15s -c lock_timeout=2s -c idle_in_transaction_session_timeout=30s"
# psql_in DB [psql 參數...]：SQL 從 stdin 進來；錯誤不中斷（少一項，不是整份失敗）
psql_in() {
    local db="$1"; shift
    docker exec -i -e PGOPTIONS="$SAFE_OPTS" -e PGAPPNAME=db-health "$PG_CONTAINER" \
        psql -X -q -U "$PG_USER" -d "$db" -P pager=off "$@" 2>/dev/null
}
in_container() { docker exec "$PG_CONTAINER" "$@" 2>/dev/null; }

human() {  # 位元組 → 好讀的大小
    awk -v b="${1:-0}" 'BEGIN { split("B KB MB GB TB", u, " "); i = 1
        while (b >= 1024 && i < 5) { b /= 1024; i++ }
        printf (i == 1 ? "%d %s" : "%.1f %s"), b, u[i] }'
}
dur() {  # 秒 → 好讀的時間長度
    local s="${1:-0}"
    if [ "$s" -ge 172800 ]; then echo "$((s / 86400)) 天"
    elif [ "$s" -ge 7200 ]; then echo "$((s / 3600)) 小時"
    elif [ "$s" -ge 120 ]; then echo "$((s / 60)) 分鐘"
    else echo "${s} 秒"; fi
}
pct() { awk -v a="${1:-0}" -v b="${2:-0}" 'BEGIN { printf "%.1f", (b > 0 ? a * 100 / b : 0) }'; }
over() { awk -v a="${1:-0}" -v b="${2:-0}" 'BEGIN { exit !(a > b) }'; }   # a > b（可以是小數）

declare -A F
load_facts() {
    local line
    while IFS= read -r line; do
        [[ "$line" == *=* ]] && F["${line%%=*}"]="${line#*=}"
    done
}
fact() { echo "${F[$1]:-}"; }
num()  { local v="${F[$1]:-}"; [[ "$v" =~ ^-?[0-9]+$ ]] && echo "$v" || echo 0; }

# ╔══════════════════════════════════════════════════════════╗
# ║  收集（兩條連線：整個 PostgreSQL、gateway 自己的表）       ║
# ╚══════════════════════════════════════════════════════════╝
load_facts < <(psql_in "$PG_DB" -At <<'SQL'
SELECT 'version=' || current_setting('server_version');
SELECT 'data_directory=' || current_setting('data_directory');
SELECT 'uptime_s=' || extract(epoch FROM now() - pg_postmaster_start_time())::bigint;
SELECT 'max_connections=' || current_setting('max_connections');
SELECT 'connections=' || count(*) FROM pg_stat_activity WHERE backend_type = 'client backend';
SELECT 'autovacuum=' || current_setting('autovacuum');
SELECT 'max_wal_size_mb=' || setting FROM pg_settings WHERE name = 'max_wal_size';
SELECT 'archive_mode=' || current_setting('archive_mode');
SELECT 'db_size=' || pg_database_size(current_database());
SELECT 'all_db_size=' || sum(pg_database_size(datname))::bigint FROM pg_database;
SELECT 'biggest_other_db=' || datname || ':' || pg_database_size(datname)
  FROM pg_database WHERE datname <> current_database() ORDER BY pg_database_size(datname) DESC LIMIT 1;
SELECT 'slots=' || count(*) FROM pg_replication_slots;
SELECT 'slots_inactive=' || count(*) FROM pg_replication_slots WHERE NOT active;
SELECT 'slot_retained=' || coalesce(max(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)), 0)::bigint
  FROM pg_replication_slots;
SELECT 'oldest_xact_s=' || coalesce(extract(epoch FROM now() - min(xact_start))::bigint, 0)
  FROM pg_stat_activity WHERE xact_start IS NOT NULL AND backend_type = 'client backend' AND pid <> pg_backend_pid();
SELECT 'idle_in_xact_s=' || coalesce(extract(epoch FROM now() - min(state_change))::bigint, 0)
  FROM pg_stat_activity WHERE state LIKE 'idle in transaction%';
SELECT 'prepared_xacts=' || count(*) FROM pg_prepared_xacts;
SELECT 'xid_age=' || max(age(datfrozenxid)) FROM pg_database;
SELECT 'archive_failing=' || (CASE WHEN last_failed_time IS NOT NULL
         AND (last_archived_time IS NULL OR last_failed_time > last_archived_time) THEN 1 ELSE 0 END)
  FROM pg_stat_archiver;
SQL
)
[ -n "$(fact version)" ] || die "連不上資料庫 $PG_DB（使用者 $PG_USER，容器 $PG_CONTAINER）"

# gateway 的表：舊版 schema 少了哪張，就少那一項
load_facts < <(psql_in "$PG_DB" -At -v dead_rows="$DEAD_WARN_ROWS" -v fat_bytes="$FAT_TABLE_BYTES" -v fat_row="$FAT_ROW_BYTES" <<'SQL'
SELECT 'ul_rows=' || reltuples::bigint FROM pg_class WHERE oid = 'usage_logs'::regclass;
SELECT 'ul_size=' || pg_total_relation_size('usage_logs');
SELECT 'ul_heap=' || pg_relation_size('usage_logs');
SELECT 'ul_id_type=' || format_type(atttypid, atttypmod)
  FROM pg_attribute WHERE attrelid = 'usage_logs'::regclass AND attname = 'id';
SELECT 'ul_seq_last=' || coalesce(last_value, 0) FROM pg_sequences
  WHERE format('%I.%I', schemaname, sequencename) = pg_get_serial_sequence('usage_logs', 'id');
SELECT 'ul_seq_max=' || max_value FROM pg_sequences
  WHERE format('%I.%I', schemaname, sequencename) = pg_get_serial_sequence('usage_logs', 'id');
SELECT 'ul_oldest=' || coalesce(min(created_at)::date::text, '') FROM usage_logs;
SELECT 'ul_newest=' || coalesce(to_char(max(created_at), 'YYYY-MM-DD HH24:MI'), '') FROM usage_logs;
SELECT 'ul_age_days=' || coalesce((now() AT TIME ZONE 'UTC')::date - min(created_at)::date, 0) FROM usage_logs;
SELECT 'rolled_through=' || coalesce(max(rolled_through)::text, '') FROM usage_rollup_state;
SELECT 'rollup_lag_days=' || coalesce((now() AT TIME ZONE 'Asia/Taipei')::date - max(rolled_through), -1)
  FROM usage_rollup_state;
SELECT 'jobs_error=' || coalesce(string_agg(name, ', '), '') FROM scheduled_jobs WHERE last_status = 'error';
SELECT 'dead_tables=' || coalesce(string_agg(relname || ' (' || n_dead_tup || ' 過期 / ' || n_live_tup || ' 有效)', '; '), '')
  FROM pg_stat_user_tables WHERE n_dead_tup > :dead_rows AND n_dead_tup > n_live_tup;
SELECT 'fat_tables=' || coalesce(string_agg(c.relname || ' (' || pg_size_pretty(pg_total_relation_size(c.oid))
         || '，約 ' || c.reltuples::bigint || ' 筆)', '; '), '')
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind = 'r' AND n.nspname NOT IN ('pg_catalog', 'information_schema')
   AND c.reltuples >= 0 AND pg_total_relation_size(c.oid) > :fat_bytes
   AND pg_total_relation_size(c.oid) / greatest(c.reltuples, 1) > :fat_row;
SQL
)

# 磁碟：資料目錄裡各部分的大小（只讀檔案資訊，不讀內容）
DATA_DIR="$(fact data_directory)"
du_bytes() { local kb; kb="$(in_container timeout 120 du -sk "$1" | awk '{print $1}')"; [[ "$kb" =~ ^[0-9]+$ ]] && echo $((kb * 1024)) || echo 0; }
PGDATA_BYTES="$(du_bytes "$DATA_DIR/")"
WAL_BYTES="$(du_bytes "$DATA_DIR/pg_wal/")"
read -r FS_TOTAL_KB FS_USED_KB FS_AVAIL_KB < <(in_container df -Pk "$DATA_DIR" | awk 'NR==2 {print $2, $3, $4}') || true
FS_TOTAL_KB="${FS_TOTAL_KB:-0}"; FS_USED_KB="${FS_USED_KB:-0}"; FS_AVAIL_KB="${FS_AVAIL_KB:-0}"

# ╔══════════════════════════════════════════════════════════╗
# ║  報告                                                     ║
# ╚══════════════════════════════════════════════════════════╝
echo -e "${BOLD}LLM Gateway 資料庫健康檢查${NC}  $(date '+%Y-%m-%d %H:%M:%S')"
info "PostgreSQL $(fact version)，容器 $PG_CONTAINER，已執行 $(( $(num uptime_s) / 86400 )) 天"

header "1. 磁碟"
info "資料目錄 $DATA_DIR：$(human "$PGDATA_BYTES")，各子目錄："
in_container sh -c 'timeout 120 du -sk "$1"/* | sort -rn' _ "$DATA_DIR" | while read -r kb path; do
    [ "$kb" -ge 1024 ] 2>/dev/null || continue   # 1 MB 以下的不列
    case "${path##*/}" in
        base) note="所有資料庫的資料表與索引" ;;
        pg_wal) note="交易日誌（WAL）" ;;
        pg_xact|pg_multixact|pg_subtrans|pg_commit_ts) note="交易狀態紀錄" ;;
        global) note="整個 PostgreSQL 共用的系統表" ;;
        pg_tblspc) note="額外的 tablespace" ;;
        log|pg_log) note="PostgreSQL 的 log 檔" ;;
        *) note="" ;;
    esac
    printf '    %-10s %s  %s\n' "$(human $((kb * 1024)))" "${path##*/}" "$note"
done
if [ "$FS_TOTAL_KB" -gt 0 ]; then
    fs_pct="$(pct "$FS_USED_KB" "$FS_TOTAL_KB")"
    info "所在的磁碟：共 $(human $((FS_TOTAL_KB * 1024)))，已用 ${fs_pct}%，剩 $(human $((FS_AVAIL_KB * 1024)))"
    over "$fs_pct" "$DISK_WARN_PCT" && warn "磁碟已用 ${fs_pct}%（超過 ${DISK_WARN_PCT}%）"
fi
MAX_WAL_BYTES=$(( $(num max_wal_size_mb) * 1024 * 1024 ))
if [ "$WAL_BYTES" -gt "$WAL_WARN_BYTES" ] && [ "$WAL_BYTES" -gt $((MAX_WAL_BYTES * 2)) ]; then
    warn "pg_wal 有 $(human "$WAL_BYTES")，設定的上限（max_wal_size）是 $(human "$MAX_WAL_BYTES")：有東西擋住交易日誌的回收（看第 3 節）"
else
    ok "pg_wal 大小正常"
fi
other=$(( PGDATA_BYTES - $(num all_db_size) - WAL_BYTES ))
if [ "$PGDATA_BYTES" -gt 0 ] && [ "$other" -gt $((5 * 1024 ** 3)) ]; then
    warn "資料目錄裡有 $(human "$other") 既不是資料庫也不是 pg_wal（暫存檔、殘留檔案或別的東西放在裡面）"
fi

header "2. 資料庫"
psql_in "$PG_DB" <<'SQL'
SELECT datname AS "資料庫", pg_size_pretty(pg_database_size(datname)) AS "大小"
  FROM pg_database ORDER BY pg_database_size(datname) DESC;
SQL
big_other="$(fact biggest_other_db)"
if [ -n "$big_other" ] && [ "${big_other##*:}" -gt "$(num db_size)" ] 2>/dev/null; then
    warn "資料庫 ${big_other%%:*}（$(human "${big_other##*:}")）比 gateway 的 $PG_DB（$(human "$(num db_size)")）還大：空間主要是它用的，不是 gateway"
fi

header "3. 會擋住空間回收的東西"
if [ "$(num slots)" -eq 0 ]; then
    ok "沒有 replication slot"
else
    psql_in "$PG_DB" <<'SQL'
SELECT slot_name AS "slot", slot_type AS "類型", active AS "使用中",
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS "保留的 WAL"
  FROM pg_replication_slots ORDER BY restart_lsn;
SQL
    [ "$(num slots_inactive)" -gt 0 ] && warn "有 $(num slots_inactive) 個沒人在用的 replication slot：PostgreSQL 會一直替它保留交易日誌"
    [ "$(num slot_retained)" -gt "$SLOT_WARN_BYTES" ] && warn "replication slot 保留了 $(human "$(num slot_retained)") 的交易日誌"
fi
if [ "$(num oldest_xact_s)" -gt "$XACT_WARN_S" ] || [ "$(num idle_in_xact_s)" -gt "$IDLE_XACT_WARN_S" ]; then
    psql_in "$PG_DB" <<'SQL'
SELECT pid, usename AS "使用者", application_name AS "程式", state AS "狀態",
       date_trunc('second', now() - xact_start) AS "交易開了多久", client_addr AS "來源", left(query, 50) AS "最後的指令"
  FROM pg_stat_activity
 WHERE xact_start < now() - interval '10 minutes' AND backend_type = 'client backend' AND pid <> pg_backend_pid()
 ORDER BY xact_start LIMIT 10;
SQL
    longest="$(num oldest_xact_s)"; [ "$(num idle_in_xact_s)" -gt "$longest" ] && longest="$(num idle_in_xact_s)"
    warn "有交易開了 $(dur "$longest") 沒結束：這段期間所有資料表的過期資料都清不掉"
else
    ok "沒有開很久的交易"
fi
[ "$(num prepared_xacts)" -gt 0 ] && warn "有 $(num prepared_xacts) 個 prepared transaction 沒有提交：同樣會擋住回收"
[ "$(fact autovacuum)" = "on" ] || warn "autovacuum 是關的（$(fact autovacuum)）：過期資料不會自動清"
[ "$(num archive_failing)" -eq 1 ] && warn "WAL 歸檔（archive_mode=$(fact archive_mode)）最近一次是失敗的：歸檔不成功，pg_wal 就不會回收"
[ "$(num xid_age)" -gt "$XID_AGE_WARN" ] && warn "交易編號年齡 $(num xid_age)：太久沒有完整 vacuum，接近上限時 PostgreSQL 會停止寫入"

TABLES_SQL=$(cat <<'SQL'
SELECT n.nspname || '.' || c.relname AS "資料表",
       pg_size_pretty(pg_total_relation_size(c.oid)) AS "總大小",
       pg_size_pretty(pg_table_size(c.oid)) AS "資料",
       pg_size_pretty(pg_indexes_size(c.oid)) AS "索引",
       CASE WHEN c.reltuples < 0 THEN '（未統計）' ELSE c.reltuples::bigint::text END AS "約幾筆",
       coalesce(s.n_dead_tup, 0) AS "過期的列",
       coalesce(to_char(s.last_autovacuum, 'MM-DD HH24:MI'), '—') AS "上次自動清理"
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
 WHERE c.relkind IN ('r', 'm') AND n.nspname NOT IN ('pg_catalog', 'information_schema')
 ORDER BY pg_total_relation_size(c.oid) DESC LIMIT :n;
SQL
)
header "4. 每張資料表（$PG_DB）"
psql_in "$PG_DB" -v n=500 <<< "$TABLES_SQL"
info "每個索引："
psql_in "$PG_DB" <<'SQL'
SELECT relname AS "資料表", indexrelname AS "索引", pg_size_pretty(pg_relation_size(indexrelid)) AS "大小",
       idx_scan AS "被用過幾次"
  FROM pg_stat_user_indexes ORDER BY pg_relation_size(indexrelid) DESC;
SQL
# 同一個 PostgreSQL 裡的其他資料庫：超過 100 MB 的也把表列出來
while IFS= read -r other_db; do
    [ -n "$other_db" ] || continue
    info "資料庫 ${other_db} 的資料表（最大的 30 張）："
    psql_in "$other_db" -v n=30 <<< "$TABLES_SQL" || info "  （連不進 ${other_db}）"
done < <(psql_in "$PG_DB" -At <<'SQL'
SELECT datname FROM pg_database
 WHERE datallowconn AND NOT datistemplate AND datname <> current_database()
   AND pg_database_size(datname) > 100 * 1024 * 1024
 ORDER BY pg_database_size(datname) DESC LIMIT 5;
SQL
)
[ -n "$(fact dead_tables)" ] && warn "過期的列比有效的列還多：$(fact dead_tables)"
[ -n "$(fact fat_tables)" ] && warn "很大但沒幾筆資料（膨脹過）：$(fact fat_tables)。空間要 VACUUM FULL 才會還給系統"
[ -z "$(fact dead_tables)$(fact fat_tables)" ] && ok "沒有看到膨脹的資料表"

header "5. usage_logs"
if [ -n "$(fact ul_size)" ]; then
    rows="$(num ul_rows)"
    info "約 ${rows} 筆，$(human "$(num ul_size)")（資料 $(human "$(num ul_heap)")，其餘是索引）"
    [ "$rows" -gt 0 ] && info "平均每筆 $(( $(num ul_size) / rows )) bytes（含索引）"
    info "最舊 $(fact ul_oldest)，最新 $(fact ul_newest) UTC，涵蓋約 $(num ul_age_days) 天"
    # id 的上限：欄位型別和 sequence 兩者較小的那個
    seq_max="$(num ul_seq_max)"; id_max="$seq_max"
    [ "$(fact ul_id_type)" = "integer" ] && [ "$seq_max" -gt 2147483647 ] && id_max=2147483647
    if [ "$id_max" -gt 0 ]; then
        id_pct="$(pct "$(num ul_seq_last)" "$id_max")"
        info "id 是 $(fact ul_id_type)，已發到 $(num ul_seq_last)，上限 ${id_max}（用了 ${id_pct}%）"
        over "$id_pct" "$ID_WARN_PCT" && warn "usage_logs 的 id 已用 ${id_pct}%：用完後用量寫不進去（見 deploy/README.md 的 Widening usage_logs.id）"
    fi
else
    info "讀不到 usage_logs（資料庫名稱不對，或還沒跑過 migration）"
fi

header "6. Gateway 的背景工作"
if [ -n "$(fact rolled_through)" ]; then
    info "每日彙總（usage_daily）已到 $(fact rolled_through)"
    [ "$(num rollup_lag_days)" -gt "$ROLLUP_LAG_WARN_DAYS" ] \
        && warn "每日彙總落後 $(num rollup_lag_days) 天：報表會變慢，retention 也不會刪超過彙總進度的資料"
else
    info "每日彙總還沒有跑過（或這個版本沒有 usage_daily）"
fi
psql_in "$PG_DB" <<'SQL'
SELECT name AS "工作", to_char(last_started_at, 'MM-DD HH24:MI') AS "上次開始(UTC)",
       last_status AS "結果", left(last_detail, 60) AS "說明"
  FROM scheduled_jobs ORDER BY name;
SQL
[ -n "$(fact jobs_error)" ] && warn "背景工作上次執行失敗：$(fact jobs_error)"

header "7. 連線"
conn_pct="$(pct "$(num connections)" "$(num max_connections)")"
info "目前 $(num connections) 條，上限 $(num max_connections)（${conn_pct}%）"
over "$conn_pct" "$CONN_WARN_PCT" && warn "連線數已用 ${conn_pct}%"

header "8. 記憶體"
info "PostgreSQL 的設定："
psql_in "$PG_DB" <<'SQL'
SELECT name AS "設定", current_setting(name) AS "值",
       CASE name WHEN 'shared_buffers' THEN '啟動時就佔用的共用快取'
                 WHEN 'work_mem' THEN '每個排序 / 雜湊動作最多用這麼多（一個查詢可以有好幾個）'
                 WHEN 'maintenance_work_mem' THEN 'vacuum、建索引時最多用這麼多'
                 WHEN 'effective_cache_size' THEN '只是給查詢規劃參考的估計值，不佔記憶體'
                 WHEN 'max_connections' THEN '每條連線自己還要幾 MB' END AS "意思"
  FROM pg_settings
 WHERE name IN ('shared_buffers', 'work_mem', 'maintenance_work_mem', 'effective_cache_size', 'max_connections')
 ORDER BY name;
SQL
info "各容器目前的用量："
docker stats --no-stream --format '    {{.Name}}  記憶體 {{.MemUsage}}（{{.MemPerc}}）  CPU {{.CPUPerc}}' 2>/dev/null \
    || info "  （讀不到 docker stats）"
gw_mem="$(systemctl --user show llm-gateway -p MemoryCurrent --value 2>/dev/null || true)"
[[ "$gw_mem" =~ ^[0-9]+$ ]] && info "gateway 服務（systemd，所有 worker 合計）：$(human "$gw_mem")"
if read -r m_total m_used m_avail < <(free -b 2>/dev/null | awk '/^Mem:/ {print $2, $3, $7}'); then
    info "主機：共 $(human "$m_total")，已用 $(human "$m_used")，還能用 $(human "$m_avail")（含可釋放的檔案快取）"
    over "$(pct $((m_total - m_avail)) "$m_total")" 90 && warn "主機記憶體只剩 $(human "$m_avail") 可用"
fi

# ╔══════════════════════════════════════════════════════════╗
# ║  結論                                                     ║
# ╚══════════════════════════════════════════════════════════╝
header "結論"
info "PostgreSQL 共佔 $(human "$PGDATA_BYTES")：所有資料庫 $(human "$(num all_db_size)")（其中 $PG_DB $(human "$(num db_size)")），pg_wal $(human "$WAL_BYTES")"
if [ ${#WARNINGS[@]} -eq 0 ]; then
    ok "沒有要注意的事"
    exit 0
fi
echo -e "  ${YELLOW}要注意（${#WARNINGS[@]} 項）：${NC}"
for w in "${WARNINGS[@]}"; do echo "   - $w"; done
exit 1
