# 搬遷：以容器執行 gateway，搬到新主機

[English](README.md)

這個資料夾收著「把 gateway 改成容器執行、搬到新主機」需要的所有東西：映像、compose 檔、搬資料的腳本，以及一套可以先在別台機器走一遍的演練。

做法是**在新主機把容器全部開好，資料最後才搬**。舊主機在切換那一刻之前照常服務。

## 不會影響現有服務

現有主機（gateway 用 systemd 執行）用到的檔案都沒有改：

- `deploy/docker-compose.yml`、`deploy/.env.example`、`.env.example` 和原本完全相同。
- gateway 容器定義在這裡的 `docker-compose.gateway.yml`，只有 `compose.sh` 會載入它。現有主機上照舊的 `docker compose -f deploy/docker-compose.yml ...` 看不到它。
- 整個流程裡，會在現有主機上執行的只有 `1-bundle.sh`：它只讀取，外加一份線上備份，不停機。

這個資料夾以外有兩處相關的修改，預設行為都不變：

- `CONFIG_DIR`（程式）：指定 `config.toml` 所在的目錄（admin 存過 *Client setup* 卡片後，`client_settings.json` 也在這裡）。沒設時和以前一樣在專案根目錄。
- `deploy/backup-db.sh`：設了公鑰才會加密備份。沒設時和以前一樣（見 [deploy/README](../README.zh-TW.md#加密)）。

## 檔案

| 檔案 | 用途 |
|---|---|
| `Dockerfile`、`Dockerfile.dockerignore` | gateway 映像，以及哪些東西不進 build context（密鑰、資料、測試） |
| `docker-compose.gateway.yml` | `migrate`（跑 alembic）和 `gateway` 兩個服務，疊加在原本的 compose 檔上 |
| `compose.sh` | `docker compose`，自動帶上原本的 compose 檔和上面那個檔案 |
| `gateway.env.example` | 以容器執行時要加進 `deploy/.env` 的設定 |
| `wait_for_db.py` | `migrate` 用：等資料庫就緒（會被複製進映像） |
| `load-backup.sh` | 搬資料：把備份載入這台主機的資料庫、跑 migration、啟動 gateway |
| `1-bundle.sh` ~ `7-teardown.sh`、`lib.sh`、`drill.env.example` | 演練 |

## 以容器執行 gateway

**一律透過 `compose.sh`。** 它等於 `docker compose -f deploy/docker-compose.yml -f deploy/migration/docker-compose.gateway.yml`，用法和 `docker compose` 相同：

```bash
bash deploy/migration/compose.sh up -d
```

```bash
bash deploy/migration/compose.sh logs -f gateway
```

以容器執行 gateway 的主機上，不要再用只帶原本檔案的指令：`docker compose -f deploy/docker-compose.yml down` 會停掉 PostgreSQL，卻留下 gateway 容器繼續跑，沒有資料庫可用。

**主機網路（host networking）。** gateway 容器使用 `network_mode: host`，看到的網路和 systemd 服務完全一樣：

- nginx 照舊連 `127.0.0.1:8050`；
- `.env` 的 `DATABASE_URL`（`localhost:5432`）和 proxy 設定不用改；
- 請求的來源是 `127.0.0.1`，也就是 uvicorn 唯一信任 `X-Forwarded-For` 的位址。若改用 bridge 網路，所有請求都會看起來來自 Docker 的閘道位址，IP allowlist、登入失敗鎖定（一個壞 client 會鎖住所有人）、audit log 的 client 欄位和 `CC002` 明文 http 檢查都會出錯。

容器只綁 `127.0.0.1`。它沿用主機的 hostname，所以前一個容器留下的並發 lease 會在啟動時清掉。

**留在主機上的東西**（在安裝目錄底下，掛進容器）：

| 主機路徑 | 容器內 | 說明 |
|---|---|---|
| `config/config.toml` | `/app/config`（`CONFIG_DIR`） | admin 頁面用暫存檔加 rename 改寫它，所以掛的是**目錄**；只掛單一檔案會讓 admin 每次存檔都失敗 |
| `keys/public.pem` | `/app/keys`（唯讀） | |
| `logs/` | `/app/logs`（`LOG_DIR`） | `docker logs llm-gateway` 也有同樣的內容 |
| `.env` | 環境變數 | compose 會用容器內路徑覆蓋 `CONFIG_DIR`、`LOG_DIR`、`AUTH_CENTER_PUBLIC_KEY_PATH`、`METRICS_DIR`，所以 `.env` 裡寫主機路徑也沒關係 |

容器以 `GATEWAY_UID:GATEWAY_GID`（`deploy/.env`，預設 `1000:1000`）執行，這個使用者必須擁有 `config/` 和 `logs/`。第一次啟動前先建立這兩個目錄：目錄不存在時 Docker 會以 root 身分建立，之後 admin 存檔就會失敗。

每次 `up` 都會先跑 `migrate`（等資料庫就緒，再 `alembic upgrade head`），成功後 `gateway` 才啟動。健康檢查打的是 `/readyz`。

**建置映像**（在 repo 根目錄執行）：

```bash
docker build -f deploy/migration/Dockerfile -t llm-gateway:2026.11.1 .
```

- 映像只裝 `uv.lock` 鎖定的版本，以非 root 使用者執行，裡面沒有任何密鑰。
- 要經過 proxy 時加 `--build-arg HTTPS_PROXY=...`。基底映像可以改用公司內部的 mirror：`--build-arg PYTHON_IMAGE=... --build-arg UV_IMAGE=...`。
- 主機不能連外時，在別台機器建好再帶過去：

```bash
docker save llm-gateway:2026.11.1 | gzip > llm-gateway-2026.11.1.tar.gz
```

```bash
docker load < llm-gateway-2026.11.1.tar.gz
```

## 搬到新主機（正式步驟）

請先用下面的[演練](#演練)走一遍：它跑的是同樣的步驟，並量出時間。

### 準備新主機（不停機）

1. 把程式和映像放上去：同一版的 checkout，然後 `docker build` 或 `docker load`。
2. 建立 `config/config.toml`、`keys/public.pem`、`.env`、`deploy/.env`：
   - `deploy/.env` 從 `deploy/.env.example` 複製，設新的 `PG_PASSWORD`，再把 `gateway.env.example` 的內容加進去（`GATEWAY_IMAGE`、`GATEWAY_UID` / `GATEWAY_GID`）；
   - `.env` 的 `DATABASE_URL` 用新密碼。
3. 在空的資料庫上把全部服務開起來：

```bash
bash deploy/migration/compose.sh up -d
```

4. 設定 nginx 和憑證。網域有變的話，到 AuthCenter 登記新的 callback URL。
5. 確認：`/readyz`、登入頁，以及**從新主機的位址**連得到 vLLM、Azure、Bedrock、Exchange、Langfuse。對方的防火牆和白名單常常只寫了舊主機。

切換之前，新主機的資料庫是空的。**舊主機還在服務時，不要把正式資料載入新主機來測試：** 新主機的排程工作會開始處理這些資料，client-rule 的信會從兩邊各寄一次。要用真實資料測試，請用演練（它會拿掉寄信設定）。

### 切換（從第 1 步開始停機）

```bash
# 舊主機
systemctl --user stop llm-gateway            # 1. 從這裡開始請求失敗；不再有新的寫入
bash deploy/backup-db.sh                     # 2. 最後一次備份
# 把最新的備份檔和 config.toml（admin 可能改過）複製到新主機；
# 同一個目錄裡若有 client_settings.json 也一起複製（沒有的話會提供內建的範例）

# 新主機
BACKUP_GPG_KEY=/media/usb/llm-gateway-backup.key.asc \
    bash deploy/migration/load-backup.sh ~/llm-gateway-<時間>.dump.gpg     # 3. 取代空的資料庫、migration、啟動
```

`load-backup.sh` 會先列出現有資料庫的內容，要你輸入資料庫名稱才會取代；現有資料庫不是空的時，會先另存到 `<BACKUP_DIR>/before-load/`。接著它停掉 gateway 容器、重建資料庫、還原、清掉舊主機留下的並發 lease、跑 migration、啟動 gateway，並印出每一段花的時間。還原或 migration 失敗時，gateway 不會被啟動。主機上有執行中的 `llm-gateway` systemd 服務時，它拒絕執行。

4. 確認新主機：腳本印出的筆數、登入一次、用既有的 key 打一個請求。
5. 把流量切到新主機（DNS、負載平衡，或 nginx 的 upstream）。

### 回退

- **流量還沒切：** 在舊主機 `systemctl --user start llm-gateway`。它的資料完全沒被動過。
- **流量已經切了：** 把流量切回去並啟動舊的 gateway。這段期間記在新主機的用量不會在舊資料庫裡。

舊主機的資料庫請保留到新主機穩定運作一段時間。

### 切換之後

- 把備份的 cron（`deploy/backup-db.sh`）移到新主機，並在那裡跑一次 `deploy/restore-drill.sh`。
- 刪除 `before-load/` 裡的另存檔（沒有加密）。
- 舊主機除役時，刪除它的 `pgdata` 和備份：裡面有所有 API key。

### 新主機的日常操作

| 操作 | 結果 |
|---|---|
| `compose.sh up -d` | 先跑 `migrate`，再啟動或換掉 `gateway`；PostgreSQL 和 oauth2-proxy 的設定沒變就不會動 |
| `compose.sh down` | 全部停止並移除；進行中的串流最多有 300 秒可以跑完 |
| `compose.sh restart` | 重啟執行中的容器；不會重跑 `migrate` |
| 主機重開機 | Docker 會把 PostgreSQL、oauth2-proxy 和 gateway 帶回來（`restart: unless-stopped`），不會重跑 `migrate`。gateway 比 PostgreSQL 早起來時，它的 worker 會一直重試到資料庫有回應 |

**更新：** 建一個新的映像 tag，改 `deploy/.env` 的 `GATEWAY_IMAGE`，跑 `deploy/backup-db.sh`，再 `bash deploy/migration/compose.sh up -d`。

手動執行的腳本在容器內跑：`bash deploy/migration/compose.sh exec gateway python scripts/rollup_usage_daily.py`。

新主機上不要跑 `deploy/setup.sh`：它會安裝並啟動 systemd 的 gateway，和容器搶 8050 port。

## 演練

在**另一台機器**上，用正式環境的一份備份，把上面的流程完整走一遍：備份、還原測試、建映像、開容器、載入資料、驗證、清除。正式環境全程只被讀取，不停機。

演練回答三個問題：備份真的還原得回來嗎？容器化的 gateway 用正式的資料和設定跑得起來嗎？正式切換要停機多久？每一步的結果都記在 `drill.log`，可以當作稽核紀錄（ISO 27001 A.8.13 備份測試、A.8.32 變更管理）。

### 兩台機器

| | 正式主機 | 演練機器 |
|---|---|---|
| 目錄 | `~/opt/llm-gateway`（現有的安裝） | 一份乾淨的 checkout，例如 `~/llm-gateway-drill` |
| 執行 | 只有 `1-bundle.sh` | `2` ~ `7` |
| 會被改動的東西 | 多一份備份與演練包 | 演練環境（`7-teardown.sh` 全部刪除） |

演練機器需要：

- Docker（含 compose）、`gpg`、`curl`；
- 和正式環境相同主版本的 PostgreSQL 映像（預設 `postgres:16`）；
- 這個 repo 的 checkout，**版本要是你打算部署的那一版**；
- 解密備份用的私鑰。

演練機器**不能是正式主機**：`2` ~ `7` 偵測到下面任何一項就拒絕執行。

- 執行中的 `llm-gateway` systemd 服務；
- 不是演練建立的 `llm-gateway-pg` / `llm-gateway` 容器；
- 既有的 `.env`、`config.toml` 或 `deploy/pgdata`。

### 步驟

| # | 在哪裡 | 指令 | 做什麼 | 通過的標準 |
|---|---|---|---|---|
| 1 | 正式主機 | `bash deploy/migration/1-bundle.sh` | 線上備份資料庫；打包 `config.toml`、公鑰、清理過的 `.env`；記下快照 | 產生加密的演練包 |
| 2 | 演練機器 | `bash deploy/migration/2-restore-test.sh` | 核對檔案雜湊；把備份還原到一次性容器並檢查 | 還原成功，筆數與觸發器都在 |
| 3 | 演練機器 | `bash deploy/migration/3-image.sh` | 建 gateway 映像（或載入別處建好的） | 映像能載入程式、列得出 migration |
| 4 | 演練機器 | `bash deploy/migration/4-up.sh` | **先把容器開好**：解開設定 → 啟動 PostgreSQL → 在空資料庫上建 schema → 啟動 gateway 容器 | gateway 就緒，資料庫是空的 |
| 5 | 演練機器 | `bash deploy/migration/5-load-data.sh` | **資料最後搬**：呼叫 `load-backup.sh` | gateway 帶著正式資料就緒 |
| 6 | 演練機器 | `bash deploy/migration/6-verify.sh` | 逐項驗證，和快照對照；估計停機時間 | 全部 ✔ |
| 7 | 演練機器 | `bash deploy/migration/7-teardown.sh` | 刪除演練環境裡的所有資料 | 沒有任何東西留下 |

任何一步失敗就停在那裡，不會自動往下。修正後重跑該步即可；`5` 可以重複執行，`4` 要重來時先跑 `7`。

第 4、5 步用的是和正式搬遷相同的 compose 檔與 `load-backup.sh`（compose project 名稱 `llmgw-drill`），所以演練到的就是正式要跑的指令。

### 1. 在正式主機打包

```bash
BACKUP_GPG_RECIPIENT_FILE=~/opt/llm-gateway/keys/backup.pub.asc bash deploy/migration/1-bundle.sh
```

產出 `~/backup/llm-gateway/migration-bundle-<時間>/`：

| 檔案 | 內容 |
|---|---|
| `llm-gateway-<時間>.dump.gpg` | 資料庫備份（`deploy/backup-db.sh` 產生，同時也是一份正常的每日備份） |
| `state.tar.gz.gpg` | `config.toml`、`keys/public.pem`、清理過的 `.env` |
| `snapshot.env` | 備份當下的快照：各表筆數、schema 版本、PostgreSQL 版本、`config.toml` 的雜湊。沒有密鑰 |
| `SHA256SUMS` | 讓演練機器確認檔案沒有在搬運途中損毀 |

**`.env` 會先清理**，這些設定不會離開正式主機：

- `EXCHANGE_*`：有了它，演練環境會**真的寄信給使用者**；
- `LANGFUSE_*`：演練的請求會混進正式的觀測資料；
- `USAGE_LOG_RETENTION_DAYS`：保留期工作會刪掉演練資料庫的資料；
- `DATABASE_URL` 與主機路徑：演練環境自己產生。

演練包裡有所有 API key 與下游密鑰，所以**一定要加密**（和備份用同一把公鑰）；沒設公鑰時腳本會拒絕執行。中途失敗的話，做到一半的目錄會被刪掉。

把整個目錄帶到演練機器：

```bash
rsync -a ~/backup/llm-gateway/migration-bundle-<時間> user@drill-host:~/
```

### 2 ~ 6. 在演練機器執行

設定寫在 `drill.env`（從 `drill.env.example` 複製），或直接用環境變數；指令列上給的優先。至少要有 `BUNDLE`（演練包目錄）和 `DRILL_GPG_KEY`（私鑰檔）。

```bash
bash deploy/migration/2-restore-test.sh
```

```bash
bash deploy/migration/3-image.sh
```

```bash
bash deploy/migration/4-up.sh
```

```bash
bash deploy/migration/5-load-data.sh
```

```bash
bash deploy/migration/6-verify.sh
```

- **私鑰**只在執行期間匯入暫時的目錄，結束就刪除，不會留在演練機器的 keyring。有密碼時 gpg 會在終端機詢問。
- **演練機器不能連外**時，把別處 `docker save` 的檔案帶過來，設 `IMAGE_TAR=<檔案>` 跑第 3 步。
- **演練通過的映像就是正式搬遷要用的映像**：用同一個 tag（或同一個 `docker save` 檔），不要重建。
- 第 4 步之後、第 5 步之前，gateway 在空的資料庫上執行：這就是正式搬遷時「新主機準備好、等著切換」的狀態。
- `5432` / `8050` 在演練機器上已被占用時，設 `PG_PORT` / `GATEWAY_PORT`。

### 第 6 步驗證哪些事

| 類別 | 檢查 |
|---|---|
| 容器 | 執行中、`/readyz`、`/healthz`、主機網路、非 root、只聽 `127.0.0.1` |
| 資料 | schema 是映像的最新版本；每張表還原的筆數不少於快照；沒有舊主機留下的並發 lease；`audit_log` 防竄改觸發器還在 |
| 設定 | `config.toml` 和正式環境的雜湊相同；gateway 讀的是掛載的那份（不是內建範例）；config 目錄可寫入並 rename；log 寫到主機；公鑰讀得到 |
| 不影響外面 | 容器裡沒有寄信 / Langfuse / 保留期設定；寄信功能關閉 |
| API | 臨時帳號的 key 驗證通過；`/v1/models` 有模型；錯誤的 key 回 401；gateway 看到的是 `X-Forwarded-For` 的使用者位址；Web UI 未登入回 401 |
| 模型（選用） | `DRILL_TEST_MODEL=<別名>`：送一個 `max_tokens=8` 的請求，確認回 200 並記進 `usage_logs` |

筆數用「不少於」比對，是因為快照在備份開始前一刻拍下，而正式環境同時還在收請求。`DRILL_TEST_MODEL` 預設不設：演練機器不一定連得到 vLLM。

**演練沒有涵蓋的**，正式切換後要另外確認：nginx 和 oauth2-proxy 的登入流程、Azure / Bedrock 模型（會產生費用）、實際負載下的表現。

### 停機時間怎麼讀

第 6 步最後會印出「備份 + 載入資料」的秒數。「載入資料」是第 5 步從停掉 gateway 到重新就緒的實測時間，包含還原、migration 和啟動。另外要加上把備份檔複製到新主機、切換流量的時間。這是用正式資料量測出來的，可以直接寫進變更申請。

### 7. 清除

```bash
bash deploy/migration/7-teardown.sh
```

演練環境裡是一份完整的正式資料（所有 API key），**演練完一定要跑**。它會刪除容器、volume、還原出來的資料庫、`config/`、`keys/`、`logs/`、`.env`、`deploy/.env`，並確認沒有東西留下。加 `--image` 連映像一起刪。

留下來的只有紀錄（`drill.log`、`restore-drill.log`），裡面沒有資料或密鑰。**演練包和私鑰檔不在這個目錄裡，要另外刪除。**

### 紀錄

`drill.log` 一行一個步驟（成功或失敗都記），欄位以 tab 分隔：

```
2026-11-11 16:26:22  step=4-up         result=OK  elapsed=10s  by=alice@drill-host  bundle=migration-bundle-2026-11-11_162514 empty_migrate=2s start=5s ...
2026-11-11 16:26:33  step=5-load-data  result=OK  elapsed=10s  by=alice@drill-host  restore=1s migrate=2s start=5s total=10s schema=07899844c700->c1a7e4b9d2f6 users=200
2026-11-11 16:26:36  step=6-verify     result=OK  elapsed=4s   by=alice@drill-host  passed=30 failed=0 downtime_move=11s ...
```

正式主機上的 `1-bundle.sh` 也會在那邊的 `drill.log` 留一行。建議連同下面這張表一起存檔：

| 項目 | 內容 |
|---|---|
| 日期 / 執行者 | |
| 演練的程式版本（commit） | |
| 映像（tag 與 `3-image.sh` 記下的 id） | |
| 演練包（目錄名稱） | |
| schema：備份時 → migration 後 | |
| 第 6 步結果（通過 / 失敗項目） | |
| 估計停機時間 | |
| 發現的問題與處理 | |
| 第 7 步已執行、演練包與私鑰已刪除 | |

## 替代方案：在原主機切換

資料庫留在原地，只把 gateway 從 systemd 換成容器。**這會動到現有服務**，除非有理由不搬主機，否則建議走上面的新主機流程。

```bash
cd ~/opt/llm-gateway
bash deploy/backup-db.sh                                   # 1. 先備份
mkdir -p config && mv config.toml config/                  # 2. config 移進自己的目錄
echo "CONFIG_DIR=$HOME/opt/llm-gateway/config" >> .env     #    systemd 也從這裡讀（回退時用）
cat deploy/migration/gateway.env.example >> deploy/.env    # 3. 再把 GATEWAY_UID / GATEWAY_GID 改成 id -u / id -g
bash deploy/migration/compose.sh build gateway             # 4. 建映像（或 docker load）
systemctl --user disable --now llm-gateway                 # 5. 停掉 systemd：兩者都用 :8050
bash deploy/migration/compose.sh up -d                     # 6. PostgreSQL 和 oauth2-proxy 不會被重建
```

**回退：** `bash deploy/migration/compose.sh rm -sf gateway migrate`，再 `systemctl --user enable --now llm-gateway`。兩者讀同一份 `config/config.toml`。已經跑過的 migration 會保留；只要回退到的程式版本不早於那個 migration 就沒問題。

## 常見問題

| 訊息 | 原因與處理 |
|---|---|
| `看起來是正式主機，拒絕執行` | 這台有正式的容器、systemd 服務或設定檔。演練請換一台機器，或用一份乾淨的 checkout |
| `演練包的檔案和 SHA256SUMS 不符` | 複製途中損毀或被改過，從正式主機重新複製 |
| `無法解開 state.tar.gz.gpg` / `無法解密` | 沒有私鑰：設 `DRILL_GPG_KEY`（`load-backup.sh` 是 `BACKUP_GPG_KEY`） |
| `PostgreSQL 主版本不同` | 演練要用和正式相同主版本的映像 |
| `127.0.0.1:5432 已經有服務在聽` | 設 `PG_PORT` / `GATEWAY_PORT` 換一個 |
| `migration 失敗` | 看腳本印出的 migrate 輸出。這正是演練要抓的問題：在正式搬遷前修好 |
| `演練環境已經存在` | 先跑 `7-teardown.sh` |
| `資料還沒搬進來` | 先跑 `5-load-data.sh` |
| `需要確認：在終端機執行，或加 --yes` | `load-backup.sh` 要你輸入資料庫名稱；在腳本裡用時加 `--yes` |
