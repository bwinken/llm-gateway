# Roadmap

未來要做、但還沒排進開發的事項。和 [tech-debt.md](tech-debt.md) 的分別：tech debt 是「已經上線、但做得不夠好」的東西；這裡是「還沒有的能力」。

每一項寫明：

- **現況**：今天的行為，附程式或文件的位置
- **缺口**：為什麼還不夠
- **方向**：可行做法（不是定案）
- **階段**：1（現在）/ 2（接下來）/ 3（之後，等條件成立）。階段內的先後見「執行順序」
- **決定**：已經定案的做法與日期（沒有這一行表示還沒決定）
- **狀態**：`未開始` / `評估中` / `進行中` / `完成`

新增項目放在所屬主題下；主題完成後整段移到最下方的「已完成」。標示 **⚠ 牽動規則** 的項目會改變 [CLAUDE.md](../CLAUDE.md) *Rules* 裡的某一條，動工前要先決定是否修改那條規則，並同步更新文件與釘住它的測試。

## 現況（2026-11-14）

排序依據。現況改變時先改這裡，再調整「執行順序」。

- **部署**：單台主機（gateway 與 PostgreSQL），沒有備機、沒有 replica。很多人每天依賴，停機很難接受。沒有 DBA，還沒有 k8s。
- **合規**：沒有認證時程；要能回應內部稽核（控制項對照、證據）。
- **雲端資料**：公司有規定，允許部分資料送往 Azure / AWS；目前只靠使用者自律。
- **費用**：預算在本部門，只看報表、不向其他部門收費（showback）；未來可能 by BU。
- **Guardrail**：地端 Qwen3Guard 已開啟，monitor 模式，有每週報告。

## 已定的決策（2026-11-14）

| 項目 | 決策 |
|---|---|
| HA-1 | PostgreSQL 用 streaming replication（一主一備、人工切換），備機放在**另一台**主機 B。暫不做 Patroni |
| HA-3 | 現在做冷備：B 裝好但平常不接流量，故障時手動切換。流量用浮動 IP（VIP）轉移。多 gateway、多 PG 是之後的事（HA-11） |
| HA-2 | 冷備階段用定時 rsync 把設定檔從 A 同步到 B。設定改存資料庫留到 HA-11 |
| GRD-1、GRD-2 | 個資與機密偵測第一版只做 monitor（記錄類型與數量），觀察後再決定對雲端開 mask / block |
| GRD-3 | 雲端內容過濾用供應商原生功能（Azure 內容篩選、Bedrock Guardrails）；不把 Qwen3Guard 加到雲端路徑，不改規則 |
| GRD-4 | 間接 prompt injection 先靠 client 端（Claude Code 權限設定 + client rules）；gateway 端偵測往後放 |
| GRD-8 | Guardrail 維持 fail-open，寫成風險接受 |
| CMP-2 | API key 維持明文，寫成風險接受；下游金鑰（Azure / AWS）先做加密存放 |
| CMP-3 | 先做「長期未用的 key 自動停用」，不做強制效期 |
| CMP-6 | `usage_logs` 明細保存 1 年；`usage_daily` 彙總永久保存 |
| BIL-1 | `org_code` → BU 用另外維護的對照表 |

## 執行順序

### 階段 1　現在：從「單台」變成「有冷備、能手動切換」

停機是目前最大的風險，其他主題都排在這之後。

| 順序 | 編號 | 做什麼 | 備註 |
|---|---|---|---|
| 1 | HA-3（前置） | 申請主機 B、VIP、B 的對外連線白名單 | 要等別的單位，最先送出 |
| 2 | HA-1 | A → B 的 PostgreSQL streaming replication，加上複寫監控 | |
| 3 | HA-2 | 設定檔定時 rsync 到 B | |
| 4 | HA-3 | B 上安裝 nginx、oauth2-proxy、gateway（gateway 停用）；服務名稱改指 VIP | |
| 5 | HA-10 | 切換 runbook（含防 split-brain、備份 cron 搬移、A 重建成備機） | |
| 6 | HA-9 | 實際演練一次冷備切換，記錄切換時間 | 之後定期重做 |
| 7 | CMP-6 | 設定 `usage_logs` 保存 1 年 | 只是一個設定，隨時可做；資料庫變小也讓複寫與還原更快 |

### 階段 2　接下來：雲端資料規定與內部稽核

文件類（AUD-3、CMP-1）不需要等階段 1，可以並行。

| 順序 | 編號 | 做什麼 | 備註 |
|---|---|---|---|
| 8 | AUD-3 | 風險登錄表 | 已經有三項風險接受要記錄（CMP-2、GRD-8、GRD-4 的範圍） |
| 9 | CMP-1 | 四個框架的控制項對照表 | 內稽最先會要的東西 |
| 10 | GRD-2 | 提示中的機密偵測（monitor，雲端） | 規則式、誤判少，比 PII 快 |
| 11 | GRD-1 | PII 偵測（monitor，雲端） | 類別對應公司規定中「不可送雲端」的資料 |
| 12 | GRD-3 | 套用並記錄供應商原生的內容過濾 | |
| 13 | AUD-2 | 存取審查報表 | |
| 14 | CMP-3 | 長期未用的 key 自動停用 | 和 AUD-2 用同一份資料 |
| 15 | CMP-2 | 下游金鑰加密存放 | |
| 16 | CMP-4 | 設定變更歷史（含舊值） | |
| 17 | AUD-1 | 稽核證據包一鍵產出 | 依賴前面幾項的產出 |
| 18 | GRD-7 | 訂出 guardrail 從 monitor 切到 block 的條件 | 資料已在累積（週報） |

