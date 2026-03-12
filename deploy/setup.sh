#!/bin/bash
# LLM Gateway 一鍵部署腳本
# 用法: bash deploy/setup.sh
# 部署到 ~/opt/llm-gateway，以當前使用者身份執行
set -e

# ── 顏色定義 ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

APP_DIR="$HOME/opt/llm-gateway"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_DIR="$APP_DIR/deploy"

info()  { echo -e "${CYAN}▸${NC} $*"; }
ok()    { echo -e "${GREEN}✔${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠${NC} $*"; }
err()   { echo -e "${RED}✘${NC} $*" >&2; }
header() { echo -e "\n${BOLD}═══ $* ═══${NC}"; }

# 讀取使用者輸入（帶預設值）
ask() {
    local prompt="$1" default="$2" var="$3"
    if [ -n "$default" ]; then
        read -rp "$(echo -e "${CYAN}?${NC} ${prompt} [${default}]: ")" input
        eval "$var='${input:-$default}'"
    else
        read -rp "$(echo -e "${CYAN}?${NC} ${prompt}: ")" input
        eval "$var='$input'"
    fi
}

# 是否確認（預設 Y）
confirm() {
    local prompt="$1"
    read -rp "$(echo -e "${CYAN}?${NC} ${prompt} [Y/n]: ")" yn
    case "$yn" in [nN]*) return 1 ;; *) return 0 ;; esac
}

# ╔══════════════════════════════════════════════════════════╗
# ║                     前置檢查                             ║
# ╚══════════════════════════════════════════════════════════╝
header "前置環境檢查"

MISSING=0
for cmd in uv docker rsync openssl; do
    if command -v "$cmd" &>/dev/null; then
        ok "$cmd $(command -v "$cmd")"
    else
        err "$cmd 未安裝"
        MISSING=1
    fi
done

if [ "$MISSING" -eq 1 ]; then
    echo ""
    err "請先安裝缺少的工具後再執行此腳本"
    command -v uv &>/dev/null || info "uv: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
fi

# 檢查 docker daemon
if ! docker info &>/dev/null 2>&1; then
    err "Docker daemon 未運行，請先啟動 Docker"
    exit 1
fi
ok "Docker daemon 正在運行"

# Proxy
PROXY_URL="${http_proxy:-}"
if [ -n "$PROXY_URL" ]; then
    export http_proxy="$PROXY_URL" HTTP_PROXY="$PROXY_URL"
    export https_proxy="$PROXY_URL" HTTPS_PROXY="$PROXY_URL"
    export no_proxy="localhost,127.0.0.1,*.company.local"
    export NO_PROXY="$no_proxy"
    info "使用 Proxy: $PROXY_URL"
fi

echo ""
info "部署目錄：${BOLD}$APP_DIR${NC}"
info "來源目錄：$SCRIPT_DIR"
echo ""

if ! confirm "開始部署？"; then
    echo "已取消。"
    exit 0
fi

# ╔══════════════════════════════════════════════════════════╗
# ║                 Step 1: 部署程式碼                        ║
# ╚══════════════════════════════════════════════════════════╝
header "Step 1/9  部署程式碼"

mkdir -p "$APP_DIR"
rsync -a --exclude='.git' --exclude='.venv' --exclude='venv' --exclude='__pycache__' \
    --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
    --exclude='pgdata/' --exclude='.claude/' \
    --exclude='config.toml' --exclude='keys/' --exclude='.env' \
    --exclude='deploy/.env' --exclude='deploy/pgdata/' \
    "$SCRIPT_DIR/" "$APP_DIR/"
ok "程式碼已同步到 $APP_DIR"

# ╔══════════════════════════════════════════════════════════╗
# ║           Step 2: 設定 Docker 基礎服務                    ║
# ╚══════════════════════════════════════════════════════════╝
header "Step 2/9  設定 Docker 基礎服務（PostgreSQL + oauth2-proxy）"

if [ -f "$DEPLOY_DIR/.env" ]; then
    ok "deploy/.env 已存在，跳過設定"
    info "如需重新設定：nano $DEPLOY_DIR/.env"
