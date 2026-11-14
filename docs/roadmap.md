# Roadmap

未來要做、但還沒排進開發的事項。和 [tech-debt.md](tech-debt.md) 的分別：tech debt 是「已經上線、但做得不夠好」的東西；這裡是「還沒有的能力」。

每一項寫明：

- **現況**：今天的行為，附程式或文件的位置
- **缺口**：為什麼還不夠
- **方向**：可行做法（不是定案）
- **優先度**：P0（擋住目標）/ P1（目標達成前要做）/ P2（可以之後再做）
- **狀態**：`未開始` / `評估中` / `進行中` / `完成`

新增項目放在所屬主題下；主題完成後整段移到最下方的「已完成」。

---

## 主題：高可用（HA）

**目標**：任何一台 gateway host 或 PostgreSQL 節點故障時，服務在不需人工介入（或只需極短的人工切換）下持續可用。

**已經具備的基礎**（不需再做，做 HA 時別破壞）：

- 定時工作以 `scheduled_jobs` 的 conditional UPDATE 搶執行權，跨 worker、跨 host 每週期只跑一次（`app/services/scheduler.py`）。
- 每日用量彙總以 `pg_try_advisory_xact_lock` 鎖每一天，只有一台在算（`app/services/usage_rollup.py`）。
- 週報以 `report_schedules.last_period` 搶執行權，每週只寄一次（`app/services/weekly_reports.py`）。
- 併發限制的 lease 存在 PostgreSQL（`concurrency_leases`），`worker_id` 含 hostname。
- `/readyz` 只檢查資料庫，下游掛掉不會讓 LB 把所有 instance 一起拿掉（`docs/architecture.md`，*Runtime patterns*）。
- vLLM request-time failover 與 health hysteresis；雲端模型的 `rate_limit_fallback`。

### HA-1　PostgreSQL 高可用 — P0 · 未開始

- **現況**：`deploy/docker-compose.yml` 只有一個 `postgres:16` 容器，資料在本機 `pgdata`。有備份（`deploy/backup-db.sh`）與還原演練（`deploy/restore-drill.sh`），那是災難復原，不是 HA。
- **缺口**：PostgreSQL 是整套系統的單點故障。它掛掉時，API-key 認證、計費、併發 lease 全部失敗，所有 gateway instance 一起不能用；`/readyz` 也會全部變成 not ready。
- **方向**（三選一，需評估）：
  1. 公司既有的託管 / DBA 維運的 PostgreSQL 叢集。
  2. Patroni + etcd + HAProxy（或 VIP），自動切換。
  3. Streaming replication 一主一備 + 人工 promote（最簡單，RTO 以分鐘計）。
- **要一起確認的事**：
  - Gateway 只吃一個 `DATABASE_URL`：需要一個會跟著 primary 走的端點（HAProxy / VIP / pgbouncer），而不是寫死節點位址。
  - 切換瞬間進行中的 transaction 會失敗：確認 `_log_usage` 等副作用的 `except` 能吸收、連線池（`pool_pre_ping` 等）能自己恢復，不需重啟 gateway。
  - Advisory lock 都是 transaction-scoped（`pg_try_advisory_xact_lock`），切換後不會殘留；確認沒有 session-level lock。
  - `audit_log` 的 append-only trigger、`alembic upgrade` 只對 primary 執行。
  - `backup-db.sh` 改對 replica（或 primary）備份的取捨；`restore-drill.sh` 照舊可用。
  - `max_connections=200` 要能容納「host 數 × worker 數 × pool 大小」。

### HA-2　多台 host 共用設定檔 — P0 · 未開始

- **現況**：`config.toml` 與 `client_settings.json` 放在各 host 自己的 `CONFIG_DIR`。Admin 在 `/admin/models` 或 *Client setup* 存檔，只寫到處理該請求的那台 host；同 host 的其他 worker 靠 mtime 自動重新載入（`app/core/config.py` `_check_auto_reload`、`client_settings.load`），**其他 host 不會**。
- **缺口**：多台 host 時，模型路由、價格、client settings 會因「這個請求落在哪台」而不同，計費與路由不一致。
- **方向**：
  1. 共用 `CONFIG_DIR`（NFS / 共享磁碟）。`save_config` 是同目錄 temp file + `os.replace`，在 NFS 上仍是原子替換；需確認 NFS 的 mtime 精度與 attribute cache 不會讓重新載入延遲太久。
  2. 或：設定改存 PostgreSQL（較大的改動，牽涉 `save_config`、`reload_config`、測試的 patch 方式，以及 CLAUDE.md 多條規則）。
  3. 短期權宜：只在一台 host 上編輯，以部署工具同步到其他 host；並在 Admin 頁面上標示「這台的設定版本」。
- `AUTH_CENTER_PUBLIC_KEY_PATH` 的公鑰檔也要在每台 host 一致（它已依 mtime 重新載入）。

### HA-3　前端拓樸：LB → 各 host nginx → gateway — P1 · 未開始