### 階段 3　之後：等條件成立再排

| 觸發條件 | 項目 |
|---|---|
| GRD-1 / GRD-2 的 monitor 累積數週資料 | 決定是否對雲端開 mask / block（GRD-1、GRD-2 的第二步） |
| 冷備的切換時間不夠快，或確定要上 k8s | HA-11（多 gateway、多 PG），連同 HA-4 ~ HA-8 |
| 確定要 by BU 看費用 | BIL-1（對照表），接著 BIL-3（雲端帳單對帳） |
| 確定要向其他部門收費 | BIL-2、BIL-4、BIL-5、BIL-6、BIL-7 |
| 內稽提出要求，或發生事件 | CMP-5（事件流程）、AUD-4（AI 系統清冊與影響評估） |
| 有 GPU 與人力可用 | GRD-5（輸出檢查）、GRD-4（gateway 端的注入偵測）、GRD-6（自訂內容政策） |

---

## 主題：高可用（HA）

**目標**：任何一台 gateway host 或 PostgreSQL 節點故障時，服務在不需人工介入（或只需極短的人工切換）下持續可用。

**分兩個階段**：現在先做**冷備**（第二台主機 B 裝好、PG 持續複寫，故障時手動切換：HA-1、HA-2、HA-3、HA-10、HA-9）；之後再做**多 gateway、多 PG**（HA-11，連同 HA-4 ~ HA-8）。

**已經具備的基礎**（不需再做，做 HA 時別破壞）：

- 定時工作以 `scheduled_jobs` 的 conditional UPDATE 搶執行權，跨 worker、跨 host 每週期只跑一次（`app/services/scheduler.py`）。
- 每日用量彙總以 `pg_try_advisory_xact_lock` 鎖每一天，只有一台在算（`app/services/usage_rollup.py`）。
- 週報以 `report_schedules.last_period` 搶執行權，每週只寄一次（`app/services/weekly_reports.py`）。
- 併發限制的 lease 存在 PostgreSQL（`concurrency_leases`），`worker_id` 含 hostname。
- `/readyz` 只檢查資料庫，下游掛掉不會讓 LB 把所有 instance 一起拿掉（`docs/architecture.md`，*Runtime patterns*）。
- vLLM request-time failover 與 health hysteresis；雲端模型的 `rate_limit_fallback`。

### HA-1　PostgreSQL 高可用 — 階段 1 · 未開始

- **決定（2026-11-14）**：streaming replication：非同步、一主一備、人工 promote；備機放在**另一台主機** B（VM 的話要在不同的實體 hypervisor 與儲存上，同一台的備機防不了主機、硬碟、電源故障）。暫不做 Patroni：沒有 DBA，而且未來若上 k8s 會改用 operator（例如 CloudNativePG）。複寫要監控延遲與中斷（備機默默停止複寫是最典型的陷阱），並設定 `max_slot_wal_keep_size`，避免 B 停機時 A 的 WAL 把磁碟塞滿。非同步複寫在切換時可能遺失最後幾秒的寫入（幾筆用量紀錄），可以接受。
- **現況**：`deploy/docker-compose.yml` 只有一個 `postgres:16` 容器，資料在本機 `pgdata`。有備份（`deploy/backup-db.sh`）與還原演練（`deploy/restore-drill.sh`），那是災難復原，不是 HA。
- **缺口**：PostgreSQL 是整套系統的單點故障。它掛掉時，API-key 認證、計費、併發 lease 全部失敗，所有 gateway instance 一起不能用；`/readyz` 也會全部變成 not ready。
- **方向**（已選 3；1 與 2 留作日後參考）：
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

### HA-2　多台 host 共用設定檔 — 階段 1 · 未開始

- **決定（2026-11-14）**：冷備階段由 A 上的 cron 定時 rsync（走 SSH）`CONFIG_DIR`（`config.toml`、`client_settings.json`）、`.env`、AuthCenter 公鑰到 B；`.env` 含機密，B 上的檔案權限要和 A 相同。B 的 gateway 平常停著，不會有兩邊同時寫入。切換後方向要反過來（B → A），寫進 HA-10 的 runbook。方向 2（設定改存資料庫）留到 HA-11。
- **現況**：`config.toml` 與 `client_settings.json` 放在各 host 自己的 `CONFIG_DIR`。Admin 在 `/admin/models` 或 *Client setup* 存檔，只寫到處理該請求的那台 host；同 host 的其他 worker 靠 mtime 自動重新載入（`app/core/config.py` `_check_auto_reload`、`client_settings.load`），**其他 host 不會**。
- **缺口**：多台 host 時，模型路由、價格、client settings 會因「這個請求落在哪台」而不同，計費與路由不一致。
- **方向**：
  1. 共用 `CONFIG_DIR`（NFS / 共享磁碟）。`save_config` 是同目錄 temp file + `os.replace`，在 NFS 上仍是原子替換；需確認 NFS 的 mtime 精度與 attribute cache 不會讓重新載入延遲太久。
  2. 或：設定改存 PostgreSQL（較大的改動，牽涉 `save_config`、`reload_config`、測試的 patch 方式，以及 CLAUDE.md 多條規則）。
  3. 短期權宜：只在一台 host 上編輯，以部署工具同步到其他 host；並在 Admin 頁面上標示「這台的設定版本」。
- `AUTH_CENTER_PUBLIC_KEY_PATH` 的公鑰檔也要在每台 host 一致（它已依 mtime 重新載入）。

