#!/bin/bash
# 步驟 5（演練機器）：容器都開好之後，才把資料搬進來
#
# 用法:
#   bash deploy/migration-drill/5-load-data.sh [演練包目錄]
#
# 這一步就是正式切換時的停機時間：呼叫 deploy/load-backup.sh，和正式搬遷用的是同一支腳本。
#   1. 停掉 gateway 容器
#   2. 刪除並重建資料庫（步驟 4 留下的空 schema）
#   3. 還原演練包裡的備份
#   4. migration（alembic upgrade head）
#   5. 啟動 gateway，等到就緒
# 每一段的秒數都會記下來，6-verify.sh 用它估計停機時間。
# 可以重複執行（每次都從備份重新載入）。
#
# 加密的演練包需要私鑰：DRILL_GPG_KEY（結束時不會留在這台機器上）。
. "$(dirname "$0")/lib.sh"
step_begin 5-load-data
need docker curl sha256sum
guard_not_production
[ -f "$MARKER" ] || die "這裡沒有演練環境：先跑 4-up.sh"
require_bundle "${1:-}"
[ "$(docker inspect -f '{{.State.Running}}' "$PG_CONTAINER" 2>/dev/null)" = true ] || die "PostgreSQL 容器沒有在執行：先跑 4-up.sh"

mkdir -p "$STATE_DIR"
rm -f "$STATE_DIR/load.env"
COMPOSE_PROJECT_NAME="$DRILL_PROJECT" BACKUP_GPG_KEY="${DRILL_GPG_KEY:-}" BACKUP_DIR="$STATE_DIR/load" \
    LOAD_SUMMARY_FILE="$STATE_DIR/load.env" \
    bash "$DEPLOY_DIR/load-backup.sh" "$BUNDLE_DUMP" --yes \
    || die "載入資料失敗（詳見上面的訊息）"
[ -f "$STATE_DIR/load.env" ] || die "load-backup.sh 沒有留下結果"
cp "$STATE_DIR/load.env" "$STATE_DIR/restored.env"

r="$STATE_DIR/restored.env"
note "bundle=$(basename "$BUNDLE") restore=$(kv "$r" restore_seconds)s migrate=$(kv "$r" migrate_seconds)s start=$(kv "$r" start_seconds)s total=$(kv "$r" load_seconds)s schema=$(kv "$r" schema_version)->$(kv "$r" schema_after) users=$(kv "$r" count_users)"
ok "資料已載入，gateway 已就緒。下一步：bash deploy/migration-drill/6-verify.sh"
