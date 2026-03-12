#!/bin/bash
# LLM Gateway 部署腳本（Docker Compose 基礎服務 + User-Level systemd + Nginx）
# 用法: bash deploy/setup.sh
# 部署到 ~/opt/llm-gateway，以當前使用者身份執行
set -e

APP_DIR="$HOME/opt/llm-gateway"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_DIR="$APP_DIR/deploy"

# Proxy 設定（只需設定 http_proxy 即可，不需要則留空）
PROXY_URL="${http_proxy:-}"
if [ -n "$PROXY_URL" ]; then
    export http_proxy="$PROXY_URL"
    export HTTP_PROXY="$PROXY_URL"
    export https_proxy="$PROXY_URL"
    export HTTPS_PROXY="$PROXY_URL"
    export no_proxy="localhost,127.0.0.1,*.company.local"
    export NO_PROXY="$no_proxy"
    echo "使用 Proxy: $PROXY_URL"
fi

# === 前置檢查 ===
if ! command -v uv &>/dev/null; then
    echo "錯誤：uv 未安裝（https://docs.astral.sh/uv/getting-started/installation/）" >&2
    exit 1
fi
if ! command -v docker &>/dev/null; then
    echo "錯誤：docker 未安裝" >&2
    exit 1
fi

echo "=== 1. 部署程式碼 ==="
mkdir -p "$APP_DIR"
rsync -a --exclude='.git' --exclude='.venv' --exclude='venv' --exclude='__pycache__' \
    --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
    --exclude='pgdata/' --exclude='.claude/' \
    --exclude='config.toml' --exclude='keys/' --exclude='.env' \
    --exclude='deploy/.env' --exclude='deploy/pgdata/' \
    "$SCRIPT_DIR/" "$APP_DIR/"

echo "=== 2. 啟動基礎服務（PostgreSQL + oauth2-proxy）==="
if [ ! -f "$DEPLOY_DIR/.env" ]; then
    cp "$DEPLOY_DIR/.env.example" "$DEPLOY_DIR/.env"
    # 自動填入絕對路徑和 cookie secret
    sed -i "s|^PGDATA_DIR=.*|PGDATA_DIR=${DEPLOY_DIR}/pgdata|" "$DEPLOY_DIR/.env"
    COOKIE_SECRET=$(openssl rand -base64 32 | head -c 32)
    sed -i "s|^OAUTH2_COOKIE_SECRET=.*|OAUTH2_COOKIE_SECRET=${COOKIE_SECRET}|" "$DEPLOY_DIR/.env"
    echo "已建立 deploy/.env（已自動產生 COOKIE_SECRET，請編輯其餘設定）"
    echo ""
    echo "  nano $DEPLOY_DIR/.env"
    echo ""
fi
docker compose -f "$DEPLOY_DIR/docker-compose.yml" up -d
echo "基礎服務已啟動"

# 等待 PostgreSQL 就緒
echo "等待 PostgreSQL 啟動..."
for i in $(seq 1 15); do
    PG_USER_CHECK=$(grep '^PG_USER=' "$DEPLOY_DIR/.env" | cut -d= -f2)
    if docker exec llm-gateway-pg pg_isready -U "${PG_USER_CHECK:-llm_gateway}" &>/dev/null; then
        echo "PostgreSQL 已就緒"
        break
    fi
    sleep 1
done

echo "=== 3. 安裝依賴 ==="
cd "$APP_DIR"
uv sync --frozen --no-dev

echo "=== 4. 檢查設定檔 ==="
if [ ! -f "$APP_DIR/config.toml" ]; then
    cp "$APP_DIR/config.toml.example" "$APP_DIR/config.toml"
    echo "已建立 config.toml（請編輯 vLLM server 位址與 API key）"
