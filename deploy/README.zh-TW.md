# LLM Gateway 部署指南

[English](README.md)

## 前置需求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) 套件管理工具
- Docker（PostgreSQL + oauth2-proxy）
- rsync、openssl（`setup.sh` 會檢查）
- Nginx（範例中的 `http2 on;` 需要 nginx ≥ 1.25.1；較舊版本請改用 `listen 443 ssl http2;`）
- sudo 權限，用於啟用 lingering 與安裝 nginx 設定
- AuthCenter RS256 public key（`keys/public.pem`）

## 架構

```
Browser    → Nginx (:80)
               ├─ /oauth2/*            → oauth2-proxy (:4180)  ← 處理登入/登出
               ├─ /v1/*, = /me         → Gateway               ← API key 認證（統一分派：vLLM / Azure / Bedrock）
               ├─ /azure/*             → Gateway               ← API key 認證（Azure OpenAI 後端）
               ├─ /aws/*               → Gateway               ← API key 認證（AWS Bedrock 後端）
               ├─ /healthz, /readyz    → Gateway               ← 免認證（健康檢查）
               ├─ /static/             → 磁碟上的靜態檔案
               └─ /*                   → auth_request → oauth2-proxy 驗證
                                       → Gateway               ← JWT header 認證
API client → Nginx (:443, TLS)  只開放 /v1/、/azure/、/aws/、= /me、/healthz、/readyz；其他路徑一律 404
```

範例 nginx 設定（`deploy/llm-gateway-example.nginx.conf`）在 HTTP 與 HTTPS 兩個 server context 中都有平行的 `/v1/`、`/azure/`、`/aws/` 和 `= /me` 區塊。它們都不經 oauth2-proxy，直接保留客戶端的 `Authorization: Bearer <api_key>` 轉發給 gateway。Web UI 只走 HTTP，所以 oauth2-proxy 的 cookie 設定（`COOKIE_SECURE=false`）不需要另一套 HTTPS 版本。

- **Gateway**：user-level systemd service（Python/FastAPI，`127.0.0.1:8050`，2 個 worker）。週期性工作（用量彙總、異常掃描、選用的 usage log 保留期清理）在 gateway 內執行，不需要 timer
- **PostgreSQL + oauth2-proxy**：Docker Compose（`deploy/docker-compose.yml`，兩者都只綁 `127.0.0.1`）
- **Nginx**：system service，反向代理 + auth_request

---

## 首次部署

```bash
git clone https://github.com/bwinken/llm-gateway.git
cd llm-gateway
cp deploy/llm-gateway-example.nginx.conf deploy/llm-gateway.nginx.conf   # 你的站台副本（gitignored），見「Nginx 設定」
bash deploy/setup.sh
```

腳本會互動式引導完成所有設定。一開始先做 **pre-flight 檢查**（uv、docker、rsync、openssl、Docker daemon 是否在跑；有設定 `http_proxy` 就會套用），接著：

1. **rsync 程式碼** 到 `~/opt/llm-gateway`（排除設定檔、金鑰、各 `.env` 與 pgdata）
2. **`deploy/.env`**：提示輸入 PostgreSQL 使用者/密碼/資料庫/port、OIDC issuer URL、OAuth2 client ID/secret 與網域；自動產生 cookie secret
3. **docker compose up**：啟動 PostgreSQL + oauth2-proxy 並等待 PostgreSQL 就緒（檢查點：可在此停下）
4. **uv sync** `--frozen --no-dev`：安裝 Python 依賴
5. **應用程式設定檔**：複製 `config.toml.example` → `config.toml`、`.env.example` → `.env`，並從 `deploy/.env` 自動填入 `DATABASE_URL` 與 `AUTH_BASE_URL`
6. **AuthCenter 公鑰**：現在貼上 PEM 內容，或稍後手動放置
7. **alembic upgrade head**：遷移失敗時腳本會停止且**不會**重啟 gateway，線上維持舊版運行（檢查點）
8. **systemd**：安裝並（重新）啟動 `llm-gateway.service`、停用並移除舊版留下的 scan / cleanup timer（這些工作改由 gateway 執行）、啟用 lingering（檢查點）
9. **Nginx**：把 `deploy/llm-gateway.nginx.conf` 套用到 `/etc/nginx/sites-available/llm-gateway`，再 `nginx -t` 並 reload（未安裝 nginx，或該檔不存在、內容為空時略過）

最後顯示**部署狀態摘要**：各服務狀態與待辦事項。