### HA-3　前端拓樸：LB → 各 host nginx → gateway — 階段 1 · 未開始

- **決定（2026-11-14）**：分兩步。**現在（冷備）**：服務名稱指向浮動 IP（VIP），切換時把 VIP 移到 B；B 上裝好 nginx、oauth2-proxy、gateway，gateway 的 systemd unit 設為 disable（B 本機的 PG 是唯讀備機，而且 gateway 一連上可寫的資料庫就會開始跑排程工作、寄信）。VIP 需要 A、B 在同一個網段；如果 B 在另一個機房，要改用 DNS 或既有的 LB。B 的出口 IP 要事先加進所有檢查來源的地方（Azure OpenAI 防火牆 / private endpoint、vLLM 主機、AuthCenter、Exchange EWS、Langfuse），TLS 憑證與 `OAUTH2_COOKIE_SECRET` 和 A 相同。**之後（HA-11）**：VIP 改接 LB，兩台同時服務；下面的 LB 相關事項屬於那個階段。
- **現況**：`deploy/llm-gateway-example.nginx.conf` 是單機架構，`proxy_pass http://127.0.0.1:8050`。容器版綁 `127.0.0.1` 並用 `network_mode: host`（刻意如此，見 CLAUDE.md *Containers*）。
- **缺口**：沒有多台 host 的參考架構與設定範例。
- **方向**：
  - 每台 host 保留自己的 nginx + oauth2-proxy + gateway；前面一層 LB 以 `/readyz` 做健康檢查。
  - LB 的位址加進 `--forwarded-allow-ips` / `FORWARDED_ALLOW_IPS`，否則所有請求的來源 IP 都變成 LB，IP allowlist、failed-auth throttle、audit `client`、`CC002` 全部錯（`docs/architecture.md`，*API IP allowlist*）。nginx 那層也要設定 `real_ip` 相關信任。
  - 各 host 的 oauth2-proxy 使用相同的 `OAUTH2_COOKIE_SECRET`，否則換一台就要重新登入；確認 session 存在 cookie 而非單機 store。
  - LB 的 idle / read timeout 要大於 nginx 的 300 s，且不能 buffer SSE。
  - 整理成 `deploy/` 下的一份多 host 說明與 nginx / LB 範例（依 CLAUDE.md，不得改動既有的 `deploy/docker-compose.yml` 與 `.env.example`）。

### HA-4　優雅下線（drain） — 階段 3 · 未開始

- **現況**：沒有 drain 機制。`deploy/llm-gateway.service` 沒設 stop timeout（systemd 預設 90 s 後 SIGKILL），`/readyz` 在關機過程中不會先變成 not ready。
- **缺口**：滾動更新或下線一台 host 時，LB 仍會送新請求進來，正在跑的 stream 可能被砍斷；Claude Code 會重試，但其他 client 未必。
- **方向**：收到 SIGTERM 後先讓 `/readyz` 回 503，等 LB 摘除（數秒），再等進行中的 stream 結束（設上限）才退出；systemd 的 `TimeoutStopSec` 與 uvicorn 的 `--timeout-graceful-shutdown` 配合調整。要遵守 CLAUDE.md *Streams* 與 *DB connections* 的規則（不在 stream 期間持有 DB 連線、teardown 要 shield）。

### HA-5　跨 instance 的 failed-auth throttle — 階段 3 · 未開始

- **現況**：`app/services/auth_throttle.py` 以每個 worker 的記憶體計數（`AUTH_FAIL_DISTINCT_KEYS` 預設 10）。
- **缺口**：H 台 host × W 個 worker 時，猜 key 的人實際可試約 H×W 倍次數才全部鎖住。256-bit key 下暴力猜測本身不可行，所以風險低，但數字和設定值不一致。
- **方向**：(a) 接受並在文件寫明，必要時把門檻除以 instance 數；或 (b) 改存 PostgreSQL（注意：throttle 在 key lookup 之前檢查，每個請求多一次 DB 存取要評估；需維持「計 distinct key、鎖住後不做 lookup」的規則）。

### HA-6　Host 故障後的併發 lease 回收 — 階段 3 · 未開始

- **現況**：同 host 死掉的 worker 由 heartbeat 以 pid 判斷清掉；**別台 host** 的 lease 只能等 `LEASE_TTL`（180 s）過期（`app/services/concurrency.py`）。
- **缺口**：一台 host 整台掛掉時，它手上的 lease 會在最多 3 分鐘內持續佔用那些使用者的額度（只在 `enforce` 模式下有感）。
- **方向**：評估縮短 TTL（heartbeat 間隔要跟著縮）或接受現況並寫進文件。

### HA-7　每 worker 狀態的一致性說明 — 階段 3 · 未開始

- **現況**：vLLM health cache、IP allowlist snapshot（15 s）、client rules snapshot（15 s）都是各 worker 各自維護。
- **缺口**：多 host 時不同 instance 可能短暫看到不同狀態（例如一台認為某 vLLM DOWN、另一台認為 UP）。屬於設計上可接受的最終一致，但沒有寫成一處說明。
- **方向**：在 `docs/architecture.md` 加一節「多 instance 部署」，列出哪些狀態共享（PG）、哪些每 worker、最大落後時間；HA-3 的部署說明連過去。

### HA-8　Prometheus 指標的多 host 收集 — 階段 3 · 未開始

