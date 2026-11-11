#!/bin/bash
# 步驟 4（演練機器）：用演練包把容器化的環境建起來
#
# 用法:
#   bash deploy/migration-drill/4-up.sh [演練包目錄]
#
# 依序做，並記下每一段花的時間（就是正式切換時的停機時間估計）：
#   1. 解開設定：config/config.toml、keys/public.pem、.env（DATABASE_URL 指向演練資料庫）
#   2. 啟動 PostgreSQL（和正式環境同一份 deploy/docker-compose.yml，project 名稱 llmgw-drill）
#   3. 把備份還原進去，記下還原後的筆數
#   4. 跑 migration（compose 的 migrate 服務：alembic upgrade head）
#   5. 啟動 gateway 容器，等到健康檢查通過
#
# 只在演練機器上跑：偵測到正式環境的跡象（systemd 服務、不是演練建立的容器、
# 既有的 .env / config.toml）就拒絕。要重來先跑 6-teardown.sh。
#
# 可用環境變數（也可寫在 drill.env）：
#   DRILL_GPG_KEY     解密用的私鑰檔
#   GATEWAY_IMAGE     gateway 映像（預設 llm-gateway:drill，和 3-image.sh 相同）
#   PG_PORT / GATEWAY_PORT   預設 5432 / 8050；這台機器上已被占用時才改
#   GATEWAY_WORKERS   預設 2
. "$(dirname "$0")/lib.sh"
step_begin 4-up
need docker sha256sum tar curl
guard_not_production
[ ! -f "$MARKER" ] || die "演練環境已經存在（$MARKER）：先跑 6-teardown.sh 再重來"
require_bundle "${1:-}"
docker image inspect "$GATEWAY_IMAGE" >/dev/null 2>&1 || die "找不到映像 $GATEWAY_IMAGE：先跑 3-image.sh"
gpg_prepare "$BUNDLE_DUMP" "$BUNDLE_STATE"

PG_PORT="${PG_PORT:-5432}"; GATEWAY_PORT="${GATEWAY_PORT:-8050}"
port_in_use "$PG_PORT" && die "127.0.0.1:$PG_PORT 已經有服務在聽（設 PG_PORT 換一個）"
port_in_use "$GATEWAY_PORT" && die "127.0.0.1:$GATEWAY_PORT 已經有服務在聽（設 GATEWAY_PORT 換一個）"

snap="$BUNDLE/snapshot.env"
PG_DB_NAME="$(kv "$snap" pg_db)"; PG_DB_NAME="${PG_DB_NAME:-llm_gateway}"
PG_USER_NAME="llm_gateway"
# 容器不以 root 執行：平常就是執行演練的使用者；以 root 執行演練時改用 1000，並把目錄交給它
RUN_UID="$(id -u)"; RUN_GID="$(id -g)"
if [ "$RUN_UID" -eq 0 ]; then RUN_UID=1000; RUN_GID=1000; fi
PG_PASSWORD_DRILL="$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')"

# ── 1. 設定 ──
# 記號檔先寫：之後任何一步失敗，6-teardown.sh 都知道要清什麼
umask 077
printf '%s\n' "config" "keys" "logs" ".env" "deploy/.env" "deploy/pgdata" > "$MARKER"
mkdir -p "$STATE_DIR" "$REPO_ROOT/config" "$REPO_ROOT/keys" "$REPO_ROOT/logs"
cp "$snap" "$STATE_DIR/snapshot.env"

info "解開設定"
work="$(mktemp -d)"
CLEANUPS+=('rm -rf "$work"')
plain_stream "$BUNDLE_STATE" | tar -xzf - -C "$work" || die "無法解開 $(basename "$BUNDLE_STATE")（沒有私鑰？設 DRILL_GPG_KEY）"
[ -f "$work/config.toml" ] || die "演練包的設定裡沒有 config.toml"
cp "$work/config.toml" "$REPO_ROOT/config/config.toml"
[ -f "$work/public.pem" ] && cp "$work/public.pem" "$REPO_ROOT/keys/public.pem"
chmod 644 "$REPO_ROOT/keys/public.pem" 2>/dev/null || true
chmod 755 "$REPO_ROOT/keys"
{
    echo "# 由 migration-drill/4-up.sh 產生：正式主機的 .env，拿掉會對外產生副作用的設定"
    grep -Ev "$ENV_STRIP_RE" "$work/env" 2>/dev/null || true      # 手工做的演練包也再濾一次
    echo "DATABASE_URL=postgresql://$PG_USER_NAME:$PG_PASSWORD_DRILL@localhost:$PG_PORT/$PG_DB_NAME"
} > "$REPO_ROOT/.env"
cat > "$DEPLOY_DIR/.env" <<ENVEOF
# 由 migration-drill/4-up.sh 產生（演練用，6-teardown.sh 會刪除）
PG_USER=$PG_USER_NAME
PG_PASSWORD=$PG_PASSWORD_DRILL
PG_DB=$PG_DB_NAME
PG_PORT=$PG_PORT
PGDATA_DIR=./pgdata
OIDC_ISSUER_URL=http://drill.invalid
OAUTH2_CLIENT_ID=drill
OAUTH2_CLIENT_SECRET=drill
OAUTH2_REDIRECT_URL=http://drill.invalid/oauth2/callback
OAUTH2_COOKIE_SECRET=0000000000000000
GATEWAY_IMAGE=$GATEWAY_IMAGE
GATEWAY_UID=$RUN_UID
GATEWAY_GID=$RUN_GID
GATEWAY_PORT=$GATEWAY_PORT
GATEWAY_WORKERS=${GATEWAY_WORKERS:-2}
ENVEOF
rm -rf "$work"
if [ "$(id -u)" -eq 0 ]; then
    chown -R "$RUN_UID:$RUN_GID" "$REPO_ROOT/config" "$REPO_ROOT/logs"
