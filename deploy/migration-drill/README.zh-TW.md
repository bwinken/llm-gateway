# 搬遷演練：把 gateway 改成容器執行

[English](README.md)

在**另一台機器**上，用正式環境的一份備份，把「systemd → 容器」的搬遷完整走一遍。順序和正式搬遷相同：**先把容器全部開好，資料最後才搬**。步驟是備份、還原測試、建映像、開容器、載入資料、驗證、清除。正式環境全程只被讀取（多一份線上備份），不停機。

演練回答三個問題：

1. 備份真的還原得回來嗎？
2. 容器化的 gateway 用正式的資料和設定跑得起來嗎？
3. 正式切換要停機多久？

每一步的結果都記在 `drill.log`，可以當作稽核紀錄（ISO 27001 A.8.13 備份測試、A.8.32 變更管理）。

## 兩台機器

| | 正式主機 | 演練機器 |
|---|---|---|
| 目錄 | `~/opt/llm-gateway`（現有的安裝） | 一份乾淨的 checkout，例如 `~/llm-gateway-drill` |
| 執行 | 只有 `1-bundle.sh` | `2` ~ `7` |
| 會被改動的東西 | 多一份備份與演練包 | 演練環境（`7-teardown.sh` 全部刪除） |

演練機器需要：

- Docker（含 compose）、`gpg`、`curl`。
- 和正式環境相同主版本的 PostgreSQL 映像（預設 `postgres:16`）。
- 這個 repo 的 checkout，**版本要是你打算部署的那一版**：演練跑的 migration 和映像都來自它。
- 解密備份用的私鑰。

演練機器**不能是正式主機**：`2` ~ `7` 偵測到下面任何一項就拒絕執行。

- 執行中的 `llm-gateway` systemd 服務；
- 不是演練建立的 `llm-gateway-pg` / `llm-gateway` 容器；
- 既有的 `.env`、`config.toml` 或 `deploy/pgdata`。

## 步驟

| # | 在哪裡 | 指令 | 做什麼 | 通過的標準 |
|---|---|---|---|---|
| 1 | 正式主機 | `bash deploy/migration-drill/1-bundle.sh` | 線上備份資料庫；打包 `config.toml`、公鑰、清理過的 `.env`；記下快照 | 產生加密的演練包 |
| 2 | 演練機器 | `bash deploy/migration-drill/2-restore-test.sh` | 核對檔案雜湊；把備份還原到一次性容器並檢查 | 還原成功，筆數與觸發器都在 |
| 3 | 演練機器 | `bash deploy/migration-drill/3-image.sh` | 建 gateway 映像（或載入別處建好的） | 映像能載入程式、列得出 migration |
| 4 | 演練機器 | `bash deploy/migration-drill/4-up.sh` | **先把容器開好**：解開設定 → 啟動 PostgreSQL → 在空資料庫上建 schema → 啟動 gateway 容器 | gateway 就緒，資料庫是空的 |
| 5 | 演練機器 | `bash deploy/migration-drill/5-load-data.sh` | **資料最後搬**：停 gateway → 重建資料庫 → 還原 → migration → 啟動 gateway | gateway 帶著正式資料就緒 |
| 6 | 演練機器 | `bash deploy/migration-drill/6-verify.sh` | 逐項驗證，和快照對照；估計停機時間 | 全部 ✔ |
| 7 | 演練機器 | `bash deploy/migration-drill/7-teardown.sh` | 刪除演練環境裡的所有資料 | 沒有任何東西留下 |

任何一步失敗就停在那裡，不會自動往下。修正後重跑該步即可；`5` 可以重複執行，`4` 要重來時先跑 `7`。

第 5 步呼叫的是 `deploy/load-backup.sh`，也就是正式搬遷時在新主機上跑的那一支：演練到的就是正式要跑的指令，它花的時間就是停機時間的主要部分。

### 1. 在正式主機打包