> 已存在的 `deploy/.env`、`config.toml`、`.env`、`keys/public.pem` 會原樣保留、不會被覆蓋，所以在檢查點停下後再重新執行腳本是安全的。

如需設定 proxy：

```bash
export http_proxy=http://proxy.example.com:8080
bash deploy/setup.sh
```

## 更新程式碼

```bash
cd /path/to/llm-gateway   # 原始 clone 目錄
git pull
bash deploy/setup.sh      # 保留既有設定；更新程式碼 + 依賴、遷移、重啟
```

快速更新（沒有新依賴時）：

```bash
git pull
rsync -a --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
    --exclude='config.toml' --exclude='.env' --exclude='keys/' \
    ./ ~/opt/llm-gateway/
(cd ~/opt/llm-gateway && uv run alembic upgrade head)   # 重啟前一定要先遷移
systemctl --user restart llm-gateway
```

> `config.toml`、`.env`、`keys/`、`deploy/.env` 不會被覆蓋。若 `deploy/` 下的 systemd unit 或 timer 有變動，請改跑 `setup.sh`，它會重新安裝。

## 服務管理

```bash
# Gateway（systemd）
systemctl --user status llm-gateway     # 查看狀態
systemctl --user restart llm-gateway    # 重啟
journalctl --user -u llm-gateway -f     # 查看服務輸出（應用程式日誌：~/opt/llm-gateway/logs/）

# PostgreSQL + oauth2-proxy（Docker Compose）
docker compose -f ~/opt/llm-gateway/deploy/docker-compose.yml logs -f
docker compose -f ~/opt/llm-gateway/deploy/docker-compose.yml restart
docker compose -f ~/opt/llm-gateway/deploy/docker-compose.yml down
```

## 排程任務與腳本

週期性工作由 gateway 自己執行，不需要安裝 systemd timer 或 cron。每個 worker 都會跑這些迴圈，由資料庫保證每個工作在所有 worker、所有主機之間每個週期只執行一次（`scheduled_jobs` 認領；彙總則用 advisory lock）。工作失敗會記錄 log（`Scheduled job ... failed`、`Usage rollup failed`），下個週期重試，絕不影響請求。

| 工作 | 頻率 | 內容 | 預設 | 設定（`.env`） |
|---|---|---|---|---|
| 用量彙總 | 每 10 分鐘 | 把已結束的台北日 `usage_logs` 彙總進 `usage_daily`，管理頁面、dashboard 與匯出都讀它。遷移後第一次啟動會自動回填。 | 開 | `USAGE_ROLLUP_INTERVAL_S`（`0` = 關） |
| 異常掃描 | 每小時 | 把異常使用記到 `anomaly_events`，供管理頁面檢視。只偵測，絕不封鎖任何人。 | 開 | `ANOMALY_SCAN_INTERVAL_S`（`0` = 關） |
| usage log 保留期 | 每天 | **刪除** N 天前的 `usage_logs`，只刪整天，且絕不刪還沒彙總進 `usage_daily` 的日子（報表仍保有） | **關** | `USAGE_LOG_RETENTION_DAYS`（不設 = 永久保留） |

`usage_logs` 是計費與稽核的底帳，PostgreSQL 存放多年資料也毫無壓力，所以保留期清理刻意預設關閉：要不要刪除是治理決定。刪除使用者時也會保留其用量紀錄。`scheduled_jobs` 資料表記錄每個工作最近一次的開始、結束時間與結果。下列腳本執行相同的程式，不加 `--execute` 都只是 dry run：

```bash
cd ~/opt/llm-gateway
uv run python scripts/rollup_usage_daily.py              # 彙總進度與待處理日期
uv run python scripts/cleanup_usage_logs.py              # 保留期預覽：截止點與筆數
uv run python scripts/cleanup_usage_logs.py --days 180   # 自訂保留天數
uv run python scripts/scan_anomalies.py                  # 只列出結果
```

