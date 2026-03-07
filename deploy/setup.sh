#!/bin/bash
# LLM Gateway 部署腳本（User-Level systemd + Nginx）
# 用法: bash deploy/setup.sh
# 部署到 ~/llm-gateway，以當前使用者身份執行（不需要 sudo）
set -e

APP_DIR="$HOME/llm-gateway"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

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
if ! command -v python3 &>/dev/null; then
    echo "錯誤：python3 未安裝" >&2
    exit 1
fi

echo "=== 1. 部署程式碼 ==="
mkdir -p "$APP_DIR"
rsync -a --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
    --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
    --exclude='pgdata/' \
    --exclude='config.toml' --exclude='keys/' --exclude='.env' \
    "$SCRIPT_DIR/" "$APP_DIR/"

echo "=== 2. 安裝依賴 ==="
if [ ! -d "$APP_DIR/venv" ]; then
    python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "=== 3. 檢查設定檔 ==="
if [ ! -f "$APP_DIR/config.toml" ]; then
    cp "$APP_DIR/config.toml.example" "$APP_DIR/config.toml"
    echo "已建立 config.toml（請編輯 vLLM server 位址與 API key）"
fi
if [ ! -f "$APP_DIR/.env" ]; then
    echo ""
    echo "警告：$APP_DIR/.env 尚未建立，跳過服務啟動。" >&2
    echo "請執行以下步驟完成部署：" >&2
    echo "  1. cp $APP_DIR/.env.example $APP_DIR/.env" >&2
    echo "  2. nano $APP_DIR/.env  （填入實際設定）" >&2
    echo "  3. 重新執行此腳本" >&2
    exit 1
fi
chmod 600 "$APP_DIR/.env"
chmod 600 "$APP_DIR/config.toml"

echo "=== 4. 檢查金鑰 ==="
mkdir -p "$APP_DIR/keys"
if [ ! -f "$APP_DIR/keys/public.pem" ]; then
    echo "提醒：keys/public.pem 尚未放置"
    echo "  請從 AuthCenter 取得 public.pem 並放到 $APP_DIR/keys/public.pem"
else
    chmod 644 "$APP_DIR/keys/public.pem"
fi

echo "=== 5. 安裝 user-level systemd service ==="
mkdir -p "$HOME/.config/systemd/user"
cp "$APP_DIR/deploy/llm-gateway.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable llm-gateway
systemctl --user restart llm-gateway

# 確保使用者登出後服務仍繼續執行
echo "=== 6. 啟用 lingering（登出後保持服務執行）==="
sudo loginctl enable-linger "$(whoami)" 2>/dev/null || \
    echo "提醒：需要 sudo 執行 loginctl enable-linger $(whoami) 以確保登出後服務持續運行"

echo "=== 7. 安裝 nginx 設定（需要 sudo）==="
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
echo "服務管理："
echo "  systemctl --user status llm-gateway    # 查看狀態"
echo "  systemctl --user restart llm-gateway   # 重啟"
echo "  journalctl --user -u llm-gateway -f    # 查看日誌"
echo ""
echo "請確認："
echo "  1. 已編輯 $APP_DIR/config.toml（vLLM server 位址與 API key）"
echo "  2. 已編輯 $APP_DIR/.env（SECRET_KEY、DATABASE_URL、AuthCenter 設定）"
echo "  3. 已放置 $APP_DIR/keys/public.pem（AuthCenter RS256 公鑰）"
echo "  4. 已修改 nginx 設定中的 server_name 為實際域名"
echo "     sudo nano /etc/nginx/sites-available/llm-gateway"
echo "     sudo nginx -t && sudo systemctl reload nginx"
echo "  5.（可選）sudo certbot --nginx -d your-domain.com"
