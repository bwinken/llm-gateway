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
瀏覽器 / API client → Nginx (:80)   → 308 轉到 https://（保留 method 與 body）；/healthz、/readyz 可選擇留在 HTTP
瀏覽器 / API client → Nginx (:443, TLS)
               ├─ /oauth2/*            → oauth2-proxy (:4180)  ← 處理登入/登出
               ├─ /v1/*, = /me         → Gateway               ← API key 認證（統一分派：vLLM / Azure / Bedrock）
               ├─ /azure/*             → Gateway               ← API key 認證（Azure OpenAI 後端）
               ├─ /aws/*               → Gateway               ← API key 認證（AWS Bedrock 後端）
               ├─ /auth/*              → Gateway               ← 帶 AuthCenter token 換 API key（terminal 工具）
               ├─ /healthz, /readyz    → Gateway               ← 免認證（健康檢查）
               ├─ /static/             → 磁碟上的靜態檔案
               └─ /*                   → auth_request → oauth2-proxy 驗證
                                       → Gateway               ← JWT header 認證
```

全站都走 HTTPS。範例 nginx 設定（`deploy/llm-gateway-example.nginx.conf`）把所有 location 都放在 `:443` server；`:80` server 只做 **308** 轉址，保留原本的 method 與 body（301 會讓很多 client 把 POST 改成 GET）。API 路徑（`/v1/`、`/azure/`、`/aws/`、`= /me`）不經 oauth2-proxy，直接保留客戶端的 `Authorization: Bearer <api_key>` 轉發給 gateway。`/auth/` 也一樣不經 oauth2-proxy：terminal 工具把 AuthCenter 的 access token 送到 `POST /auth/api-key`，換回該帳號的 API key；在這個 location 出現之前就部署的 nginx 設定，要照範例補上 `location /auth/`，否則這些請求會被導到瀏覽器登入頁。登入 cookie 設為 `Secure`（`OAUTH2_PROXY_COOKIE_SECURE`，`docker-compose.yml` 預設 `true`），不會在明文 HTTP 上傳送。

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
2. **`deploy/.env`**：提示輸入 PostgreSQL 使用者/密碼/資料庫/port、OIDC issuer URL、OAuth2 client ID/secret 與網域（callback 會是 `https://<domain>/oauth2/callback`，請在 AuthCenter 的 client 允許它）；自動產生 cookie secret
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
bash deploy/backup-db.sh  # 先備份資料庫（線上執行，見「資料庫備份」）
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

## 以容器執行 gateway、搬到新主機

相關的東西都在 [`deploy/migration/`](migration/README.zh-TW.md)，有自己的 README：gateway 映像與它的 compose 檔、搬到新主機的步驟，以及演練搬遷的腳本。systemd 部署完全用不到那裡的東西：`deploy/docker-compose.yml` 沒有改，gateway 容器定義在另一個檔案，只有 `deploy/migration/compose.sh` 會載入它。

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

## 安全相關設定

- **稽核紀錄。** 每個 admin / dashboard 的變更、每次顯示完整 API key、每次匯出，都會在 `audit_log` 留一列（admin *Security* → *Audit Log*，可下載 CSV）。只能新增：PostgreSQL 的 trigger 會拒絕 UPDATE、DELETE 和 TRUNCATE，要刪資料必須由 DBA 刻意拿掉 trigger。每一筆也會寫成 `Audit | {...}` log（含使用者名稱、位址、送出的欄位；密鑰已遮蔽）。
- **驗證失敗節流。** `AUTH_FAIL_DISTINCT_KEYS` / `AUTH_FAIL_WINDOW_S` / `AUTH_LOCKOUT_S`（預設 600 秒內 10 把不同的錯誤 key → 封鎖 900 秒，回 429）。依來源位址判斷，所以 uvicorn 必須信任 nginx 轉送的 header；loopback 永不封鎖，若沒設定好，所有請求都會變成 loopback，節流也就形同關閉。
- **套件與密鑰掃描** 在 GitHub Actions 執行（`.github/workflows/security.yml`）。要讓 Dependabot 自動開安全修補 PR，需到 repo 的 Settings → Code security 打開 *Dependabot security updates*。

## 資料庫備份

`deploy/backup-db.sh` 做線上備份：`pg_dump` 在一個快照裡讀完整個資料庫，備份內容一致，gateway 不用停。它只會擋住改表結構的動作，所以不要和 `alembic upgrade` / `setup.sh` 同時跑（偵測到 `alembic upgrade` 正在執行時，腳本會拒絕執行）。

```bash
bash ~/opt/llm-gateway/deploy/backup-db.sh                  # → ~/backup/llm-gateway
bash ~/opt/llm-gateway/deploy/backup-db.sh /mnt/nas/llmgw   # 指定其他目錄
bash ~/opt/llm-gateway/deploy/backup-db.sh --check FILE     # 檢查備份檔能否讀取（.gpg 需要私鑰）
bash ~/opt/llm-gateway/deploy/restore-drill.sh              # 把最新一份備份還原到一次性容器演練
```

腳本會從 `deploy/.env` 讀 `PG_USER` / `PG_DB`，先確認剩餘空間大於資料庫大小，經由暫存檔寫出 `daily/llm-gateway-<日期>_<時間>.dump`（`pg_dump -Fc`），用 `pg_restore --list` 驗證通過才保留；每個月的第一份另存到 `monthly/`，並只保留 `KEEP_DAILY`（30）份每日、`KEEP_MONTHLY`（12）份每月備份。檔案權限 `600`、目錄 `700`：備份裡有所有人的 API key。設 `BACKUP_COPY_TO=user@host:/path/` 會把每份備份 rsync 到主機以外的地方；沒設時腳本會提醒備份只在本機。成功回傳 0；失敗時不會留下不完整的檔案。

### 加密

設定 `BACKUP_GPG_RECIPIENT_FILE` 指向一把 GnuPG **公鑰**，每份備份就會寫成 `llm-gateway-<日期>_<時間>.dump.gpg`。gateway 主機上只放公鑰，私鑰由備份負責人離線保管，所以就算磁碟、NAS 或異地備份被拿走，也讀不到內容。備份會先驗證再加密，未加密的暫存檔驗證完立刻刪除（只在 `700` 的備份目錄裡存在幾秒，和資料庫本身在同一顆磁碟上）；只有加密檔會被保留、另存到 `monthly/`、複製到 `BACKUP_COPY_TO`。公鑰無法加密（檔案錯誤、已過期）時，在開始 dump 之前就會失敗。沒設公鑰時照常備份，但會提醒備份沒有加密。

金鑰在負責人的電腦上產生一次，不要在 gateway 主機上產生：

```bash
gpg --quick-gen-key "LLM Gateway backup <dba@your-company.com>" default default never   # 設定密碼
gpg --armor --export dba@your-company.com > llm-gateway-backup.pub.asc                     # → 放到 gateway 主機
gpg --armor --export-secret-keys dba@your-company.com > llm-gateway-backup.key.asc          # → 離線保存
```

把 `.pub.asc` 複製到 gateway 主機（例如 `~/opt/llm-gateway/keys/backup.pub.asc`），再加進 cron。公鑰已經匯入該使用者的 keyring 時，也可以改用 `BACKUP_GPG_RECIPIENT=<指紋>`。私鑰遺失等於所有加密備份都無法還原：請在兩個不同地方各存一份。手動解密：`gpg --decrypt FILE.dump.gpg > FILE.dump`。

每次升版前跑一次，平常用 cron 在離峰時段每天跑。資料庫備份是主機層級的工作，不是 gateway 程式內的定期工作（那些不會碰 Docker）：

```bash
30 2 * * * BACKUP_GPG_RECIPIENT_FILE=$HOME/opt/llm-gateway/keys/backup.pub.asc BACKUP_COPY_TO=user@nas:/backup/llmgw/ bash $HOME/opt/llm-gateway/deploy/backup-db.sh >> $HOME/backup/llm-gateway/backup.log 2>&1
```

### 還原演練

`pg_restore --list` 只證明檔案讀得出來，不證明還原得回來。`deploy/restore-drill.sh` 會真的還原一次：建議每季跑一次，以及每次改動備份方式之後。它不會連線到正式資料庫。

```bash
bash ~/opt/llm-gateway/deploy/restore-drill.sh                     # <BACKUP_DIR>/daily 裡最新的一份
bash ~/opt/llm-gateway/deploy/restore-drill.sh FILE                # 指定 .dump 或 .dump.gpg
DRILL_GPG_KEY=/media/usb/llm-gateway-backup.key.asc \
    bash ~/opt/llm-gateway/deploy/restore-drill.sh                 # 加密備份：帶私鑰來
```

它用和 `llm-gateway-pg` **相同的映像**（或 `DRILL_IMAGE`）起一個沒有網路的一次性容器，解密後以 `pg_restore --exit-on-error` 還原，再檢查 schema 版本、`users` / `usage_logs` / `usage_daily` / `audit_log` 的筆數、最新一筆用量時間，以及 `audit_log` 的防竄改觸發器是否一起還原。不論成功或失敗，容器都會連同資料 volume 一起刪除。`DRILL_GPG_KEY` 會匯入一個暫時的 GnuPG 目錄，結束時刪除，私鑰不會留在主機上；沒設時用目前使用者的 keyring。gpg 會在終端機詢問私鑰密碼。

每次演練（成功或失敗）都會在 `<BACKUP_DIR>/restore-drill.log`（`DRILL_LOG`）附加一行：時間、結果、檔案與 SHA-256 前綴、執行者、還原與整體耗時、schema 版本與筆數。這個檔案就是稽核要的證據（ISO 27001 A.8.13），還原耗時也是 RTO 的實測數字。只有還原和每項檢查都通過時才回傳 0。

真的遺失資料時，還原到一個新的資料庫（加密檔先解密：`gpg --decrypt FILE.dump.gpg | ...`）：

```bash
docker exec -i llm-gateway-pg pg_restore -U "$PG_USER" --create -d postgres < FILE
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
    ├── backup-db.sh          # 資料庫線上備份（pg_dump、驗證、加密、保留期）
    ├── restore-drill.sh      # 把備份還原到一次性容器演練，留下紀錄
    ├── migration/            # gateway 容器、搬到新主機與演練（有自己的 README）
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

範例的 `:443` 區塊指向 Let's Encrypt 路徑（`/etc/letsencrypt/live/<domain>/`），在這些檔案存在之前 `nginx -t` 會失敗。所有頁面都由這個區塊服務，所以安裝前要先準備憑證：Let's Encrypt、公司 CA，或執行 `bash deploy/gen-selfsigned-cert.sh`（離線環境適用；預設寫到 `/etc/nginx/ssl/llm-gateway.{crt,key}`，並列出要修改的設定；自簽憑證瀏覽器會警告）。

手動調整已安裝的站台設定：

```bash
sudo nano /etc/nginx/sites-available/llm-gateway
sudo nginx -t && sudo systemctl reload nginx
```

Let's Encrypt 憑證（可選）：

```bash
sudo certbot --nginx -d your-domain.com
```

### 既有的 HTTP 部署改為全站 HTTPS

舊版的 Web UI 走 HTTP，只有 API 走 `:443`。既有安裝要切換時：

> **先更新程式、之後才切換？** `docker-compose.yml` 現在把 SSO cookie 設為 `Secure`，瀏覽器不會在純 HTTP 下送出它，Web UI 會登入不了。在做到第 5 步之前，跑 `setup.sh` 前先在 `deploy/.env` 加上 `OAUTH2_COOKIE_SECURE=false`，切換時再拿掉。


1. **憑證。** 現在整個站都需要瀏覽器信任的憑證：Let's Encrypt、公司 CA，或 `deploy/gen-selfsigned-cert.sh`（自簽憑證每個瀏覽器都會警告；內網建議用公司 CA）。
2. **AuthCenter。** 在 OIDC client 允許 `https://<domain>/oauth2/callback`。切換完成前先保留舊的 `http://` callback。
3. **`deploy/.env`。** 手動把 `OAUTH2_REDIRECT_URL` 改成 `https://<domain>/oauth2/callback`：`setup.sh` 不會覆蓋已存在的 `deploy/.env`。
4. **Nginx。** 把新範例合併進你的 `deploy/llm-gateway.nginx.conf`（所有 location 放在 `:443`，`:80` = 308），再執行 `setup.sh` 或 `sudo nginx -t && sudo systemctl reload nginx`。如果 `:443` 有 `allow` / `deny`，現在也會擋到 Web UI 與登入。
5. **oauth2-proxy。** 執行 `docker compose -f ~/opt/llm-gateway/deploy/docker-compose.yml up -d`，以新的 https callback 與 `Secure` cookie 重建容器。所有人需要重新登入一次：舊的 cookie 不是 `Secure`。
6. **Client。** 把所有 base URL 改成 `https://`：Claude Code 的 `ANTHROPIC_BASE_URL`、SDK 的 `base_url`、`setup/install-claude-code.bat` 的 `GATEWAY_URL`。308 只是保險：有些 client 被轉址時會丟掉 `Authorization` header 或 body。
7. **（選用）過渡期。** 範例 `:80` 裡註解掉的區塊可以讓 API 暫時繼續走 HTTP，並記在獨立的 access log，先看還有誰在用 `http://`，再把它們交給 308。
8. **之後再開 HSTS。** 全站 https 都正常後，取消 `Strict-Transport-Security` 那行的註解，先用短的 `max-age`，過一陣子再拉長。

確認：

```bash
curl -sI http://your-domain.com/dashboard | head -3      # 應為 308，Location: https://...
curl -s https://your-domain.com/v1/models -H "Authorization: Bearer <key>" | head -c 200
```

## Proxy 設定

`setup.sh` 不會詢問 proxy。它會把 shell 中的 `http_proxy` 匯出給它執行的指令（例如 `uv sync`），並設定 `no_proxy=localhost,127.0.0.1,*.company.local`；Docker daemon 拉取 image 需要另外設定它自己的 proxy：

```bash
export http_proxy=http://proxy.example.com:8080
```

`llm-gateway.service` 中被註解的 `HTTP_PROXY` / `HTTPS_PROXY` **不會**影響 gateway 的下游流量：它的 httpx client 都以明確指定的 transport 建立，因此 httpx 會忽略 proxy 環境變數。請改用下方各雲端專用的設定。

### 僅雲端的 HTTP proxy（Azure、Bedrock）

若 Azure OpenAI 或 AWS Bedrock 只能透過企業 HTTP proxy 連線，而內部 vLLM 下游必須直連，請在 Gateway 的 `.env` 設定 `AZURE_HTTP_PROXY` 和/或 `BEDROCK_HTTP_PROXY`。只有該雲端的下游呼叫（來自 `/azure/v1/*`、`/aws/v1/*`，以及路由到該雲端的 `/v1/*` 別名）會走此 proxy；vLLM 流量永遠直連。支援內嵌帳密（`AZURE_HTTP_PROXY=http://user:pass@proxy.company.local:8080`）。若 TLS 檢查 proxy 以主機不信任的 CA 重新簽署雲端憑證，可設 `AZURE_INSECURE=true` / `BEDROCK_INSECURE=true`，只關閉該雲端 client 的憑證驗證。
