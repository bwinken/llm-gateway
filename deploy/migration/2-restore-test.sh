#!/bin/bash
# 步驟 2（演練機器）：確認演練包裡的備份真的還原得回來
#
# 用法:
#   bash deploy/migration/2-restore-test.sh [演練包目錄]
#
# 先核對 SHA256SUMS（搬運途中有沒有損毀），再呼叫 deploy/restore-drill.sh：
# 還原到一次性的容器、檢查內容、刪除。這一步沒過就不要往下做。
# 加密的演練包需要私鑰：DRILL_GPG_KEY=/path/private.asc（結束時不會留在這台機器上）。
. "$(dirname "$0")/lib.sh"
step_begin 2-restore-test
need docker sha256sum
require_bundle "${1:-}"
ok "演練包檔案完整（SHA256SUMS 相符）"

# 正式的 PG 容器不在這台機器上：用快照記下的同一個映像
image="${DRILL_IMAGE:-$(kv "$BUNDLE/snapshot.env" pg_image)}"
[ -n "$image" ] || die "snapshot.env 沒有 pg_image，請用 DRILL_IMAGE 指定（例如 postgres:16）"
docker image inspect "$image" >/dev/null 2>&1 || docker pull "$image" >/dev/null \
    || die "這台機器沒有映像 $image，也拉不下來（先 docker load，或用 DRILL_IMAGE 指定可用的映像）"

DRILL_IMAGE="$image" DRILL_LOG="$DRILL_HOME/restore-drill.log" \
    bash "$DEPLOY_DIR/restore-drill.sh" "$BUNDLE_DUMP" || die "還原演練失敗（詳見上面的訊息與 $DRILL_HOME/restore-drill.log）"

note "bundle=$(basename "$BUNDLE") image=$image $(tail -n1 "$DRILL_HOME/restore-drill.log" | cut -f6 | grep -oE '(restore|schema|users|usage_logs)=[^ ]+' | tr '\n' ' ')"
ok "備份可以還原。下一步：bash deploy/migration/3-image.sh"