```bash
cd ~/opt/llm-gateway
BACKUP_GPG_RECIPIENT_FILE=~/opt/llm-gateway/keys/backup.pub.asc bash deploy/migration-drill/1-bundle.sh
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

演練包裡有所有 API key 與下游密鑰，所以**一定要加密**（和備份用同一把公鑰，見 [deploy/README](../README.zh-TW.md#加密)）；沒設公鑰時腳本會拒絕執行。

把整個目錄帶到演練機器：

```bash
rsync -a ~/backup/llm-gateway/migration-bundle-<時間> user@drill-host:~/
```

### 2 ~ 6. 在演練機器執行

設定寫在 `drill.env`（從 `drill.env.example` 複製），或直接用環境變數；指令列上給的優先。

```bash
cd ~/llm-gateway-drill
cp deploy/migration-drill/drill.env.example deploy/migration-drill/drill.env
# 編輯 drill.env：BUNDLE=演練包目錄、DRILL_GPG_KEY=私鑰檔
```

```bash
bash deploy/migration-drill/2-restore-test.sh
```

```bash
bash deploy/migration-drill/3-image.sh
```

```bash
bash deploy/migration-drill/4-up.sh
```

```bash
bash deploy/migration-drill/5-load-data.sh
```

```bash
bash deploy/migration-drill/6-verify.sh
```

- **私鑰**（`DRILL_GPG_KEY`）只在執行期間匯入暫時的目錄，結束就刪除，不會留在演練機器的 keyring。有密碼時 gpg 會在終端機詢問。
- **演練機器不能連外**時，在別處 `docker build` 再 `docker save`，把檔案帶過來，設 `IMAGE_TAR=<檔案>` 跑第 3 步。公司內部 mirror 用 `PYTHON_IMAGE` / `UV_IMAGE`。
- **演練通過的映像就是正式切換要用的映像**：用同一個 tag（或同一個 `docker save` 檔）去正式主機，不要重建。
- 第 4、5 步用的是和正式環境同一份 `deploy/docker-compose.yml`（compose project 名稱 `llmgw-drill`），所以演練到的就是之後要用的設定。
- 第 4 步之後、第 5 步之前，gateway 在空的資料庫上執行：這就是正式搬遷時「新主機準備好、等著切換」的狀態，可以趁這時確認它的行為。
- `5432` / `8050` 在演練機器上已被占用時，設 `PG_PORT` / `GATEWAY_PORT`。

### 第 6 步驗證哪些事

| 類別 | 檢查 |
|---|---|
| 容器 | 執行中、`/readyz`、`/healthz`、主機網路、非 root、只聽 `127.0.0.1` |
| 資料 | schema 是映像的最新版本；每張表還原的筆數不少於快照；沒有舊主機留下的並發 lease；`audit_log` 防竄改觸發器還在 |
| 設定 | `config.toml` 和正式環境的雜湊相同；gateway 讀的是掛載的那份（不是內建範例）；config 目錄可寫入並 rename（admin 存檔的方式）；log 寫到主機；公鑰讀得到 |
| 不影響外面 | 容器裡沒有寄信 / Langfuse / 保留期設定；寄信功能關閉 |
| API | 臨時帳號的 key 驗證通過；`/v1/models` 有模型；錯誤的 key 回 401；gateway 看到的是 `X-Forwarded-For` 的使用者位址；Web UI 未登入回 401 |
| 模型（選用） | `DRILL_TEST_MODEL=<別名>`：送一個 `max_tokens=8` 的請求，確認回 200 並記進 `usage_logs` |

筆數用「不少於」比對，是因為快照在備份開始前一刻拍下，而正式環境同時還在收請求。

`DRILL_TEST_MODEL` 預設不設：演練機器不一定連得到 vLLM。要測的話給一個本機（vLLM）模型的別名，並確認演練機器連得到那台 vLLM。

**演練沒有涵蓋的**，正式切換後要另外確認：

- nginx 和 oauth2-proxy 的登入流程（演練機器沒有這兩者）；
- Azure / Bedrock 模型（會產生費用，演練不打）；
- 實際負載下的表現。

### 停機時間怎麼讀

第 6 步最後會印出兩個數字：

- **搬到新主機**（容器先開好，資料最後搬）：備份 + 載入資料。「載入資料」是第 5 步從停掉 gateway 到重新就緒的實測時間，包含還原、migration 和啟動。另外要加上把備份檔複製到新主機、切換流量的時間。
- **在原主機直接切換**（資料庫不動，只把 gateway 從 systemd 換成容器）：migration + 啟動。

這是用正式資料量測出來的，可以直接寫進變更申請。

### 7. 清除

```bash
bash deploy/migration-drill/7-teardown.sh
```

演練環境裡是一份完整的正式資料（所有 API key），**演練完一定要跑**。它會刪除容器、volume、還原出來的資料庫（`deploy/pgdata`）、`config/`、`keys/`、`logs/`、`.env`、`deploy/.env`，並確認沒有東西留下。加 `--image` 連映像一起刪。

留下來的只有紀錄（`drill.log`、`restore-drill.log`），裡面沒有資料或密鑰。**演練包和私鑰檔不在這個目錄裡，要另外刪除。**

## 紀錄

`drill.log` 一行一個步驟（成功或失敗都記），欄位以 tab 分隔：

```
2026-11-11 16:09:38  step=4-up         result=OK  elapsed=10s  by=alice@drill-host  bundle=migration-bundle-2026-11-11_160917 empty_migrate=2s start=5s ...
2026-11-11 16:09:56  step=5-load-data  result=OK  elapsed=10s  by=alice@drill-host  restore=1s migrate=2s start=5s total=10s schema=07899844c700->c1a7e4b9d2f6 users=200
2026-11-11 16:10:00  step=6-verify     result=OK  elapsed=4s   by=alice@drill-host  passed=30 failed=0 downtime_move=11s downtime_inplace=7s ...
```

正式主機上的 `1-bundle.sh` 也會在那邊的 `drill.log` 留一行。演練紀錄建議連同下面這張表一起存檔：

| 項目 | 內容 |
|---|---|
| 日期 / 執行者 | |
| 演練的程式版本（commit） | |
| 映像（tag 與 `3-image.sh` 記下的 id） | |
| 演練包（目錄名稱） | |
| schema：備份時 → migration 後 | |
| 第 6 步結果（通過 / 失敗項目） | |
| 估計停機時間（搬到新主機 / 直接切換） | |
| 發現的問題與處理 | |
| 第 7 步已執行、演練包與私鑰已刪除 | |

## 演練通過之後：正式切換與回退

正式切換的步驟和回退方式在 [deploy/README 的「以容器執行 gateway」](../README.zh-TW.md#以容器執行-gateway)：

- **搬到新主機**（演練的就是這個順序）：見「搬到新主機（容器先開好，資料最後搬）」。新主機先準備好，切換時在舊主機停 gateway、備份，在新主機跑 `deploy/load-backup.sh`，再切流量。
- **在原主機切換**：見「在原主機切換」。

切換前再確認一次：

- 用的是演練通過的同一個映像；
- 新主機連得到 vLLM、Azure、Bedrock、Exchange、Langfuse（對方的防火牆和白名單要加新主機的位址）；
- 切換前跑過 `deploy/backup-db.sh`；
- `deploy/.env` 的 `GATEWAY_UID` / `GATEWAY_GID` 是擁有 `config/`、`logs/` 的使用者；
- 公告的停機時間不少於演練量到的數字。

## 常見問題

| 訊息 | 原因與處理 |
|---|---|
| `看起來是正式主機，拒絕執行` | 這台有正式的容器、systemd 服務或設定檔。演練請換一台機器，或用一份乾淨的 checkout |
| `演練包的檔案和 SHA256SUMS 不符` | 複製途中損毀或被改過，從正式主機重新複製 |
| `無法解開 state.tar.gz.gpg` / `解密失敗` | 沒有私鑰：設 `DRILL_GPG_KEY` |
| `PostgreSQL 主版本不同` | 演練要用和正式相同主版本的映像 |
| `127.0.0.1:5432 已經有服務在聽` | 設 `PG_PORT` / `GATEWAY_PORT` 換一個 |
| `migration 失敗` | 看腳本印出的 migrate 輸出。這正是演練要抓的問題：在正式環境升版前修好 |
| `演練環境已經存在` | 先跑 `7-teardown.sh` |
| `資料還沒搬進來` | 先跑 `5-load-data.sh` |