- **現況**：`METRICS_ENABLED` 時以 multiprocess 模式在 `METRICS_DIR` 加總**同一台 host** 的 worker。
- **缺口**：多 host 時每台各有一份 `/metrics`。
- **方向**：Prometheus 對每台 host 各自 scrape（以 `instance` label 區分），dashboard / alert 的查詢改成 `sum by (...)`；寫進部署說明。`/metrics` 不要經過 LB（會隨機打到某一台）。

### HA-9　HA 演練 — 階段 1 · 未開始（依賴 HA-1 ~ HA-3、HA-10）

- **決定（2026-11-14）**：第一次演練的對象是冷備切換（HA-10 的 runbook）：A → B，再把 A 重建成備機。多 gateway 的故障演練等 HA-11。
- **現況**：有搬遷演練（`deploy/migration/` 的 1 ~ 7 步）與備份還原演練，沒有故障演練。
- **方向**：在非正式環境以兩台 gateway host + PG HA 做一次：停掉一台 gateway、切換 PG primary、滾動更新，各自記錄 client 看到的錯誤與恢復時間（RTO），並留下紀錄檔（比照 `drill.log` / `restore-drill.log`）。

### HA-10　冷備切換 runbook — 階段 1 · 未開始

- **現況**：沒有。
- **方向**：一份照著做就能完成的文件（放在 `deploy/`），並用腳本包住容易出錯的步驟：
  1. 判斷要不要切：A 的 gateway / PG 是否真的無法在短時間內恢復。
  2. **隔離 A**：確認 A 的 PG 與 gateway 已停，並設為開機不自動啟動。A 之後修好開機時，兩者都不能自己起來，否則會有兩個主資料庫、兩邊都在跑排程工作與寄信（split-brain）。
  3. 在 B 上 promote PG，檢查資料新舊（最後一筆 `usage_logs` 的時間）。
  4. 清掉 A 的 worker 留下的 `concurrency_leases`（比照 `load-backup.sh`；否則要等 180 秒 TTL）。
  5. 啟動 B 的 gateway，確認 `/readyz`，把 VIP 移到 B。
  6. 驗證：SSO 登入、一個地端請求、一個 Azure 與一個 AWS 請求（確認白名單）、admin 頁面。
  7. 切換後要搬的東西：`backup-db.sh` 的 cron 改在 B 上跑（它是主機 cron，不是 gateway 內的工作）；設定檔 rsync 的方向反過來。
  8. A 修好後：用 `pg_rewind` 或 `pg_basebackup` 重建成 B 的備機，再決定要不要切回來。
- 每次上版要兩台都更新程式；`alembic upgrade` 只在主資料庫執行。這一條寫進部署流程。
- 要遵守 `deploy/` 的既有規則：不改動 `deploy/docker-compose.yml` 與 `.env.example`。

### HA-11　多 gateway、多 PG — 階段 3 · 未開始

- **現況**：冷備（HA-1 ~ HA-3、HA-10 完成後）。
- **觸發條件**：冷備的手動切換時間不夠快，或確定要上 k8s。多台 gateway 不一定需要 k8s，兩台主機前面放 LB 就做得到。
- **方向**：VIP 改接 LB，兩台 gateway 同時服務（HA-3 的後半）；設定檔不能再用單向 rsync，要改成共用或存資料庫（HA-2 的方向 1 / 2）；PG 改成自動切換（Patroni 可以接管既有的複寫；上 k8s 的話用 operator）。HA-4（drain）、HA-5 ~ HA-8 都屬於這個階段。

---

## 主題：合規框架對應

**目標**：說得清楚這個 gateway 在 ISO/IEC 27001:2022、ISO/IEC 42001:2023、NIST AI RMF 1.0（含生成式 AI profile NIST AI 600-1）、OWASP Top 10 for LLM Applications（2025）裡負責哪些控制、能提供什麼證據、缺什麼。

**現況**：沒有認證時程，目標是能回應內部稽核。所以先做對照表（CMP-1）與風險登錄表（AUD-3），程式改動依對照出來的缺口排。

**範圍提醒**：這些框架大多是**組織層級**的管理系統（政策、角色、風險評估、管理審查、適用性聲明 SoA）。Gateway 只是其中一個系統，能做的是「實作一部分技術控制」和「產出證據」；政策文件和管理流程由組織負責，不在這個 repo 裡。

**已經具備、可當證據的部分**：

| 能力 | 位置 |
|---|---|
| SSO（AuthCenter JWT）、admin 權限只來自 token scope | `app/core/auth.py` |
| 每人 API key（256 bit）、失敗認證 throttle、IP allowlist | `schema._generate_api_key`、`auth_throttle.py`、`ip_allowlist.py` |
| 每人每日預算、雲端上限、併發上限 | `deps.py`、`concurrency.py` |
| 稽核紀錄（append-only，PG trigger 拒絕修改），CSV 匯出 | `audit.py` |
| key 發行時間與最後使用時間 | `users.api_key_created_at` / `api_key_last_used_at` |
| Log 不記 prompt（只記形狀）、client 看不到內部錯誤 | `redact.py` |
| Langfuse I/O 擷取預設關閉 | `LANGFUSE_CAPTURE_IO` |
| 每小時異常偵測（成本暴增、非上班時間、爆量、行為變化） | `anomaly_scan.py` |
| Prompt 安全篩檢（地端流量）、guardrail 週報 | `guardrail.py`、`weekly_reports.py` |
| 加密備份、還原演練與紀錄 | `deploy/backup-db.sh`、`deploy/restore-drill.sh` |
| 相依套件弱點掃描、secret 掃描 | `.github/workflows/security.yml`（pip-audit、gitleaks） |
| CORS 不帶 credentials、跨站寫入防護 | `browser_security.py` |

