#!/bin/bash
# 步驟 6（演練機器）：把演練環境刪乾淨
#
# 用法:
#   bash deploy/migration-drill/6-teardown.sh             # 刪除演練環境
#   bash deploy/migration-drill/6-teardown.sh --image     # 連 gateway 映像一起刪
#
# 演練環境裡是一份完整的正式資料（所有 API key），演練完一定要刪：
#   - 容器、網路、volume（compose project llmgw-drill）
#   - deploy/pgdata（還原出來的資料庫）
#   - config/、keys/、logs/、.env、deploy/.env（4-up.sh 建立的，列在 .migration-drill）
# 留下來的只有紀錄：drill.log、restore-drill.log（不含任何資料或密鑰）。
# 演練包本身不在這個目錄裡，請另外刪除。
. "$(dirname "$0")/lib.sh"
step_begin 6-teardown
need docker
guard_not_production
[ -f "$MARKER" ] || die "這個目錄沒有演練記號（.migration-drill）：沒有演練環境可以刪，也不會動任何東西"

pg_image="$(docker inspect -f '{{.Config.Image}}' "$PG_CONTAINER" 2>/dev/null || true)"
info "停止並刪除容器、網路、volume"
if [ -f "$DEPLOY_DIR/.env" ]; then
    compose --profile gateway down -v --remove-orphans >/dev/null 2>&1 || warn "docker compose down 有錯誤，繼續清理"
fi
for c in $(docker ps -aq --filter "label=com.docker.compose.project=$DRILL_PROJECT"); do docker rm -f -v "$c" >/dev/null; done

# pgdata 屬於容器裡的 postgres 使用者，一般使用者刪不掉：用容器刪
if [ -d "$DEPLOY_DIR/pgdata" ]; then
    info "刪除還原出來的資料庫（deploy/pgdata）"
    rm -rf "$DEPLOY_DIR/pgdata" 2>/dev/null \
        || docker run --rm --network none -v "$DEPLOY_DIR":/d --entrypoint rm "${pg_image:-postgres:16}" -rf /d/pgdata \
        || die "無法刪除 $DEPLOY_DIR/pgdata（用 sudo rm -rf 刪除它，再跑一次）"
fi

info "刪除演練建立的設定與 log"
while IFS= read -r rel; do
    case "$rel" in ""|/*|*..*) continue ;; esac          # 只刪這個目錄底下、記號檔列出的路徑
    rm -rf "${REPO_ROOT:?}/$rel"
done < "$MARKER"
rm -rf "$STATE_DIR"
if [ "${1:-}" = "--image" ]; then
    docker image rm "$GATEWAY_IMAGE" >/dev/null 2>&1 && info "已刪除映像 $GATEWAY_IMAGE" || true
fi

# ── 確認真的沒有東西留下 ──
left=""
[ -z "$(docker ps -aq --filter "label=com.docker.compose.project=$DRILL_PROJECT")" ] || left="$left 容器"
[ -z "$(docker volume ls -q --filter "label=com.docker.compose.project=$DRILL_PROJECT")" ] || left="$left volume"
while IFS= read -r rel; do
    case "$rel" in ""|/*|*..*) continue ;; esac
    [ ! -e "$REPO_ROOT/$rel" ] || left="$left $rel"
done < "$MARKER"
[ -z "$left" ] || die "還有東西沒刪掉：$left"
rm -f "$MARKER"

note "removed=containers,volumes,pgdata,config,keys,logs,env image_removed=$([ "${1:-}" = "--image" ] && echo yes || echo no)"
ok "演練環境已清除。紀錄留在 $MIGRATION_DRILL_LOG"
warn "演練包（1-bundle.sh 產生的目錄）和私鑰檔如果還在這台機器上，請一併刪除"
