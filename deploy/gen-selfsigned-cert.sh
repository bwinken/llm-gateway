#!/bin/bash
# ============================================================
# LLM Gateway 自簽憑證產生腳本（airgapped 環境適用）
# ============================================================
# 用途：
#   為 deploy/llm-gateway-example.nginx.conf 的 :443 server block
#   產生一張 self-signed TLS 憑證。瀏覽器 / API client 會跳出
#   「不受信任」警告，但 TLS 加密本身是有效的。
#
# 用法：
#   bash deploy/gen-selfsigned-cert.sh
#
# 產出：
#   <output_dir>/llm-gateway.crt  （憑證，644）
#   <output_dir>/llm-gateway.key  （私鑰，600）
#
# 下一步：
#   把 nginx 設定的 ssl_certificate / ssl_certificate_key 指向
#   這兩個檔案，然後 sudo nginx -t && sudo systemctl reload nginx
# ============================================================
set -e

# ── 顏色定義（與 setup.sh 一致）──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()   { echo -e "${CYAN}▸${NC} $*"; }
ok()     { echo -e "${GREEN}✔${NC} $*"; }
warn()   { echo -e "${YELLOW}⚠${NC} $*"; }
err()    { echo -e "${RED}✘${NC} $*" >&2; }
header() { echo -e "\n${BOLD}═══ $* ═══${NC}"; }

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

confirm() {
    local prompt="$1"
    read -rp "$(echo -e "${CYAN}?${NC} ${prompt} [Y/n]: ")" yn
    case "$yn" in [nN]*) return 1 ;; *) return 0 ;; esac
}

# ╔══════════════════════════════════════════════════════════╗
# ║             Step 1: 前置檢查                              ║
# ╚══════════════════════════════════════════════════════════╝
header "Step 1/5  前置檢查"

if ! command -v openssl &>/dev/null; then
    err "openssl 未安裝，無法產生憑證"
    echo ""
    info "請先安裝 openssl："
    info "  Debian/Ubuntu : sudo apt install openssl"
    info "  RHEL/Rocky    : sudo dnf install openssl"
    exit 1
fi
ok "openssl $(openssl version | awk '{print $2}')"

# ╔══════════════════════════════════════════════════════════╗
# ║             Step 2: 收集憑證參數                          ║
# ╚══════════════════════════════════════════════════════════╝
header "Step 2/5  憑證參數"

info "Common Name (CN) 會成為憑證的主識別名稱，通常填主機的 FQDN。"
ask "主機名 (FQDN)" "llm-gateway.your-domain.com" CN

echo ""
info "Subject Alternative Name (SAN) 是 client 實際檢查的欄位。"
info "列出所有會被用來連到這台機器的名稱／IP，以空白分隔。"
info "例如：$CN llm-gateway localhost 127.0.0.1 10.0.0.5"
ask "SAN 清單 (DNS + IP 混合)" "$CN localhost 127.0.0.1" SAN_RAW