### CMP-1　控制項對照表 — 階段 2 · 未開始

- **方向**：在 `docs/compliance/` 下各框架一份對照表，每列：控制項 → gateway 的做法（程式 / 設定位置）→ 證據（哪個報表、log、表格）→ 缺口（連到本 roadmap 的編號）。初步對照：
  - **ISO 27001:2022 Annex A**：5.15 / 5.18 存取控制與存取權（SSO、API key、AUD-2）、5.17 認證資訊（CMP-3）、8.2 特權存取（admin scope）、8.8 技術弱點（pip-audit）、8.10 資訊刪除（CMP-6）、8.11 資料遮罩與 8.12 資料外洩防護（GRD-1、GRD-2）、8.13 備份（已有）、8.14 備援（HA 主題）、8.15 日誌與 8.16 監控（audit log、異常偵測、Prometheus）、8.24 密碼學（CMP-2）、8.32 變更管理（CMP-4）、5.24 ~ 5.27 事件管理（CMP-5）、5.30 營運持續（HA、還原演練）。
  - **ISO 42001 Annex A**：A.4 AI 系統資源（模型與後端清冊，AUD-4）、A.5 影響評估（AUD-4）、A.6 生命週期中的運作與監控（Langfuse、週報）、A.8 對利害關係人的資訊（dashboard 公告、client notices）、A.9 AI 系統的使用（預算、guardrail、使用政策）、A.10 第三方（Azure、AWS 供應商管理）。
  - **NIST AI RMF**：GOVERN（角色與權限、政策）、MAP（AI 系統清冊與使用情境，AUD-4）、MEASURE（guardrail 判定率、錯誤率、延遲：週報）、MANAGE（block 模式、模型下架、事件處理 CMP-5）。
  - **OWASP LLM Top 10（2025）**：LLM01 Prompt Injection（部分：Qwen3Guard 的 Jailbreak 類別只看使用者最新一輪 → GRD-4）、LLM02 Sensitive Information Disclosure（部分：log 已遮蔽；請求內容無 PII 處理 → GRD-1、GRD-2）、LLM03 Supply Chain（pip-audit、gitleaks；模型來源未記錄 → AUD-4）、LLM04 Data and Model Poisoning（gateway 不訓練模型，記錄模型來源即可）、LLM05 Improper Output Handling（輸出未檢查 → GRD-5；實際的輸出處理在 client 端）、LLM06 Excessive Agency（tool 執行在 client 端，gateway 看不到，寫明不在範圍）、LLM07 System Prompt Leakage（gateway 不注入 system prompt，風險低）、LLM08 Vector and Embedding Weaknesses（embeddings / rerank 直接轉送，RAG 在 client 端）、LLM09 Misinformation（不在 gateway 範圍）、LLM10 Unbounded Consumption（已涵蓋：每日預算、雲端上限、併發限制、throttle）。
- 對照表要隨程式變更維護：可以在 CLAUDE.md 加一條「改到對照表引用的控制，要同步更新 `docs/compliance/`」。

### CMP-2　機密資料加密 — 階段 2 · 未開始

- **決定（2026-11-14）**：API key 維持明文（dashboard 與安裝程式要讀得回來），寫成風險接受放進 AUD-3，補償控制是主機與資料庫的存取控制、加密備份。先做方向 (b)：下游金鑰（Azure / AWS）加密存放，它們外洩的影響最大。
- **現況**：API key 以明文存在 `users` 表（`docs/architecture.md` *API key exposure* 已說明這是刻意保留的 UX 決定：dashboard 和安裝程式要讀得回來）。下游金鑰（Azure、Bedrock）以明文存在 `config.toml`；admin 介面只送 `<stored:...>` 參照，但磁碟上是明文。
- **缺口**：ISO 27001 8.24 的稽核常會問到；資料庫備份或 `config.toml` 外洩就等於所有 key 外洩。
- **方向**：(a) API key 改存 hash，dashboard 只在發行當下顯示一次，安裝程式改用 `POST /auth/api-key` 的 SSO 登入流程取得 key；需要決定既有使用者的轉換方式。(b) 下游金鑰移到 secret manager 或以主機金鑰加密存放。(c) 至少先寫成風險接受（risk acceptance）放進 AUD-3。

### CMP-3　API key 期限與輪替政策 — 階段 2 · 未開始

- **決定（2026-11-14）**：先做「長期未用自動停用」（例如 90 天；service account 另訂或排除），不做強制效期。天數由 AUD-2 的實際資料決定。
- **現況**：key 沒有到期日；有發行與最後使用時間，舊格式 key 有 dashboard 提醒。
- **方向**：可設定的最長效期（到期前 dashboard 與信件提醒）、長期未用 key 自動停用（例如 90 天，service account 另訂）、輪替紀錄進稽核。要遵守 CLAUDE.md「不得依格式拒絕舊 key」與「只透過 `schema.issue_api_key` 發行」兩條規則。

### CMP-4　設定變更歷史 — 階段 2 · 未開始

- **現況**：`PUT /admin/api/config` 的稽核只記 `config_changes`，也就是**欄位名稱**，不記改前改後的值；`config.toml` 本身沒有版本。
- **缺口**：變更管理（ISO 27001 8.32）問「誰在什麼時候把價格 / 路由從 A 改成 B」時答不出來，也沒辦法回滾到某個版本。
- **方向**：每次 `save_config` 保留一份版本（排除 `SECRET_FIELDS`），稽核紀錄指向版本號；admin 頁面可比對、回滾。和 BIL-5、HA-2（如果設定改存 DB）一起設計。