- **現況**：`deploy/llm-gateway-example.nginx.conf` 是單機架構，`proxy_pass http://127.0.0.1:8050`。容器版綁 `127.0.0.1` 並用 `network_mode: host`（刻意如此，見 CLAUDE.md *Containers*）。
- **缺口**：沒有多台 host 的參考架構與設定範例。
- **方向**：
  - 每台 host 保留自己的 nginx + oauth2-proxy + gateway；前面一層 LB 以 `/readyz` 做健康檢查。
  - LB 的位址加進 `--forwarded-allow-ips` / `FORWARDED_ALLOW_IPS`，否則所有請求的來源 IP 都變成 LB，IP allowlist、failed-auth throttle、audit `client`、`CC002` 全部錯（`docs/architecture.md`，*API IP allowlist*）。nginx 那層也要設定 `real_ip` 相關信任。
  - 各 host 的 oauth2-proxy 使用相同的 `OAUTH2_COOKIE_SECRET`，否則換一台就要重新登入；確認 session 存在 cookie 而非單機 store。
  - LB 的 idle / read timeout 要大於 nginx 的 300 s，且不能 buffer SSE。
  - 整理成 `deploy/` 下的一份多 host 說明與 nginx / LB 範例（依 CLAUDE.md，不得改動既有的 `deploy/docker-compose.yml` 與 `.env.example`）。

### HA-4　優雅下線（drain）— P1 · 未開始

- **現況**：沒有 drain 機制。`deploy/llm-gateway.service` 沒設 stop timeout（systemd 預設 90 s 後 SIGKILL），`/readyz` 在關機過程中不會先變成 not ready。
- **缺口**：滾動更新或下線一台 host 時，LB 仍會送新請求進來，正在跑的 stream 可能被砍斷；Claude Code 會重試，但其他 client 未必。
- **方向**：收到 SIGTERM 後先讓 `/readyz` 回 503，等 LB 摘除（數秒），再等進行中的 stream 結束（設上限）才退出；systemd 的 `TimeoutStopSec` 與 uvicorn 的 `--timeout-graceful-shutdown` 配合調整。要遵守 CLAUDE.md *Streams* 與 *DB connections* 的規則（不在 stream 期間持有 DB 連線、teardown 要 shield）。

### HA-5　跨 instance 的 failed-auth throttle — P2 · 未開始

- **現況**：`app/services/auth_throttle.py` 以每個 worker 的記憶體計數（`AUTH_FAIL_DISTINCT_KEYS` 預設 10）。
- **缺口**：H 台 host × W 個 worker 時，猜 key 的人實際可試約 H×W 倍次數才全部鎖住。256-bit key 下暴力猜測本身不可行，所以風險低，但數字和設定值不一致。
- **方向**：(a) 接受並在文件寫明，必要時把門檻除以 instance 數；或 (b) 改存 PostgreSQL（注意：throttle 在 key lookup 之前檢查，每個請求多一次 DB 存取要評估；需維持「計 distinct key、鎖住後不做 lookup」的規則）。

### HA-6　Host 故障後的併發 lease 回收 — P2 · 未開始

- **現況**：同 host 死掉的 worker 由 heartbeat 以 pid 判斷清掉；**別台 host** 的 lease 只能等 `LEASE_TTL`（180 s）過期（`app/services/concurrency.py`）。
- **缺口**：一台 host 整台掛掉時，它手上的 lease 會在最多 3 分鐘內持續佔用那些使用者的額度（只在 `enforce` 模式下有感）。
- **方向**：評估縮短 TTL（heartbeat 間隔要跟著縮）或接受現況並寫進文件。

### HA-7　每 worker 狀態的一致性說明 — P2 · 未開始

- **現況**：vLLM health cache、IP allowlist snapshot（15 s）、client rules snapshot（15 s）都是各 worker 各自維護。
- **缺口**：多 host 時不同 instance 可能短暫看到不同狀態（例如一台認為某 vLLM DOWN、另一台認為 UP）。屬於設計上可接受的最終一致，但沒有寫成一處說明。
- **方向**：在 `docs/architecture.md` 加一節「多 instance 部署」，列出哪些狀態共享（PG）、哪些每 worker、最大落後時間；HA-3 的部署說明連過去。

### HA-8　Prometheus 指標的多 host 收集 — P2 · 未開始

- **現況**：`METRICS_ENABLED` 時以 multiprocess 模式在 `METRICS_DIR` 加總**同一台 host** 的 worker。
- **缺口**：多 host 時每台各有一份 `/metrics`。
- **方向**：Prometheus 對每台 host 各自 scrape（以 `instance` label 區分），dashboard / alert 的查詢改成 `sum by (...)`；寫進部署說明。`/metrics` 不要經過 LB（會隨機打到某一台）。

### HA-9　HA 演練 — P1 · 未開始（依賴 HA-1 ~ HA-4）

- **現況**：有搬遷演練（`deploy/migration/` 的 1 ~ 7 步）與備份還原演練，沒有故障演練。
- **方向**：在非正式環境以兩台 gateway host + PG HA 做一次：停掉一台 gateway、切換 PG primary、滾動更新，各自記錄 client 看到的錯誤與恢復時間（RTO），並留下紀錄檔（比照 `drill.log` / `restore-drill.log`）。

---

## 已完成

（尚無）