fi

# ── 2. PostgreSQL ──
info "啟動 PostgreSQL"
compose up -d postgres >/dev/null 2>&1 || die "無法啟動 PostgreSQL（docker compose up postgres）"
ready=0
for _ in $(seq 1 60); do
    # 初始化期間的暫時伺服器只聽 unix socket；TCP 通了才是正式啟動完成
    if docker exec "$PG_CONTAINER" pg_isready -h 127.0.0.1 -U "$PG_USER_NAME" -q 2>/dev/null; then ready=1; break; fi
    sleep 1
done
[ "$ready" -eq 1 ] || die "PostgreSQL 60 秒內沒有就緒"
drill_ver="$(drill_psql 'SHOW server_version')"
prod_ver="$(kv "$snap" pg_server_version)"
if [ "${drill_ver%%.*}" != "${prod_ver%%.*}" ]; then
    die "PostgreSQL 主版本不同：正式 $prod_ver，演練 $drill_ver。演練要用和正式相同的主版本"
fi

# ── 3. 還原 ──
info "還原備份 $(basename "$BUNDLE_DUMP")"
t=$(date +%s)
err="$(mktemp)"; CLEANUPS+=('rm -f "$err"')
set +e
plain_stream "$BUNDLE_DUMP" | docker exec -i "$PG_CONTAINER" \
    pg_restore -U "$PG_USER_NAME" -d "$PG_DB_NAME" --no-owner --no-acl --exit-on-error 2> "$err"
st=("${PIPESTATUS[@]}")
set -e
[ "${st[1]}" -eq 0 ] || die "還原失敗: $(tail -n 3 "$err" | tr '\n' ' ')"
[ "${st[0]}" -eq 0 ] || die "讀取或解密備份失敗"
restore_s=$(( $(date +%s) - t ))
schema_before="$(drill_psql "SELECT string_agg(version_num, ',') FROM alembic_version")"
{
    echo "schema_version=$schema_before"
    table_counts drill_psql
    echo "restore_seconds=$restore_s"
} > "$STATE_DIR/restored.env"
ok "還原完成（${restore_s}s，schema $schema_before，users=$(kv "$STATE_DIR/restored.env" count_users)）"

# ── 4. Migration ──
info "執行 migration（alembic upgrade head）"
t=$(date +%s)
compose --profile gateway up --no-deps --exit-code-from migrate migrate > "$STATE_DIR/migrate.log" 2>&1 \
    || { tail -n 15 "$STATE_DIR/migrate.log" >&2; die "migration 失敗（完整輸出：$STATE_DIR/migrate.log）"; }
migrate_s=$(( $(date +%s) - t ))
schema_after="$(drill_psql "SELECT string_agg(version_num, ',') FROM alembic_version")"
if [ "$schema_before" = "$schema_after" ]; then
    ok "migration 完成（${migrate_s}s）：schema 已是最新 $schema_after，沒有需要套用的變更"
else
    ok "migration 完成（${migrate_s}s）：schema $schema_before → $schema_after"
fi

# ── 5. Gateway ──
info "啟動 gateway 容器"
t=$(date +%s)
compose --profile gateway up -d gateway >/dev/null 2>&1 || die "無法啟動 gateway（docker compose up gateway）"
health=""
for _ in $(seq 1 90); do
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$GW_CONTAINER" 2>/dev/null || echo missing)"
    [ "$health" = healthy ] && break
    state="$(docker inspect -f '{{.State.Status}}' "$GW_CONTAINER" 2>/dev/null || echo missing)"
    if [ "$state" = exited ] || [ "$state" = missing ]; then
        docker logs --tail 20 "$GW_CONTAINER" >&2 2>/dev/null || true
        die "gateway 容器啟動後就結束了（狀態 $state）"
    fi
    # 健康檢查每 30 秒才跑一次；/readyz 通了就不用等它
    if curl -fsS -m 3 -o /dev/null "http://127.0.0.1:$GATEWAY_PORT/readyz" 2>/dev/null; then health=ready; break; fi
    sleep 1
done
[ "$health" = healthy ] || [ "$health" = ready ] || { docker logs --tail 20 "$GW_CONTAINER" >&2 || true; die "gateway 90 秒內沒有就緒"; }
start_s=$(( $(date +%s) - t ))
{
    echo "schema_after=$schema_after"
    echo "migrate_seconds=$migrate_s"
    echo "start_seconds=$start_s"
} >> "$STATE_DIR/restored.env"

note "bundle=$(basename "$BUNDLE") restore=${restore_s}s migrate=${migrate_s}s start=${start_s}s schema=$schema_before->$schema_after pg=$drill_ver image=$GATEWAY_IMAGE"
ok "gateway 已就緒（${start_s}s）：http://127.0.0.1:$GATEWAY_PORT"
ok "下一步：bash deploy/migration-drill/5-verify.sh"