else
    info "首次部署，需要設定基礎服務參數"
    echo ""

    # PostgreSQL
    echo -e "${BOLD}── PostgreSQL ──${NC}"
    ask "資料庫使用者名稱" "llm_gateway" PG_USER
    ask "資料庫密碼" "" PG_PASSWORD
    while [ -z "$PG_PASSWORD" ]; do
        warn "密碼不能為空"
        ask "資料庫密碼" "" PG_PASSWORD
    done
    ask "資料庫名稱" "llm_gateway" PG_DB
    ask "資料庫 Port" "5432" PG_PORT
    echo ""

    # oauth2-proxy
    echo -e "${BOLD}── oauth2-proxy（SSO 登入）──${NC}"
    ask "OIDC Issuer URL（AuthCenter 位址）" "" OIDC_ISSUER_URL
    while [ -z "$OIDC_ISSUER_URL" ]; do
        warn "OIDC Issuer URL 不能為空"
        ask "OIDC Issuer URL（AuthCenter 位址）" "" OIDC_ISSUER_URL
    done
    ask "OAuth2 Client ID" "llm_gateway" OAUTH2_CLIENT_ID
    ask "OAuth2 Client Secret" "" OAUTH2_CLIENT_SECRET
    while [ -z "$OAUTH2_CLIENT_SECRET" ]; do
        warn "Client Secret 不能為空"
        ask "OAuth2 Client Secret" "" OAUTH2_CLIENT_SECRET
    done
    echo ""

    echo -e "${BOLD}── 網域設定 ──${NC}"
    ask "Gateway 域名（用於 oauth2-proxy callback）" "llm-gateway.your-domain.com" DOMAIN
    OAUTH2_REDIRECT_URL="http://${DOMAIN}/oauth2/callback"
    info "OAuth2 Redirect URL: $OAUTH2_REDIRECT_URL"
    echo ""

    # 產生 cookie secret
    COOKIE_SECRET=$(openssl rand -base64 32 | head -c 32)

    # 寫入 deploy/.env
    cat > "$DEPLOY_DIR/.env" <<ENVEOF
# deploy/.env — docker compose 環境變數（由 setup.sh 自動產生）

# PostgreSQL
PG_USER=${PG_USER}
PG_PASSWORD=${PG_PASSWORD}
PG_DB=${PG_DB}
PG_PORT=${PG_PORT}
PGDATA_DIR=${DEPLOY_DIR}/pgdata

# oauth2-proxy
OIDC_ISSUER_URL=${OIDC_ISSUER_URL}
OAUTH2_CLIENT_ID=${OAUTH2_CLIENT_ID}
OAUTH2_CLIENT_SECRET=${OAUTH2_CLIENT_SECRET}
OAUTH2_REDIRECT_URL=${OAUTH2_REDIRECT_URL}
OAUTH2_COOKIE_NAME=_llm_gw_oauth2
OAUTH2_COOKIE_SECRET=${COOKIE_SECRET}
ENVEOF
    chmod 600 "$DEPLOY_DIR/.env"
    ok "deploy/.env 已建立"
fi

# ╔══════════════════════════════════════════════════════════╗
# ║           Step 3: 啟動 Docker 服務                       ║
# ╚══════════════════════════════════════════════════════════╝
header "Step 3/9  啟動 Docker 服務"

docker compose -f "$DEPLOY_DIR/docker-compose.yml" up -d
ok "Docker 服務已啟動"

# 等待 PostgreSQL 就緒
info "等待 PostgreSQL..."
PG_USER_CHECK=$(grep '^PG_USER=' "$DEPLOY_DIR/.env" | cut -d= -f2)
PG_READY=0
for i in $(seq 1 15); do
    if docker exec llm-gateway-pg pg_isready -U "${PG_USER_CHECK:-llm_gateway}" &>/dev/null; then
        PG_READY=1
        break
    fi
    sleep 1
done
if [ "$PG_READY" -eq 1 ]; then
    ok "PostgreSQL 已就緒"
else
    warn "PostgreSQL 尚未就緒，繼續部署（可能需要手動檢查）"
fi

# ╔══════════════════════════════════════════════════════════╗
# ║           Step 4: 安裝 Python 依賴                       ║
# ╚══════════════════════════════════════════════════════════╝
header "Step 4/9  安裝 Python 依賴"

cd "$APP_DIR"
uv sync --frozen --no-dev
ok "依賴安裝完成"

# ╔══════════════════════════════════════════════════════════╗
# ║           Step 5: 應用程式設定檔                          ║
# ╚══════════════════════════════════════════════════════════╝
header "Step 5/9  應用程式設定檔"

