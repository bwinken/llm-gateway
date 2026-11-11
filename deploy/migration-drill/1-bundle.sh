#!/bin/bash
# 步驟 1（在「正式主機」執行）：備份資料庫，連同設定打包成演練包
#
# 用法:
#   BACKUP_GPG_RECIPIENT_FILE=~/opt/llm-gateway/keys/backup.pub.asc bash deploy/migration-drill/1-bundle.sh
#   bash deploy/migration-drill/1-bundle.sh /path/to/output-dir
#
# 對正式環境只做讀取：一次線上備份（deploy/backup-db.sh，不停機），外加讀設定檔。
# 產出 <BACKUP_DIR>/migration-bundle-<時間>/：
#   llm-gateway-<時間>.dump.gpg   資料庫備份（同時也是一份正常的每日備份）
#   state.tar.gz.gpg              config.toml、keys/public.pem、清理過的 .env
#   snapshot.env                  備份當下的快照：筆數、schema 版本、PG 版本、設定檔雜湊（沒有密鑰）
#   SHA256SUMS                    演練機器用來確認檔案沒有在搬運途中損毀
# 把整個目錄帶到演練機器，接著跑 2 ~ 7。
#
# .env 會先清理再打包：寄信（EXCHANGE_*）、Langfuse、DATABASE_URL、保留期設定不會離開正式主機，
# 演練環境因此不會寄信給使用者、不會把資料送進正式的 Langfuse。
#
# 演練包含有所有 API key 與下游密鑰，所以必須加密（和備份用同一把公鑰）：
#   BACKUP_GPG_RECIPIENT_FILE / BACKUP_GPG_RECIPIENT   見 deploy/README 的「加密」
#   DRILL_ALLOW_PLAINTEXT=1                            明知風險仍要不加密時才設
. "$(dirname "$0")/lib.sh"
step_begin 1-bundle
need docker sha256sum tar

APP_DIR="$REPO_ROOT"
[ -f "$APP_DIR/.env" ] || die "找不到 $APP_DIR/.env：這支要在正式主機的安裝目錄（~/opt/llm-gateway）裡執行"
[ ! -f "$MARKER" ] || die "這個目錄是演練環境（有 .migration-drill）：1-bundle.sh 要在正式主機執行"
docker inspect -f '{{.State.Running}}' "$PG_CONTAINER" 2>/dev/null | grep -q true \
    || die "PostgreSQL 容器 $PG_CONTAINER 沒有在執行"

CONFIG_FILE=""
for f in "$APP_DIR/config/config.toml" "$APP_DIR/config.toml"; do
    [ -f "$f" ] && { CONFIG_FILE="$f"; break; }
done
[ -n "$CONFIG_FILE" ] || die "找不到 config.toml（$APP_DIR/config/ 或 $APP_DIR/）"
KEY_FILE="$APP_DIR/keys/public.pem"
[ -f "$KEY_FILE" ] || warn "找不到 $KEY_FILE：演練環境的 Web UI 無法驗證登入（API 不受影響）"

# ── 加密設定 ──
GPG_ARGS=()
if [ -n "${BACKUP_GPG_RECIPIENT_FILE:-}" ]; then
    [ -r "$BACKUP_GPG_RECIPIENT_FILE" ] || die "讀不到 BACKUP_GPG_RECIPIENT_FILE: $BACKUP_GPG_RECIPIENT_FILE"
    GPG_ARGS=(--recipient-file "$BACKUP_GPG_RECIPIENT_FILE")
elif [ -n "${BACKUP_GPG_RECIPIENT:-}" ]; then
    GPG_ARGS=(--recipient "$BACKUP_GPG_RECIPIENT" --trust-model always)