### CMP-5　AI 事件處理流程與紀錄 — 階段 3 · 未開始

- **現況**：異常偵測只偵測、不阻擋（`anomaly_events`）；guardrail 判定在 Langfuse；沒有「事件」的概念（開立、指派、處置、結案）。
- **方向**：定義什麼算事件（guardrail 重複 unsafe、異常偵測高嚴重度、key 外洩、模型輸出造成問題的通報），在 admin 開一個事件紀錄（append-only，比照 `audit_log`），可以從異常偵測和 guardrail 週報的「要注意的帳號」一鍵開立；處置動作（停用帳號、撤銷 key、模型下架）連到稽核紀錄。

### CMP-6　資料保存政策 — 階段 1 · 未開始

- **決定（2026-11-14）**：`usage_logs` 明細保存 1 年（`USAGE_LOG_RETENTION_DAYS=365`）；`usage_daily` 彙總永久保存，報表數字不受影響。其他資料（log、Langfuse、備份）的期限還沒訂。
- **現況**：`usage_logs` 保存預設關閉（`USAGE_LOG_RETENTION_DAYS`，刻意不預設）；`audit_log` 不刪；應用程式 log 保存 `LOG_RETENTION`（預設 14 天）；Langfuse 的保存由 Langfuse 端設定；備份有自己的保留期。
- **方向**：一份保存期限表（每種資料：內容、是否含個資、保存期限、依據、刪除方式），由組織決定期限；gateway 端照表設定。注意 CLAUDE.md 的規則：retention 不得刪到 rollup watermark 之後，`usage_logs` 是計費紀錄。

---

## 主題：稽核與風險文件產出

**目標**：稽核時需要的文件和證據，可以從系統直接產出，不靠人工截圖、整理。

### AUD-1　稽核證據包一鍵產出 — 階段 2 · 未開始

- **方向**：一支 `scripts/` 腳本（和 admin 頁面上的對應按鈕），指定期間後產出一個壓縮檔：
  - 帳號與權限清單（含 service account 與擁有者）、AUD-2 的存取審查報表
  - 期間內的 `audit_log`（現有 CSV 匯出）
  - 設定變更歷史（CMP-4 完成後）
  - 備份紀錄、`restore-drill.log`、`drill.log`
  - IP allowlist 規則與期間內的變更
  - guardrail 週報、異常偵測結果
  - 最近一次 pip-audit / gitleaks 結果（從 CI 取得）
  - 一份索引頁：每個檔案對應 CMP-1 對照表的哪個控制項
- 匯出要進稽核紀錄（`audit._AUDITED_READS`），檔案內不得含 API key 或下游金鑰。

### AUD-2　存取審查報表 — 階段 2 · 未開始

- **現況**：資料都有（`is_disabled`、`can_use_azure` / `can_use_bedrock`、`concurrency_waived`、*Special access* 統計、key 發行與最後使用時間、舊格式 key 篩選），但沒有一份可以交給主管簽核的報表。
- **方向**：依部門（`org_code`）產出：每個帳號的權限、key 年齡、最後使用時間、N 天未使用、特殊權限、service account 擁有者；可寄給各部門主管確認（照 *Weekly reports* 的寄信規則：一封信、公司網域收件人、寫入 mail log）。確認結果（保留 / 移除）回寫並進稽核紀錄。

### AUD-3　風險登錄表 — 階段 2 · 未開始

- **方向**：`docs/compliance/risk-register.md`（或組織既有的風險管理工具）：每項風險的描述、可能性、衝擊、現有控制（連到程式）、殘餘風險、處置（降低 / 接受 / 移轉）、負責人、檢視日期。初始內容可以從本 roadmap 的缺口和 CLAUDE.md *Rules* 整理而來，例如：PG 單點故障（HA-1）、明文 key（CMP-2）、guardrail fail-open（GRD-8）、雲端流量未篩檢（GRD-3）、資料送往境外雲端（GRD-1）。

### AUD-4　AI 系統清冊與影響評估 — 階段 3 · 未開始

- **現況**：模型清單在 `config.toml`（`[models.*]`、`[azure_models.*]`、`[bedrock_models.*]`），對使用者的檢視在 `model_catalog`；沒有記錄模型來源、授權、用途限制、資料所在區域。
- **方向**：從設定自動產出清冊（別名、實際模型、後端、區域、上線日期、使用量），再加上人工維護的欄位（供應商、授權條款、資料是否離開公司、允許 / 禁止的用途、評估日期）。影響評估（ISO 42001 A.5、NIST MAP）以每個模型或每種用途一份，範本放在 `docs/compliance/`。

---

## 主題：用量計費與 chargeback

**目標**：每月可以依部門（成本中心）分攤 LLM 費用，數字可對帳、出帳後不會變。

**現況**：預算在本部門，只看報表、不向其他部門收費（showback），未來可能 by BU。所以整個主題在階段 3：確定要 by BU 看費用時先做 BIL-1 與 BIL-3（本部門付的是實際雲端帳單，估算與實際的差距值得先知道）；月結封帳、GPU 分攤、幣別換算等到確定要收費再做。

**已經具備的部分**：每個 `usage_logs` 列在請求當下就寫入 `cost_usd` 與 `backend`（地端 / Azure / AWS），之後改價格不影響已記錄的費用；`usage_daily` 彙總；admin 匯出 `usage.xlsx`（Summary、By Department（`org_code`）、By Service Account、Top 10 Users、Cost by Backend）與 `user-backend-costs.csv`；報表日與月都是 Asia/Taipei。