# config.toml
if [ -f "$APP_DIR/config.toml" ]; then
    ok "config.toml 已存在"
else
    cp "$APP_DIR/config.toml.example" "$APP_DIR/config.toml"
    warn "已建立 config.toml — 部署完成後請編輯 vLLM server 位址與 API key"
    info "  nano $APP_DIR/config.toml"
fi

# .env
if [ -f "$APP_DIR/.env" ]; then
    ok ".env 已存在"
else
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    # 從 deploy/.env 讀取 PG 設定自動填入 DATABASE_URL
    PG_USER=$(grep '^PG_USER=' "$DEPLOY_DIR/.env" | cut -d= -f2)
    PG_PASSWORD=$(grep '^PG_PASSWORD=' "$DEPLOY_DIR/.env" | cut -d= -f2)
    PG_PORT=$(grep '^PG_PORT=' "$DEPLOY_DIR/.env" | cut -d= -f2)
    PG_DB=$(grep '^PG_DB=' "$DEPLOY_DIR/.env" | cut -d= -f2)
    sed -i "s|DATABASE_URL=.*|DATABASE_URL=postgresql://${PG_USER}:${PG_PASSWORD}@localhost:${PG_PORT}/${PG_DB}|" "$APP_DIR/.env"
    ok ".env 已建立（DATABASE_URL 已自動填入）"
fi
chmod 600 "$APP_DIR/.env"
chmod 600 "$APP_DIR/config.toml"

# ╔══════════════════════════════════════════════════════════╗
# ║           Step 6: AuthCenter 公鑰                        ║
# ╚══════════════════════════════════════════════════════════╝
header "Step 6/9  AuthCenter 公鑰"

mkdir -p "$APP_DIR/keys"
if [ -f "$APP_DIR/keys/public.pem" ]; then
    ok "keys/public.pem 已存在"
    chmod 644 "$APP_DIR/keys/public.pem"
else
    warn "keys/public.pem 尚未放置"
    echo ""
    info "請將 AuthCenter 的 RS256 公鑰複製到："
    info "  ${BOLD}$APP_DIR/keys/public.pem${NC}"
    echo ""
    if confirm "是否現在貼上公鑰內容？（選 n 則稍後手動放置）"; then
        echo -e "${CYAN}請貼上 PEM 內容，貼完後按 Ctrl+D：${NC}"
        cat > "$APP_DIR/keys/public.pem"
        chmod 644 "$APP_DIR/keys/public.pem"
        ok "public.pem 已儲存"
    else
        warn "請稍後手動放置 public.pem，否則 JWT 驗證會失敗"
    fi
fi

# ╔══════════════════════════════════════════════════════════╗
# ║           Step 7: 資料庫遷移                              ║
# ╚══════════════════════════════════════════════════════════╝
header "Step 7/9  資料庫遷移"

cd "$APP_DIR"
if uv run alembic upgrade head 2>/dev/null; then
    ok "資料庫 schema 已同步"
else
    warn "資料庫遷移失敗 — 請確認 PostgreSQL 已啟動且 .env 中的 DATABASE_URL 正確"
fi

# ╔══════════════════════════════════════════════════════════╗
# ║           Step 8: systemd 服務                           ║
# ╚══════════════════════════════════════════════════════════╝
header "Step 8/9  安裝 systemd 服務"

mkdir -p "$HOME/.config/systemd/user"
cp "$DEPLOY_DIR/llm-gateway.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable llm-gateway
systemctl --user restart llm-gateway
ok "llm-gateway.service 已啟動"

# lingering
if loginctl show-user "$(whoami)" 2>/dev/null | grep -q "Linger=yes"; then
    ok "Lingering 已啟用"
else
    info "啟用 lingering（需要 sudo，確保登出後服務繼續運行）"
    sudo loginctl enable-linger "$(whoami)" 2>/dev/null && ok "Lingering 已啟用" || \
        warn "請手動執行：sudo loginctl enable-linger $(whoami)"
fi

# ╔══════════════════════════════════════════════════════════╗
# ║           Step 9: Nginx 設定                             ║
# ╚══════════════════════════════════════════════════════════╝
header "Step 9/9  Nginx 設定"

if ! command -v nginx &>/dev/null; then
    warn "nginx 未安裝，跳過此步驟"
    info "請手動安裝 nginx 並設定反向代理"
    info "  設定檔模板：$DEPLOY_DIR/llm-gateway.nginx.conf"
