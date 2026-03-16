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
bash deploy/setup.sh
```

腳本會互動式引導完成所有設定：

1. **Pre-flight 檢查** — 確認 uv、docker、rsync、openssl 等工具已安裝
2. **PostgreSQL 設定** — 提示輸入密碼（或自動產生）
3. **OIDC / oauth2-proxy 設定** — 提示輸入 issuer URL、client ID/secret、domain
4. **AuthCenter 公鑰** — 可選擇貼上 PEM 內容或指定檔案路徑
5. **rsync 程式碼** 到 `~/opt/llm-gateway`
6. **docker compose up** 啟動 PostgreSQL + oauth2-proxy
7. **uv sync** 安裝 Python 依賴
8. **自動建立 `.env`** 並填入 DATABASE_URL
9. **alembic upgrade head** 執行資料庫遷移
10. **設定 user-level systemd** service + lingering
11. **設定 nginx** reverse proxy + auth_request
12. **部署狀態摘要** — 顯示各服務健康狀態與待辦事項

> 重複執行時，腳本會偵測已存在的設定檔並詢問是否覆蓋，不會強制重設。

如需設定 proxy：

```bash
export http_proxy=http://proxy.company.com:8080
bash deploy/setup.sh
```

## 更新程式碼

```bash
cd /path/to/llm-gateway   # 原始 clone 目錄
git pull
bash deploy/setup.sh      # 偵測已有設定，只更新程式碼 + 依賴 + 重啟
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

## PostgreSQL 資料遷移（更換 Volume）

當需要將 PG 資料搬移到其他 volume（例如掛錯磁碟、擴容）時，使用 `pg_dumpall` 邏輯備份最安全，不受檔案系統或權限差異影響。

```bash
# 1. 匯出整個資料庫（容器還在跑的時候執行）
docker exec llm-gateway-pg pg_dumpall -U "$PG_USER" > /tmp/pg_backup.sql

# 2. 停掉所有服務
docker compose -f deploy/docker-compose.yml down

# 3. 修改 deploy/.env，將 PGDATA_DIR 指向新 volume
#    例如: PGDATA_DIR=/mnt/new-volume/pgdata

# 4. 啟動 PG（會在新 volume 初始化空資料庫）
docker compose -f deploy/docker-compose.yml up -d postgres

# 5. 等 PG 就緒後匯入備份
docker exec -i llm-gateway-pg psql -U "$PG_USER" -d "$PG_DB" < /tmp/pg_backup.sql

# 6. 確認資料正確後，啟動其餘服務
docker compose -f deploy/docker-compose.yml up -d
```

> **注意**：執行前請確認新 volume 已掛載且空間足夠。`$PG_USER` / `$PG_DB` 對應 `deploy/.env` 中的設定值。

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

部署腳本會提示是否需要設定 proxy。如需手動設定：

```bash
export http_proxy=http://proxy.company.com:8080
```

如需 Gateway runtime proxy，取消 `llm-gateway.service` 中的 proxy 註解。