# 分類 DNS / IP
SAN_LINES=""
idx_dns=1
idx_ip=1
for entry in $SAN_RAW; do
    if [[ "$entry" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || [[ "$entry" == *:* ]]; then
        SAN_LINES+="IP.${idx_ip} = ${entry}"$'\n'
        idx_ip=$((idx_ip + 1))
    else
        SAN_LINES+="DNS.${idx_dns} = ${entry}"$'\n'
        idx_dns=$((idx_dns + 1))
    fi
done

echo ""
info "憑證有效天數（self-signed 通常設長一點，免得忘記更新）"
ask "有效天數" "3650" DAYS

echo ""
info "金鑰長度（RSA 2048 已足夠，4096 更安全但 handshake 略慢）"
ask "RSA 金鑰長度 (2048/3072/4096)" "2048" KEY_BITS

echo ""
info "輸出目錄（nginx 讀得到就好，建議放在 /etc/nginx/ssl）"
ask "輸出目錄" "/etc/nginx/ssl" OUT_DIR

CRT_PATH="$OUT_DIR/llm-gateway.crt"
KEY_PATH="$OUT_DIR/llm-gateway.key"

# ╔══════════════════════════════════════════════════════════╗
# ║             Step 3: 預覽 + 確認                           ║
# ╚══════════════════════════════════════════════════════════╝
header "Step 3/5  確認"

echo -e "${BOLD}即將產生以下憑證：${NC}"
echo "  CN           : $CN"
echo "  有效期       : $DAYS 天"
echo "  金鑰長度     : RSA $KEY_BITS"
echo "  憑證路徑     : $CRT_PATH"
echo "  私鑰路徑     : $KEY_PATH"
echo "  SAN："
echo "$SAN_LINES" | sed 's/^/    /'

if [ -e "$CRT_PATH" ] || [ -e "$KEY_PATH" ]; then
    warn "目標路徑已有檔案，繼續會覆寫原有憑證／私鑰"
fi

if ! confirm "確認產生？"; then
    err "使用者取消"
    exit 1
fi

# ╔══════════════════════════════════════════════════════════╗
# ║             Step 4: 產生憑證                              ║
# ╚══════════════════════════════════════════════════════════╝
header "Step 4/5  產生憑證"

# 需要 sudo 建立目錄 + 寫檔（尤其 /etc/nginx/ssl）
SUDO=""
if [ ! -w "$(dirname "$OUT_DIR")" ] 2>/dev/null; then
    SUDO="sudo"
fi

info "建立輸出目錄 $OUT_DIR"
$SUDO mkdir -p "$OUT_DIR"

# 產生 OpenSSL 設定檔（包含 SAN），用暫存檔避免污染 current dir
TMP_CNF=$(mktemp)
trap 'rm -f "$TMP_CNF"' EXIT

cat >"$TMP_CNF" <<EOF
[req]
distinguished_name = req_distinguished_name
x509_extensions    = v3_req
prompt             = no

[req_distinguished_name]
CN = $CN

[v3_req]
basicConstraints     = CA:FALSE
keyUsage             = digitalSignature, keyEncipherment
extendedKeyUsage     = serverAuth
subjectAltName       = @alt_names

[alt_names]
$SAN_LINES
EOF

info "產生 RSA $KEY_BITS 私鑰 + self-signed 憑證（有效 $DAYS 天）"
$SUDO openssl req -x509 -nodes \
    -newkey "rsa:$KEY_BITS" \
    -days "$DAYS" \
    -keyout "$KEY_PATH" \
    -out "$CRT_PATH" \
    -config "$TMP_CNF" 2>&1 | sed 's/^/    /'

info "設定檔案權限（key 600、crt 644）"
$SUDO chmod 600 "$KEY_PATH"
$SUDO chmod 644 "$CRT_PATH"
$SUDO chown root:root "$KEY_PATH" "$CRT_PATH" 2>/dev/null || true

ok "憑證產生完成"

# ╔══════════════════════════════════════════════════════════╗
# ║             Step 5: 驗證 + 下一步指引                     ║
# ╚══════════════════════════════════════════════════════════╝
header "Step 5/5  驗證 + 下一步"

info "憑證內容摘要："
$SUDO openssl x509 -in "$CRT_PATH" -noout \
    -subject -issuer -dates -ext subjectAltName 2>&1 | sed 's/^/    /'

echo ""
ok "一切就緒。請依下列步驟接上 nginx："
echo ""
echo -e "${BOLD}1.${NC} 複製範本到正式路徑（若尚未複製）："
echo "     cp deploy/llm-gateway-example.nginx.conf deploy/llm-gateway.nginx.conf"
echo ""
echo -e "${BOLD}2.${NC} 編輯 deploy/llm-gateway.nginx.conf，把 :443 server block 裡的："
echo "     ssl_certificate     /etc/letsencrypt/live/.../fullchain.pem;"
echo "     ssl_certificate_key /etc/letsencrypt/live/.../privkey.pem;"
echo "   改成："
echo -e "     ${GREEN}ssl_certificate     $CRT_PATH;${NC}"
echo -e "     ${GREEN}ssl_certificate_key $KEY_PATH;${NC}"
echo ""
echo -e "${BOLD}3.${NC} 把 server_name 從 llm-gateway.your-domain.com 改成："
echo -e "     ${GREEN}server_name $CN;${NC}"
echo ""
echo -e "${BOLD}4.${NC} 安裝到 nginx 並 reload："
echo "     sudo cp deploy/llm-gateway.nginx.conf /etc/nginx/sites-available/llm-gateway"
echo "     sudo ln -sf /etc/nginx/sites-available/llm-gateway /etc/nginx/sites-enabled/"
echo "     sudo nginx -t && sudo systemctl reload nginx"
echo ""
echo -e "${BOLD}5.${NC} 測試 HTTPS（self-signed 需要 -k 跳過驗證）："
echo "     curl -kv https://$CN/v1/models -H 'Authorization: Bearer <your-api-key>'"
echo ""
warn "注意：self-signed 憑證 client 端會看到警告。若是 Python httpx/requests："
warn "  httpx.Client(verify='$CRT_PATH')  # 把憑證加到 trust store"
warn "  或設 verify=False（不建議，等同沒加密保護）"
