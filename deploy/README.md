# LLM Gateway 部署指南

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) 套件管理工具
- Docker（PostgreSQL + oauth2-proxy）
- Nginx
- AuthCenter RS256 public key（`keys/public.pem`）

## 架構

```
Browser → Nginx (:80)
            ├─ /oauth2/*  → oauth2-proxy (:4180)  ← 處理登入/登出
            ├─ /v1/*      → Gateway (:8050)        ← API key 認證
            └─ /*         → auth_request → oauth2-proxy 驗證
                          → Gateway (:8050)        ← JWT header 認證
```

- **Gateway**：user-level systemd service（Python/FastAPI）
- **PostgreSQL + oauth2-proxy**：Docker Compose（`deploy/docker-compose.yml`）
- **Nginx**：system service，反向代理 + auth_request

---

## 首次部署

```bash
git clone https://github.com/bwinken/llm-gateway.git
cd llm-gateway

# 1. 設定 Docker 基礎服務
cp deploy/.env.example deploy/.env
nano deploy/.env    # 填入 OIDC_ISSUER_URL、CLIENT_SECRET、PG_PASSWORD 等

# 2. 設定應用
cp .env.example .env && nano .env
cp config.toml.example config.toml && nano config.toml

# 3. 放置 AuthCenter 公鑰
mkdir -p keys && cp /path/to/public.pem keys/public.pem

# 4. 如需 Proxy
export http_proxy=http://proxy.company.com:8080

# 5. 執行部署
bash deploy/setup.sh
```

腳本會自動：
1. rsync 程式碼到 `~/opt/llm-gateway`
2. `docker compose up` 啟動 PostgreSQL + oauth2-proxy
3. `uv sync` 安裝依賴
4. 自動建立 `.env` 並填入 DATABASE_URL
5. `alembic upgrade head` 執行資料庫遷移
6. 設定 user-level systemd service + lingering
7. 設定 nginx reverse proxy + auth_request

## 更新程式碼

```bash
cd /path/to/llm-gateway   # 原始 clone 目錄
git pull
bash deploy/setup.sh      # rsync + 更新依賴 + 重啟（Docker 服務不受影響）
```

快速更新（沒有新依賴時）：

```bash
git pull
rsync -a --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
    --exclude='config.toml' --exclude='.env' --exclude='keys/' \
    ./ ~/opt/llm-gateway/
systemctl --user restart llm-gateway
```

> `config.toml`、`.env`、`keys/`、`deploy/.env` 不會被覆蓋。

## 服務管理

```bash
# Gateway（systemd）
systemctl --user status llm-gateway     # 查看狀態
systemctl --user restart llm-gateway    # 重啟
journalctl --user -u llm-gateway -f     # 查看日誌

# PostgreSQL + oauth2-proxy（Docker Compose）
docker compose -f ~/opt/llm-gateway/deploy/docker-compose.yml logs -f
docker compose -f ~/opt/llm-gateway/deploy/docker-compose.yml restart
docker compose -f ~/opt/llm-gateway/deploy/docker-compose.yml down
```

## 目錄結構

```
~/opt/llm-gateway/
├── app/                      # 應用程式碼
├── .venv/                    # uv 管理的虛擬環境
├── config.toml               # 模型路由設定
├── .env                      # 應用環境變數
├── keys/public.pem           # AuthCenter 公鑰
└── deploy/
    ├── docker-compose.yml    # PG + oauth2-proxy
    ├── .env                  # Docker 服務環境變數
    ├── pgdata/               # PostgreSQL 資料
    ├── setup.sh              # 部署腳本
    ├── llm-gateway.service   # systemd unit
    └── llm-gateway.nginx.conf
```

## 設定檔說明

| 檔案 | 用途 |
|---|---|
| `deploy/.env` | Docker 服務設定：PG 密碼、OIDC issuer、oauth2-proxy client 等 |
| `.env` | Gateway 應用設定：DATABASE_URL、AUTH_CENTER_APP_ID |
| `config.toml` | 模型路由、定價、fallback |

## Nginx 設定

部署腳本會自動安裝。手動調整：

```bash
sudo nano /etc/nginx/sites-available/llm-gateway
sudo nginx -t && sudo systemctl reload nginx
```

SSL 憑證（可選）：

```bash
sudo certbot --nginx -d your-domain.com
```

## Proxy 設定

```bash
export http_proxy=http://proxy.company.com:8080
```

如需 Gateway runtime proxy，取消 `llm-gateway.service` 中的 proxy 註解。
