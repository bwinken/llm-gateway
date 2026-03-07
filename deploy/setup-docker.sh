#!/bin/bash
# LLM Gateway 部署腳本（Docker Compose + Nginx）
# 用法: bash deploy/setup-docker.sh
# 在當前目錄使用 Docker Compose 啟動 Gateway + PostgreSQL
set -e

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"

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

echo "=== LLM Gateway — Docker + Nginx 部署 ==="
echo "應用目錄：$APP_DIR"
echo ""

echo "=== 1. 檢查設定檔 ==="
if [ ! -f "$APP_DIR/config.toml" ]; then
    cp "$APP_DIR/config.toml.example" "$APP_DIR/config.toml"
    echo "已建立 config.toml（請編輯 vLLM server 位址與 API key）"
fi
if [ ! -f "$APP_DIR/.env" ]; then
    echo ""
    echo "警告：$APP_DIR/.env 尚未建立，跳過容器啟動。" >&2
    echo "請執行以下步驟完成部署：" >&2
    echo "  1. cp $APP_DIR/.env.example $APP_DIR/.env" >&2
    echo "  2. nano $APP_DIR/.env  （填入實際設定）" >&2
    echo "  3. 重新執行此腳本" >&2
    exit 1
fi

echo "=== 2. 檢查 DATABASE_URL ==="
if grep -q "localhost:5432" "$APP_DIR/.env" 2>/dev/null; then
    echo "警告：DATABASE_URL 使用 'localhost'，Docker 環境下應改為 'postgres'" >&2
    echo "  修改 .env 中的 DATABASE_URL 為：" >&2
    echo "  DATABASE_URL=postgresql://llm_gateway:your_password@postgres:5432/llm_gateway" >&2
    echo ""
fi

echo "=== 3. 檢查金鑰 ==="
mkdir -p "$APP_DIR/keys"
if [ ! -f "$APP_DIR/keys/public.pem" ]; then
    echo "提醒：keys/public.pem 尚未放置"
    echo "  請從 AuthCenter 取得 public.pem 並放到 $APP_DIR/keys/public.pem"
fi

echo "=== 4. 建置並啟動容器 ==="
docker compose -f "$APP_DIR/docker-compose.yml" up -d --build
echo "容器已啟動"

echo "=== 5. 安裝 nginx 設定（需要 sudo）==="
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
echo "服務管理："
echo "  docker compose logs -f gateway       # 查看日誌"
echo "  docker compose restart gateway       # 重啟"
echo "  docker compose down                  # 停止"
echo "  docker compose up -d --build         # 重建並啟動"
echo ""
echo "請確認："
echo "  1. 已編輯 $APP_DIR/config.toml（vLLM server 位址與 API key）"
echo "  2. 已編輯 $APP_DIR/.env（SECRET_KEY、DATABASE_URL 使用 'postgres'、AuthCenter 設定）"
echo "  3. 已放置 $APP_DIR/keys/public.pem（AuthCenter RS256 公鑰）"
echo "  4. 已修改 nginx 設定中的 server_name 為實際域名"
echo "     sudo nano /etc/nginx/sites-available/llm-gateway"
echo "     sudo nginx -t && sudo systemctl reload nginx"
echo "  5.（可選）sudo certbot --nginx -d your-domain.com"