else
    # 讀取域名（優先從 deploy/.env 取得）
    DOMAIN_FROM_ENV=$(grep '^OAUTH2_REDIRECT_URL=' "$DEPLOY_DIR/.env" | sed 's|.*://||;s|/.*||')
    if [ -n "$DOMAIN_FROM_ENV" ] && [ "$DOMAIN_FROM_ENV" != "llm-gateway.your-domain.com" ]; then
        NGINX_DOMAIN="$DOMAIN_FROM_ENV"
    else
        ask "Nginx server_name（域名）" "llm-gateway.your-domain.com" NGINX_DOMAIN
    fi

    sed -e "s|__APP_DIR__|$APP_DIR|g" \
        -e "s|llm-gateway.your-domain.com|$NGINX_DOMAIN|g" \
        "$DEPLOY_DIR/llm-gateway.nginx.conf" \
        | sudo tee /etc/nginx/sites-available/llm-gateway > /dev/null
    sudo ln -sf /etc/nginx/sites-available/llm-gateway /etc/nginx/sites-enabled/llm-gateway

    if sudo nginx -t 2>/dev/null; then
        sudo systemctl reload nginx
        ok "Nginx 設定已安裝（server_name: $NGINX_DOMAIN）"
    else
        err "Nginx 設定檔語法錯誤，請手動檢查"
        info "  sudo nano /etc/nginx/sites-available/llm-gateway"
        info "  sudo nginx -t && sudo systemctl reload nginx"
    fi
fi

# ╔══════════════════════════════════════════════════════════╗
# ║                   部署完成                                ║
# ╚══════════════════════════════════════════════════════════╝
echo ""
echo -e "${GREEN}${BOLD}══════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}           部署完成！${NC}"
echo -e "${GREEN}${BOLD}══════════════════════════════════════════${NC}"
echo ""

# 狀態總結
echo -e "${BOLD}部署狀態：${NC}"
echo -e "  應用目錄    $APP_DIR"

# 檢查各服務狀態
if docker ps --format '{{.Names}}' | grep -q llm-gateway-pg; then
    echo -e "  PostgreSQL  ${GREEN}running${NC}"
else
    echo -e "  PostgreSQL  ${RED}stopped${NC}"
fi
if docker ps --format '{{.Names}}' | grep -q llm-gateway-oauth2-proxy; then
    echo -e "  oauth2-proxy ${GREEN}running${NC}"
else
    echo -e "  oauth2-proxy ${RED}stopped${NC}"
fi
if systemctl --user is-active llm-gateway &>/dev/null; then
    echo -e "  Gateway     ${GREEN}running${NC}"
else
    echo -e "  Gateway     ${RED}stopped${NC}"
fi
if command -v nginx &>/dev/null && systemctl is-active nginx &>/dev/null; then
    echo -e "  Nginx       ${GREEN}running${NC}"
else
    echo -e "  Nginx       ${YELLOW}unchecked${NC}"
fi

# 待辦事項
TODOS=()
[ ! -f "$APP_DIR/keys/public.pem" ] && TODOS+=("放置 AuthCenter 公鑰：$APP_DIR/keys/public.pem")
if grep -q "your_password" "$APP_DIR/.env" 2>/dev/null; then
    TODOS+=("確認 .env 中的 DATABASE_URL 密碼已修改")
fi
if grep -q "change-me" "$DEPLOY_DIR/.env" 2>/dev/null; then
    TODOS+=("編輯 deploy/.env 填入正確的 OAUTH2_CLIENT_SECRET")
fi
# config.toml 總是需要檢查
TODOS+=("編輯 config.toml 設定 vLLM server 位址與 API key")

if [ ${#TODOS[@]} -gt 0 ]; then
    echo ""
    echo -e "${BOLD}待辦事項：${NC}"
    for i in "${!TODOS[@]}"; do
        echo -e "  ${YELLOW}$((i+1)).${NC} ${TODOS[$i]}"
    done
fi

echo ""
echo -e "${BOLD}常用指令：${NC}"
echo "  systemctl --user status llm-gateway          # Gateway 狀態"
echo "  systemctl --user restart llm-gateway         # Gateway 重啟"
echo "  journalctl --user -u llm-gateway -f          # Gateway 日誌"
echo "  docker compose -f $DEPLOY_DIR/docker-compose.yml logs -f  # 基礎服務日誌"
echo ""
