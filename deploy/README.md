# LLM Gateway 部署指南

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) 套件管理工具
- Docker（用於 PostgreSQL）
- Nginx（透過 `systemctl` 管理）
- AuthCenter RS256 public key（`keys/public.pem`）

---

## 部署方式：systemd user service + PostgreSQL Docker + Nginx

使用 user-level systemd 執行 Gateway，Docker 執行 PostgreSQL。不需要 root（nginx 和 linger 除外）。

### 首次部署

```bash
git clone https://github.com/bwinken/llm-gateway.git
cd llm-gateway

# 建立並編輯設定檔
cp .env.example .env && nano .env
cp config.toml.example config.toml && nano config.toml

# 放置 AuthCenter 公鑰
mkdir -p keys && cp /path/to/public.pem keys/public.pem

# 如需 Proxy，先設定環境變數
export http_proxy=http://proxy.company.com:8080

# 如需自訂 PostgreSQL 密碼
export PG_PASSWORD=my_secure_password

# 執行部署
bash deploy/setup.sh
```

腳本會自動：
1. 用 Docker 啟動 PostgreSQL 16（資料存於 `~/opt/pgdata`，僅綁定 127.0.0.1）
2. rsync 程式碼到 `~/opt/llm-gateway`（排除 `.env`、`config.toml`、`keys/`）
3. `uv sync` 安裝依賴
4. 自動建立 `.env` 並填入 DATABASE_URL
5. `uv run alembic upgrade head` 執行資料庫遷移
6. 設定 user-level systemd service
7. 啟用 lingering（登出後服務持續運行）
8. 設定 nginx reverse proxy

### 更新程式碼

```bash
cd /path/to/llm-gateway   # 原始 clone 目錄
git pull

# 重新執行部署腳本（會 rsync + 更新依賴 + 重啟服務，PostgreSQL 不受影響）
bash deploy/setup.sh
```

如果只改了程式碼、沒有新增依賴，也可以手動更新：

```bash
cd /path/to/llm-gateway
git pull
rsync -a --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
    --exclude='config.toml' --exclude='.env' --exclude='keys/' \
    ./ ~/opt/llm-gateway/
systemctl --user restart llm-gateway
```

> `config.toml`、`.env`、`keys/` 不會被覆蓋。

### 服務管理

```bash
# Gateway
systemctl --user status llm-gateway     # 查看狀態
systemctl --user restart llm-gateway    # 重啟
systemctl --user stop llm-gateway       # 停止
journalctl --user -u llm-gateway -f     # 查看日誌

# PostgreSQL
docker logs -f llm-gateway-pg           # 查看日誌
docker restart llm-gateway-pg           # 重啟
docker stop llm-gateway-pg              # 停止
```

### 目錄結構

```
~/opt/
├── llm-gateway/          # 應用程式
│   ├── app/
│   ├── .venv/            # uv 管理的虛擬環境
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── config.toml
│   ├── .env
│   └── keys/public.pem
└── pgdata/               # PostgreSQL 資料
```

### 相關檔案

- [setup.sh](setup.sh) — 部署腳本
- [llm-gateway.service](llm-gateway.service) — systemd unit file
- [llm-gateway.nginx.conf](llm-gateway.nginx.conf) — nginx 設定

---

## Nginx 設定

部署腳本會自動安裝 nginx 設定。如需手動調整：

```bash
sudo nano /etc/nginx/sites-available/llm-gateway
sudo nginx -t && sudo systemctl reload nginx
```

SSL 憑證（可選）：

```bash
sudo certbot --nginx -d your-domain.com
```

---

## Proxy 設定

在 airgapped 環境下，部署前設定 proxy 環境變數：

```bash
export http_proxy=http://proxy.company.com:8080
```

`setup.sh` 會自動使用。如需 runtime proxy，取消 `llm-gateway.service` 中的 proxy 註解。