### BIL-1　成本中心對應 — 階段 3 · 未開始

- **決定（2026-11-14）**：`org_code` → BU 用另外維護的對照表（admin 維護或從人資系統匯入）。
- **現況**：只有 `users.org_code`，沒有部門名稱、上層部門、成本中心代碼。service account 列出擁有者，但費用該算給誰沒有定義。
- **方向**：一張 `org_code` → 成本中心 / 部門名稱 / 上層部門 的對照（從 AuthCenter 或人資系統同步，或 admin 維護）；service account 加一個「計費成本中心」欄位（預設為第一位擁有者的部門）。對照要有生效日期，部門改組時舊月份不受影響。

### BIL-2　月結 chargeback 報表與封帳 — 階段 3 · 未開始

- **現況**：匯出是即時計算，`--recompute` 或補資料後同一個月份的數字可能改變。
- **方向**：每月初自動「封帳」上個月：把各成本中心的用量與費用寫進一張不可修改的月結表（比照 `audit_log` 的 append-only），報表從月結表讀；事後更正以調整列（adjustment）處理，不改原列。月結完成後寄給財務與各部門（`scheduler.Job`，照 *Weekly reports* 的寄信規則）。費用的讀取一律走 `usage_rollup.aggregate`。

### BIL-3　雲端帳單對帳 — 階段 3 · 未開始

- **現況**：Azure / AWS 費用是 token × `config.toml` 的價格，是**估算**（週報已標示 *estimated*）。
- **方向**：每月匯入實際帳單（Azure Cost Management 匯出、AWS Cost and Usage Report），依部署 / 模型比對估算與實際，差異超過門檻時提醒；決定 chargeback 用估算還是按實際帳單比例分攤。

### BIL-4　地端 GPU 成本分攤模型 — 階段 3 · 未開始

- **現況**：地端模型的價格是名目價格（notional），沒有對應實際成本。
- **方向**：由財務決定分攤方式（例如 GPU 主機折舊 + 電費 + 維運，按 token 或按使用時段比例），換算成每個模型的每百萬 token 價格寫回設定；或地端只分攤不計價。這是治理決定，程式改動不大。

### BIL-5　價格變更歷史 — 階段 3 · 未開始

- **現況**：已記錄的費用不受改價影響，但「某段期間用的是什麼價格」只能從 `config.toml` 的現況推，沒有歷史。
- **方向**：併入 CMP-4 的設定版本；月結報表附上當月生效的價格表。

### BIL-6　部門預算與告警 — 階段 3 · 未開始

- **現況**：預算以每人每日（`daily_limit_usd`）與每人雲端上限為主。
- **方向**：成本中心的月預算，達 80% / 100% 時通知部門主管；是否硬性擋下要另外決定（硬擋會影響整個部門，建議先只通知）。

### BIL-7　幣別換算 — 階段 3 · 未開始

- **現況**：全部是 USD。
- **方向**：月結時以當月匯率（由財務提供）換算成新台幣，原始 USD 與匯率一併保存。

---

## 主題：AI 安全防護（guardrails、prompt injection、內容過濾、PII）

**目標**：在資料送出（特別是送往雲端）之前，攔下不該送出的內容；看得到並能處置不安全的使用。

**已經具備的部分**（`app/services/guardrail.py`，`docs/architecture.md` *Guardrail*）：Qwen3Guard-Gen 篩檢**地端流量**中**使用者最新一輪**的文字；門檻、各類別 `block` / `flag` / `ignore`、適用帳號與 endpoint 可設定；`monitor`（預設，背景執行不增加延遲）或 `block`（400）；fails open；判定進 Langfuse 並有每週 guardrail 報告（含 Jailbreak、PII 等類別的統計與要注意的帳號）。

### GRD-1　PII 偵測與遮罩 — 階段 2 · 未開始

- **決定（2026-11-14）**：第一版只做 `monitor`：類型與數量進 Langfuse 與週報，不記內容；先對送往雲端的請求。公司規定允許部分資料送雲端，所以偵測類別要對應規定中「不可送」的類別。觀察數週的命中與誤判後，再決定是否開 `mask` / `block`。
- **現況**：請求路徑沒有 PII 處理。Qwen3Guard 會回報 PII 類別，但只限地端流量、只看最新一輪，而且只判定、不遮罩。最需要保護的其實是**送往 Azure / AWS** 的資料（資料離開公司）。
- **方向**：
  - 偵測：規則（台灣身分證字號、統一編號、手機、email、信用卡號含 Luhn 檢查、銀行帳號）+ 可選的 NER 模型（例如 Presidio 類的方案）。先做 `monitor`：只記錄類型和數量進 Langfuse 與週報，不記內容。
  - 處置模式：`flag`（記錄）、`mask`（替換成 `[TW_ID]` 之類的標記後再送出）、`block`（400，告訴使用者哪一類資料不能送往雲端）；可以只對雲端後端生效。
  - 可還原的假名化（送出前換成代號、回應時換回）在 streaming 下很難做對，列為後續研究。
  - `LANGFUSE_CAPTURE_IO` 開啟時，寫進 Langfuse 的內容也要套同樣的遮罩。
- **要遵守的規則**：`detect` 類的程式在每個請求上跑，不能做 I/O、不能讓請求失敗；不得把被篩檢的文字寫進 log 或 Langfuse；遮罩不得改到 prefix caching 依賴的部分（system message、歷史訊息的位置）。