資料修正後用 `scripts/rollup_usage_daily.py --recompute FROM TO --execute` 重算指定日期。詳見 [docs/architecture.md](../docs/architecture.md#daily-usage-rollup-appservicesusage_rolluppy)。

從仍有舊 `llm-gateway-scan` / `llm-gateway-cleanup` timer 的版本升級：重新執行 `setup.sh` 會移除它們，或手動執行：

```bash
systemctl --user disable --now llm-gateway-scan.timer llm-gateway-cleanup.timer
```

其他腳本，需要時在 `~/opt/llm-gateway` 手動執行：`scripts/migrate_sqlite_to_pg.py`（一次性的 SQLite → PostgreSQL 資料遷移，`--dry-run` / `--sync`）與 `scripts/azure_latency_probe.py`（把 Azure 延遲拆成各階段，比較直連與經過 gateway；見 `--help`）。

## 健康檢查

Gateway 自身提供兩個免認證端點：

| 路徑 | 意義 | 狀態碼 |
|---|---|---|
| `/healthz` | Liveness — process 還活著、event loop 還在轉。不做任何 I/O。 | 永遠 `200` |
| `/readyz` | Readiness — 資料庫能回應 `SELECT 1`。同時附上 vLLM 健康快取的統計供人閱讀。 | `200` / `503` |

```bash
curl -s localhost:8050/healthz
# {"status":"ok","app":"LLM Gateway"}

curl -s localhost:8050/readyz
# {"status":"ok","database":"ok","downstreams":{"alive":3,"total":4}}
```

`/readyz` 刻意**不會**因為下游 vLLM 全掛而失敗。健康快取是每個 worker 各自持有、開機後約 30 秒內還是空的（剛啟動的 worker 會把自己判成 not ready）；而且下游全域故障時，這樣會一次把所有 Gateway 實例踢出負載平衡 —— 把「降級」（Azure/Bedrock 仍可路由、client 至少拿得到真正的錯誤訊息）變成「全斷」。

範例 nginx 設定已讓這兩個路徑不經 `auth_request`，外部監控可以直接輪詢；本機的探針則建議直接打 `127.0.0.1:8050`，不必繞 nginx。

## Prometheus 指標（選用）

預設關閉，在 `.env` 設 `METRICS_ENABLED=true` 並重啟（`systemctl --user restart llm-gateway`）後啟用：`GET /metrics` 提供請求數、token、花費、延遲 / TTFT、429 拒絕、進行中請求與地端伺服器健康，自動加總所有 worker。可選 `METRICS_DIR`（預設 `<tmp>/llm-gateway-metrics`）存放各 worker 的檔案。

請在主機上直接抓，不經 nginx（範例設定對 `/metrics` 回 404）：

```yaml
scrape_configs:
  - job_name: llm-gateway
    static_configs:
      - targets: ["127.0.0.1:8050"]
```

## 日誌隱私

有兩個設定決定使用者的 prompt 會不會落到永久儲存：

| 設定 | 預設 | 開啟時的效果 |
|---|---|---|
| `LANGFUSE_CAPTURE_IO` | 關 | 每個請求的 prompt / 回應內容都會送到 Langfuse |
| `LOG_REQUEST_BODIES` | 關 | Azure / Bedrock 回 4xx/5xx 時，原始請求內容會寫進 `LOG_DIR` |

兩者都關閉時，失敗的請求只會記錄請求的**結構** —— 有哪些欄位、訊息的 role 順序、content block 型別、各字串長度 —— 這些足以診斷下游的 400，但不包含內容本身。要追特定問題時再開 `LOG_REQUEST_BODIES`，追完關掉；它寫下的東西會保留 `LOG_RETENTION`（預設 14 天），且任何有主機存取權的人都讀得到。

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
├── scripts/                  # 異常掃描、usage log 清理、一次性工具
├── .venv/                    # uv 管理的虛擬環境
├── config.toml               # 模型路由設定
├── .env                      # 應用環境變數
├── keys/public.pem           # AuthCenter 公鑰
├── logs/                     # 應用程式日誌，每個 worker 一個檔（LOG_DIR）
└── deploy/
    ├── docker-compose.yml    # PG + oauth2-proxy
    ├── .env                  # Docker 服務環境變數（範本：.env.example）
    ├── pgdata/               # PostgreSQL 資料
    ├── setup.sh              # 部署腳本
    ├── gen-selfsigned-cert.sh               # 產生 :443 用的自簽 TLS 憑證
    ├── llm-gateway.service                  # Gateway systemd unit
    ├── llm-gateway-example.nginx.conf       # Nginx 範本
    └── llm-gateway.nginx.conf               # 你的站台副本（gitignored）
```

## 設定檔說明

| 檔案 | 用途 |
|---|---|
| `deploy/.env` | Docker 服務設定：PG 帳密/port/`PGDATA_DIR`、OIDC issuer、oauth2-proxy client、redirect URL、cookie |
| `.env` | Gateway 應用環境變數，見下方 |
| `config.toml` | 模型路由（vLLM、Azure、Bedrock）、定價、fallback、`[app]` 執行期設定 |

`.env` 由 systemd unit 讀入（`EnvironmentFile`），修改後要重啟 gateway。[`.env.example`](../.env.example) 說明了每個變數，根目錄 [README](../README.zh-TW.md) 的環境變數表列有預設值。摘要如下：

- **核心**：`DATABASE_URL`、`APP_TITLE`，以及 Web UI 的 JWT 設定 `AUTH_BASE_URL`、`AUTH_CENTER_APP_ID`、`AUTH_CENTER_PUBLIC_KEY_PATH`
- **雲端網路**：`AZURE_HTTP_PROXY` / `BEDROCK_HTTP_PROXY` 與 `AZURE_INSECURE` / `BEDROCK_INSECURE`（見「Proxy 設定」）
- **Bedrock 共用憑證**：`BEDROCK_REGION`、`BEDROCK_AWS_ACCESS_KEY_ID`、`BEDROCK_AWS_SECRET_ACCESS_KEY`，供沒有自行設定的 `[bedrock_models.*]` 使用。放在這裡是為了讓 secret 永遠不會進到 `config.toml` 或管理頁面。
- **預算**：`ENFORCE_DAILY_LIMIT=false` 是整個 gateway 的寬鬆模式（超支只記 log、不擋請求）。沒有單一使用者的「無上限」：使用者額度設 `0` 就是 $0 預算。
- **日誌**：`LOG_DIR`、`LOG_LEVEL`、`LOG_ROTATION`、`LOG_RETENTION`、`SLOW_REQUEST_WARN_S`、`LOG_REQUEST_BODIES`（見「日誌隱私」）
- **Langfuse**（選用）：`LANGFUSE_HOST` + `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY`（三者都設才會啟用）、`LANGFUSE_SAMPLE_RATE`、`LANGFUSE_CAPTURE_IO`
- **連線池**：`DOWNSTREAM_MAX_CONNECTIONS`（預設 1000；每條進行中的串流佔用一條連線）

## Nginx 設定

`setup.sh` 以 **`deploy/llm-gateway.nginx.conf`**（你從 `deploy/llm-gateway-example.nginx.conf` 複製的副本）產生站台設定，並替換 `__APP_DIR__` 與 `llm-gateway.your-domain.com`。請在 step 9 之前建立這份副本，並把修改都留在這裡：每次執行到 step 9 都會重寫 `/etc/nginx/sites-available/llm-gateway`。

範例的 `:443` 區塊指向 Let's Encrypt 路徑（`/etc/letsencrypt/live/<domain>/`），在這些檔案存在之前 `nginx -t` 會失敗。安裝前請擇一：取得憑證、執行 `bash deploy/gen-selfsigned-cert.sh`（離線環境適用；預設寫到 `/etc/nginx/ssl/llm-gateway.{crt,key}`，並列出要修改的設定），或刪掉 `:443` 區塊。

手動調整已安裝的站台設定：

```bash
sudo nano /etc/nginx/sites-available/llm-gateway
sudo nginx -t && sudo systemctl reload nginx
```

Let's Encrypt 憑證（可選）：

```bash
sudo certbot --nginx -d your-domain.com
```

## Proxy 設定

`setup.sh` 不會詢問 proxy。它會把 shell 中的 `http_proxy` 匯出給它執行的指令（例如 `uv sync`），並設定 `no_proxy=localhost,127.0.0.1,*.company.local`；Docker daemon 拉取 image 需要另外設定它自己的 proxy：

```bash
export http_proxy=http://proxy.example.com:8080
```

`llm-gateway.service` 中被註解的 `HTTP_PROXY` / `HTTPS_PROXY` **不會**影響 gateway 的下游流量：它的 httpx client 都以明確指定的 transport 建立，因此 httpx 會忽略 proxy 環境變數。請改用下方各雲端專用的設定。

### 僅雲端的 HTTP proxy（Azure、Bedrock）

若 Azure OpenAI 或 AWS Bedrock 只能透過企業 HTTP proxy 連線，而內部 vLLM 下游必須直連，請在 Gateway 的 `.env` 設定 `AZURE_HTTP_PROXY` 和/或 `BEDROCK_HTTP_PROXY`。只有該雲端的下游呼叫（來自 `/azure/v1/*`、`/aws/v1/*`，以及路由到該雲端的 `/v1/*` 別名）會走此 proxy；vLLM 流量永遠直連。支援內嵌帳密（`AZURE_HTTP_PROXY=http://user:pass@proxy.company.local:8080`）。若 TLS 檢查 proxy 以主機不信任的 CA 重新簽署雲端憑證，可設 `AZURE_INSECURE=true` / `BEDROCK_INSECURE=true`，只關閉該雲端 client 的憑證驗證。