fi
if [ ${#GPG_ARGS[@]} -gt 0 ]; then
    need gpg
elif [ "${DRILL_ALLOW_PLAINTEXT:-}" != "1" ]; then
    die "演練包含有所有 API key 與下游密鑰，必須加密：設 BACKUP_GPG_RECIPIENT_FILE（見 deploy/README 的「加密」），或明知風險時設 DRILL_ALLOW_PLAINTEXT=1"
else
    warn "DRILL_ALLOW_PLAINTEXT=1：演練包不加密，請自行確保搬運與存放安全"
fi

ENV_FILE="${ENV_FILE:-$DEPLOY_DIR/.env}"
[ -f "$ENV_FILE" ] || die "找不到 $ENV_FILE"
PG_USER="$(kv "$ENV_FILE" PG_USER)"; PG_DB="$(kv "$ENV_FILE" PG_DB)"
[ -n "$PG_USER" ] && [ -n "$PG_DB" ] || die "$ENV_FILE 裡缺少 PG_USER 或 PG_DB"
prod_psql() { docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -At -v ON_ERROR_STOP=1 -c "$1"; }

BACKUP_DIR="${BACKUP_DIR:-$HOME/backup/llm-gateway}"
stamp="$(date '+%Y-%m-%d_%H%M%S')"
OUT="${1:-$BACKUP_DIR/migration-bundle-$stamp}"
umask 077
mkdir -p "$OUT"
[ -z "$(ls -A "$OUT")" ] || die "$OUT 不是空的"
OUT="$(cd "$OUT" && pwd)"

# ── 快照：緊接在備份之前，所以備份裡的筆數只會多不會少 ──
info "記錄正式環境的快照"
{
    echo "taken_at=$(date '+%Y-%m-%dT%H:%M:%S%z')"
    echo "host=$(hostname)"
    echo "pg_image=$(docker inspect -f '{{.Config.Image}}' "$PG_CONTAINER")"
    echo "pg_server_version=$(prod_psql 'SHOW server_version')"
    echo "pg_db=$PG_DB"
    echo "schema_version=$(prod_psql "SELECT string_agg(version_num, ',') FROM alembic_version")"
    echo "db_size_bytes=$(prod_psql 'SELECT pg_database_size(current_database())')"
    table_counts prod_psql
    echo "latest_usage=$(prod_psql "SELECT coalesce(to_char(max(created_at), 'YYYY-MM-DD\"T\"HH24:MI:SS'), '-') FROM usage_logs")"
    echo "config_sha256=$(sha256sum "$CONFIG_FILE" | cut -d' ' -f1)"
} > "$OUT/snapshot.env"

# ── 資料庫：用正常的備份腳本（驗證、加密都在裡面）──
info "備份資料庫（deploy/backup-db.sh）"
before="$(ls -1 "$BACKUP_DIR"/daily/ 2>/dev/null || true)"
backup_start=$(date +%s)
bash "$DEPLOY_DIR/backup-db.sh" "$BACKUP_DIR" || die "backup-db.sh 失敗"
backup_s=$(( $(date +%s) - backup_start ))
dump=""
for f in $(ls -1 "$BACKUP_DIR"/daily/ | sort); do
    case "$f" in llm-gateway-*.dump|llm-gateway-*.dump.gpg)
        grep -qxF "$f" <<< "$before" || dump="$BACKUP_DIR/daily/$f" ;;
    esac
done
[ -n "$dump" ] || die "備份完成了，但在 $BACKUP_DIR/daily 找不到新的備份檔"
if [ ${#GPG_ARGS[@]} -gt 0 ] && [[ "$dump" != *.gpg ]]; then die "備份沒有加密：$dump"; fi
cp -p "$dump" "$OUT/"
echo "backup_seconds=$backup_s" >> "$OUT/snapshot.env"

# ── 設定：config.toml、公鑰、清理過的 .env ──
info "打包設定（.env 先清理）"
work="$(mktemp -d "$OUT/.state.XXXXXX")"
CLEANUPS+=('rm -rf "$work"')
cp "$CONFIG_FILE" "$work/config.toml"
[ -f "$KEY_FILE" ] && cp "$KEY_FILE" "$work/public.pem"
grep -Ev "$ENV_STRIP_RE" "$APP_DIR/.env" > "$work/env" || true
removed="$(grep -E "$ENV_STRIP_RE" "$APP_DIR/.env" | sed -E 's/^[[:space:]]*(export[[:space:]]+)?//; s/=.*//' | sort -u | tr '\n' ',' | sed 's/,$//')"
echo "env_removed=${removed:--}" >> "$OUT/snapshot.env"
if [ ${#GPG_ARGS[@]} -gt 0 ]; then
    tar -C "$work" -czf - . | gpg --batch --quiet --yes "${GPG_ARGS[@]}" --output "$OUT/state.tar.gz.gpg" --encrypt \
        || die "設定打包或加密失敗"
else
    tar -C "$work" -czf "$OUT/state.tar.gz" .
fi
rm -rf "$work"

( cd "$OUT" && sha256sum -- * > SHA256SUMS )

note "bundle=$OUT backup=${backup_s}s schema=$(kv "$OUT/snapshot.env" schema_version) users=$(kv "$OUT/snapshot.env" count_users) encrypted=$([ ${#GPG_ARGS[@]} -gt 0 ] && echo yes || echo no)"
ok "演練包完成：$OUT（$(du -sh "$OUT" | cut -f1)）"
info "沒有帶走的設定：${removed:-（無）}"
info "下一步：把整個目錄複製到演練機器（例如 rsync -a $OUT user@drill-host:~/），在那裡的 checkout 執行："
info "  BUNDLE=~/$(basename "$OUT") bash deploy/migration-drill/2-restore-test.sh"
