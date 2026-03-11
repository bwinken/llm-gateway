#!/bin/bash
# LLM Gateway 部署腳本（PostgreSQL Docker + User-Level systemd + Nginx）
# 用法: bash deploy/setup.sh
# 部署到 ~/opt/llm-gateway，以當前使用者身份執行
set -e

APP_DIR="$HOME/opt/llm-gateway"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# PostgreSQL 設定（可透過環境變數覆蓋）
PG_CONTAINER="llm-gateway-pg"
PG_USER="${PG_USER:-llm_gateway}"
PG_PASSWORD="${PG_PASSWORD:-your_password}"
PG_DB="${PG_DB:-llm_gateway}"
PG_PORT="${PG_PORT:-5432}"

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
    echo "錯誤：docker 未安裝（PostgreSQL 需要 Docker）" >&2
    exit 1
fi

echo "=== 1. 啟動 PostgreSQL Docker 容器 ==="
if docker ps --format '{{.Names}}' | grep -q "^${PG_CONTAINER}$"; then
    echo "PostgreSQL 容器已在運行中"
elif docker ps -a --format '{{.Names}}' | grep -q "^${PG_CONTAINER}$"; then
    echo "啟動已存在的 PostgreSQL 容器..."
    docker start "$PG_CONTAINER"
else
    echo "建立並啟動 PostgreSQL 容器..."
    mkdir -p "$HOME/opt/pgdata"
    docker run -d \
        --name "$PG_CONTAINER" \
        --restart unless-stopped \
        -e POSTGRES_USER="$PG_USER" \
        -e POSTGRES_PASSWORD="$PG_PASSWORD" \
        -e POSTGRES_DB="$PG_DB" \
        -p "127.0.0.1:${PG_PORT}:5432" \
        -v "$HOME/opt/pgdata:/var/lib/postgresql/data" \
        postgres:16
    echo "等待 PostgreSQL 啟動..."
    for i in $(seq 1 15); do
        if docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" &>/dev/null; then
            echo "PostgreSQL 已就緒"
            break
        fi
        sleep 1
    done
fi

echo "=== 2. 部署程式碼 ==="
mkdir -p "$APP_DIR"
rsync -a --exclude='.git' --exclude='.venv' --exclude='venv' --exclude='__pycache__' \
    --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
    --exclude='pgdata/' --exclude='.claude/' \
    --exclude='config.toml' --exclude='keys/' --exclude='.env' \
    "$SCRIPT_DIR/" "$APP_DIR/"

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
    # 自動填入 PostgreSQL 連線資訊
    sed -i "s|DATABASE_URL=.*|DATABASE_URL=postgresql://${PG_USER}:${PG_PASSWORD}@localhost:${PG_PORT}/${PG_DB}|" "$APP_DIR/.env"
    echo "已建立 .env（已自動填入 DATABASE_URL，請編輯其餘設定）"
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
cp "$APP_DIR/deploy/llm-gateway.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable llm-gateway
systemctl --user restart llm-gateway

# 確保使用者登出後服務仍繼續執行
echo "=== 8. 啟用 lingering（登出後保持服務執行）==="
sudo loginctl enable-linger "$(whoami)" 2>/dev/null || \
    echo "提醒：需要 sudo 執行 loginctl enable-linger $(whoami) 以確保登出後服務持續運行"

echo "=== 9. 安裝 nginx 設定（需要 sudo）==="
if command -v nginx &>/dev/null; then
    sed "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/deploy/llm-gateway.nginx.conf" \
        | sudo tee /etc/nginx/sites-available/llm-gateway > /dev/null
    sudo ln -sf /etc/nginx/sites-available/llm-gateway /etc/nginx/sites-enabled/llm-gateway
    sudo nginx -t && sudo systemctl reload nginx
else
    echo "提醒：nginx 未安裝，請手動設定反向代理"
fi

echo ""
echo "=== 部署完成 ==="
echo "應用目錄：$APP_DIR"
echo "PostgreSQL：docker container '$PG_CONTAINER' (127.0.0.1:$PG_PORT)"
echo "資料目錄：$HOME/opt/pgdata"
echo ""
echo "服務管理："
echo "  systemctl --user status llm-gateway    # 查看狀態"
echo "  systemctl --user restart llm-gateway   # 重啟"
echo "  journalctl --user -u llm-gateway -f    # 查看日誌"
echo "  docker logs -f $PG_CONTAINER           # PostgreSQL 日誌"
echo ""
echo "請確認："
echo "  1. 已編輯 $APP_DIR/config.toml（vLLM server 位址與 API key）"
echo "  2. 已編輯 $APP_DIR/.env（SECRET_KEY、AuthCenter 設定）"
echo "  3. 已放置 $APP_DIR/keys/public.pem（AuthCenter RS256 公鑰）"
echo "  4. 已修改 nginx 設定中的 server_name 為實際域名"
echo "     sudo nano /etc/nginx/sites-available/llm-gateway"
echo "     sudo nginx -t && sudo systemctl reload nginx"
echo "  5.（可選）sudo certbot --nginx -d your-domain.com"