fi
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    # 從 deploy/.env 讀取 PG 設定自動填入 DATABASE_URL
    PG_USER=$(grep '^PG_USER=' "$DEPLOY_DIR/.env" | cut -d= -f2)
    PG_PASSWORD=$(grep '^PG_PASSWORD=' "$DEPLOY_DIR/.env" | cut -d= -f2)
    PG_PORT=$(grep '^PG_PORT=' "$DEPLOY_DIR/.env" | cut -d= -f2)
    PG_DB=$(grep '^PG_DB=' "$DEPLOY_DIR/.env" | cut -d= -f2)
    sed -i "s|DATABASE_URL=.*|DATABASE_URL=postgresql://${PG_USER}:${PG_PASSWORD}@localhost:${PG_PORT}/${PG_DB}|" "$APP_DIR/.env"
    echo "已建立 .env（已自動填入 DATABASE_URL）"
fi
chmod 600 "$APP_DIR/.env"
chmod 600 "$APP_DIR/config.toml"

echo "=== 5. 檢查金鑰 ==="
mkdir -p "$APP_DIR/keys"
if [ ! -f "$APP_DIR/keys/public.pem" ]; then
    echo "提醒：keys/public.pem 尚未放置"
    echo "  請從 AuthCenter 取得 public.pem 並放到 $APP_DIR/keys/public.pem"
else
    chmod 644 "$APP_DIR/keys/public.pem"
fi

echo "=== 6. 資料庫遷移 ==="
cd "$APP_DIR"
uv run alembic upgrade head 2>/dev/null || echo "提醒：資料庫遷移失敗，請確認 .env 中的 DATABASE_URL 設定正確"

echo "=== 7. 安裝 user-level systemd service ==="
mkdir -p "$HOME/.config/systemd/user"
cp "$DEPLOY_DIR/llm-gateway.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable llm-gateway
systemctl --user restart llm-gateway

echo "=== 8. 啟用 lingering（登出後保持服務執行）==="
sudo loginctl enable-linger "$(whoami)" 2>/dev/null || \
    echo "提醒：需要 sudo 執行 loginctl enable-linger $(whoami) 以確保登出後服務持續運行"

echo "=== 9. 安裝 nginx 設定（需要 sudo）==="
if command -v nginx &>/dev/null; then
    sed "s|__APP_DIR__|$APP_DIR|g" "$DEPLOY_DIR/llm-gateway.nginx.conf" \
        | sudo tee /etc/nginx/sites-available/llm-gateway > /dev/null
    sudo ln -sf /etc/nginx/sites-available/llm-gateway /etc/nginx/sites-enabled/llm-gateway
    sudo nginx -t && sudo systemctl reload nginx
else
    echo "提醒：nginx 未安裝，請手動設定反向代理"
fi

echo ""
echo "=== 部署完成 ==="
echo "應用目錄：$APP_DIR"
echo ""
echo "服務管理："
echo "  systemctl --user status llm-gateway                          # Gateway 狀態"
echo "  systemctl --user restart llm-gateway                         # Gateway 重啟"
echo "  journalctl --user -u llm-gateway -f                          # Gateway 日誌"
echo "  docker compose -f $DEPLOY_DIR/docker-compose.yml logs -f     # PG + oauth2-proxy 日誌"
echo "  docker compose -f $DEPLOY_DIR/docker-compose.yml restart     # PG + oauth2-proxy 重啟"
echo ""
echo "請確認："
echo "  1. 已編輯 $DEPLOY_DIR/.env（OIDC_ISSUER_URL、CLIENT_SECRET 等）"
echo "  2. 已編輯 $APP_DIR/config.toml（vLLM server 位址與 API key）"
echo "  3. 已放置 $APP_DIR/keys/public.pem（AuthCenter RS256 公鑰）"
echo "  4. 已修改 nginx 設定中的 server_name 為實際域名"
echo "     sudo nano /etc/nginx/sites-available/llm-gateway"
echo "     sudo nginx -t && sudo systemctl reload nginx"