### GRD-2　提示中的機密偵測 — 階段 2 · 未開始

- **決定（2026-11-14）**：同 GRD-1，第一版只做 `monitor`。排在 GRD-1 之前做：規則式、誤判少、做得快。
- **現況**：無。使用者常把 API key、密碼、連線字串、私鑰貼進 prompt，或 Claude Code 讀到 `.env` 後送出。
- **方向**：用 gitleaks 類的規則（repo 已經在 CI 用 gitleaks，可共用規則來源）掃描請求內容，**包括 tool result**（機密最常從檔案內容進來）；模式同 GRD-1（monitor → mask / block），先只對雲端後端生效。命中只記規則名稱與數量。

### GRD-3　雲端流量的內容過濾 — 階段 2 · 未開始

- **決定（2026-11-14）**：用供應商原生過濾（Azure 內容篩選、Bedrock Guardrails），不把 Qwen3Guard 加到雲端路徑，CLAUDE.md 的規則維持不變。
- **現況**：CLAUDE.md 明定 guardrail **只**篩檢地端流量，不得加到 Azure / Bedrock 路徑（`test_cloud_traffic_is_not_screened`）。
- **方向**：優先用供應商原生的過濾，而不是把 Qwen3Guard 搬過去：Azure OpenAI 的內容篩選（content filtering）政策、Bedrock Guardrails（Converse 的 `guardrailConfig`）。gateway 端要做的是：設定哪些部署套用哪個政策、把供應商回傳的篩選結果記進 Langfuse 與 guardrail 週報、被擋時回給 client 一致的錯誤格式。如果決定要自己篩檢雲端流量，就要修改那條規則與測試。

### GRD-4　間接 prompt injection — 階段 3 · 未開始

- **決定（2026-11-14）**：先靠 client 端：實際的防護在 Claude Code 的權限設定，用 client rules 確保使用者開啟；在 CMP-1 對照表與 AUD-3 寫明 gateway 目前不偵測這一項。gateway 端的偵測模型往後放（需要 GPU，agent loop 請求量大）。
- **現況**：最新一輪是 tool result 時判定為 `skipped`（Claude Code 的 agent loop 大多是這種）；tool result、文件、圖片、system prompt 都不送 guard。也就是說，從網頁、檔案、工具輸出進來的注入指令（間接 prompt injection，OWASP LLM01）完全沒有偵測。
- **方向**：
  - 用專門的 prompt injection 分類模型（小型 classifier，延遲比 Qwen3Guard 低）篩檢 tool result 與文件內容，先 `monitor`。
  - Agent loop 的請求量大、tool result 長：只看新加入的那一輪、限制長度、取樣，並在週報看命中率與延遲再決定是否擴大。
  - 誠實寫明限制：gateway 能偵測，但實際的防護（不讓模型照著注入指令執行工具）在 client 端（Claude Code 的權限設定）。可以和 client rules 結合，提醒使用者開啟對應設定。

### GRD-5　輸出內容檢查 — 階段 3 · 未開始

- **現況**：只檢查 prompt，不檢查模型輸出。
- **方向**：`monitor` 模式在回應結束後於背景判定完整輸出（和現有的 `PendingVerdict` 機制同一套，Langfuse generation 等判定完成再送出）；`block` 模式只在非 streaming 請求可行；streaming 已送到 client 的內容收不回來，只能中途截斷，要另外設計並先評估對 client（Claude Code 的重試行為）的影響。

### GRD-6　公司自訂內容政策 — 階段 3 · 未開始

- **現況**：Qwen3Guard 的安全政策是固定的，只能調整 gateway 如何對判定結果採取動作。
- **方向**：admin 可維護的關鍵字 / regex 清單（例如機密專案代號、客戶名稱），以及依帳號群組限制的主題；命中動作同 GRD-1。注意 CLAUDE.md 對 client rules 的原則（判斷邏輯在程式、不在 admin 輸入）不適用於這裡，但清單本身可能就是機密（專案代號、客戶名稱），它的變更要進稽核紀錄，而且不能把清單內容寫進 log。

### GRD-7　從 monitor 切到 block 的條件 — 階段 2 · 未開始

- **方向**：訂出切換條件（例如連續 4 週誤判率低於某值，以 guardrail 週報與人工抽樣判斷），先對部分類別（如 Jailbreak）或部分帳號群組啟用 block；建立申訴流程（使用者被擋時可以回報誤判）。屬於治理決定，程式只需要依類別、依帳號群組設定（大多已支援）。

### GRD-8　Guardrail 失敗時的處置 — 階段 2 · 已決定（待寫入 AUD-3）

- **決定（2026-11-14）**：維持 fail-open（方向 a），寫成風險接受放進 AUD-3，理由是可用性優先；以週報的「guard 沒在判定 ≥ 5%」監控。不改程式、不改規則。
- **現況**：guardrail 在 `block` 模式也 fail open（逾時、錯誤、無法解析都放行），這是 CLAUDE.md 的規則（`test_block_mode_fails_open_on_timeout`）。Guard 模型目前也沒有 HA。
- **方向**：(a) 維持 fail-open，寫進風險登錄表（AUD-3）作為風險接受，並監控 fail-open 的比例（週報已有「guard 沒在判定 ≥ 5%」的提醒）；(b) 或為特定類別 / 特定後端（例如雲端 + PII）提供 fail-closed 選項，需修改規則與測試。Guard 模型本身多台部署併入 HA 主題。

---

## 已完成

（尚無）
