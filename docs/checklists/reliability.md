# 系統可靠度盤點清單

範圍：讓服務持續可用所需要的每一件事，不只是 HA。標記說明見 [README](README.md)。現況是 2026-11-14 的單台部署。

## A. 單點故障

每一個「它壞了會怎樣」。

| 元件 | 壞了會怎樣 | 狀態 | 現況 / 待辦 |
|---|---|---|---|
| Gateway 主機 | 全部停止 | ⬜ | 單台。→ HA-3（冷備）、HA-10 |
| Gateway 程序 | 該主機上的請求失敗 | 🟡 | 2 個 worker；systemd `Restart=on-failure`。重啟時進行中的 stream 會斷 → HA-4 |
| PostgreSQL | API key 認證、計費、lease 全部失敗，等於全部停止 | ⬜ | 單一容器、沒有 replica。→ HA-1 |
| nginx / oauth2-proxy | nginx 壞：全部停止。oauth2-proxy 壞：網頁登不進去，API 不受影響 | ⬜ | 和 gateway 同一台。→ HA-3 |
| AuthCenter | 網頁無法登入、`POST /auth/api-key` 無法換 key；**已有 key 的 API 請求不受影響**（JWT 公鑰是本機檔案） | 🟡 | 外部相依，gateway 無法改善。公鑰更換的流程 → N-10 |
| TLS 憑證到期 | 全部 client 連線失敗 | ⬜ | 沒有到期監控。→ N-10 |
| 單一 vLLM 主機 | 該主機上的模型不可用 | 🟡 | 有同類型主機時自動 failover（連不上或 502 / 503 / 504，一次）；30 秒健康檢查。❓ 哪些模型類型只有一台 |
| Azure / Bedrock | 該雲端模型不可用或被限流 | 🟡 | 429 時可在同一個後端換模型（`rate_limit_fallback`，最多 3 次）。雲端沒有健康檢查；不會跨後端切換（刻意的） |
| Guard 模型（Qwen3Guard） | 不影響請求 | ✅ | fail-open，3 秒逾時；週報會指出「guard 沒在判定 ≥ 5%」。風險接受 → AUD-3 |
| Langfuse | 不影響請求；延遲、錯誤率、guardrail 判定在那段期間沒有紀錄，週報少幾個區塊 | ✅ | 非同步送出，失敗被吞掉 |
| Exchange（寄信） | 不影響請求；提醒信與週報延後 | ✅ | 分批寄送、失敗進 mail log、週報 6 小時後重試 |
| 設定檔損毀（`config.toml`） | 手動改壞：啟動時報錯，gateway 起不來。執行中改壞：❓ | 🟡 | admin 頁面存檔前會驗證；手動編輯沒有保護。→ CMP-4（版本與回滾）、N-11 |
| 磁碟滿 | PG 停止寫入 → 全部停止 | ⬜ | log 會輪替（100 MB、14 天）；`pgdata`、WAL、備份（30 份每日）沒有監控。→ N-2、CMP-6 |

## B. 偵測與告警

| 項目 | 狀態 | 現況 / 待辦 |
|---|---|---|
| 存活 / 就緒探針 | ✅ | `/healthz`、`/readyz`（只檢查資料庫，刻意的） |
| 指標 | 🟡 | Prometheus `/metrics` 要設 `METRICS_ENABLED=true`。❓ 確認正式環境有開、有人在抓 |
| **告警規則** | ⬜ | repo 裡沒有。至少需要：gateway 不 ready、5xx 比例、PG 連不上、vLLM 主機 DOWN、連線池 ≥ 80%（目前只有 log）。→ N-2 |
| 排程工作失敗 | 🟡 | `scheduled_jobs.last_status` 會記錄、log 有 `Scheduled job ... failed`；admin 頁面只顯示週報的上次結果，其他工作要查資料表或 log；沒有通知。→ N-2 |
| 備份失敗 | 🟡 | `backup-db.sh` 失敗時非 0 結束並寫 stderr；有沒有人收到取決於 cron 的設定。❓ → N-2 |
| 複寫延遲 / 中斷 | ⬜ | 還沒有複寫。HA-1 要一起做 |
| 磁碟、憑證到期 | ⬜ | → N-2、N-10 |
| 慢請求 | 🟡 | 超過 `SLOW_REQUEST_WARN_S` 寫警告到 log；Langfuse 有各階段耗時 |
| 用量異常 | ✅ | 每小時掃描，admin 頁面顯示（不通知） |
| 每週檢視 | ✅ | 用量週報的 *Worth a look*（模型失敗率 ≥ 5%、p95 比上週慢 1.5 倍） |

## C. 容量

| 項目 | 狀態 | 現況 / 待辦 |
|---|---|---|
| DB 連線數 | 🟡 | 每個 worker 最多 35 條（`pool_size=20` + `max_overflow=15`），2 個 worker = 70；PG `max_connections=200`。加第二台 gateway 後是 140，再加複寫與管理連線，餘裕不多。→ N-9 |
| 下游連線數 | 🟡 | 地端 1000、雲端 200（可用 `DOWNSTREAM_*`、`CLOUD_*` 調整）；每 30 秒記錄使用率 |
| 單台能撐多少並行 stream | ⬜ | 沒有壓測。→ N-9 |
| 每人併發上限 | 🟡 | 功能有，預設 `off`。❓ 正式環境的模式 |
| GPU 滿載時的行為 | 🟡 | 看得到排隊數（dashboard）；gateway 不會主動拒絕或分流 |
| 資料成長 | 🟡 | `usage_logs` 每個請求一筆、目前永久保存。→ CMP-6（已決定保存 1 年）。報表走 `usage_daily`，不受明細量影響 |
| worker 數 | 🟡 | 預設 2（`GATEWAY_WORKERS` / systemd unit）。❓ 是否依主機核心數調整過 |

## D. 變更與部署

| 項目 | 狀態 | 現況 / 待辦 |
|---|---|---|
| 部署順序 | ✅ | 先 `alembic upgrade head` 再重啟；容器版 `migrate` 一定先跑完 |
| Migration 安全性 | ✅ | `lock_timeout`、常數預設值；規定要在真的 PostgreSQL 上驗證升級 → 降級 → 升級 |
| 不中斷部署 | ⬜ | 重啟會中斷進行中的 stream（systemd 90 秒後強制結束）。→ HA-4 |
| 回滾程序 | 🟡 | migration 可降級；沒有寫成步驟（程式版本 + 資料庫版本 + 設定） |
| 測試環境 | ⬜ | → N-4 |
| CI | ✅ | ruff、pytest、鎖定相依版本；安全掃描另一條 workflow |
| 設定變更可回復 | ⬜ | → CMP-4 |
| 兩台版本一致 | ⬜ | 冷備上線後才有這個問題。→ HA-10 |

## E. 備份與復原

| 項目 | 狀態 | 現況 / 待辦 |
|---|---|---|
| 資料庫備份 | ✅ | 線上 `pg_dump`、備份後驗證、GPG 公鑰加密、30 份每日 + 12 份每月 |
| 異地複製 | 🟡 | `BACKUP_COPY_TO`（rsync）是選項。❓ 確認有設定，而且目的地不是同一台、同一個機房 |
| 還原演練 | ✅ | `restore-drill.sh`，每次都寫紀錄。❓ 多久做一次、上次是什麼時候 |
| 設定檔備份 | ⬜ | → N-11 |
| 加密私鑰的保管 | ❓ | 私鑰遺失 = 所有加密備份作廢；文件要求兩份分開保存 |
| RPO / RTO | ⬜ | 沒有定義。每日備份代表最多掉一天的用量紀錄，HA-1 完成後降到幾秒。→ N-3 |
| 冷備切換 | ⬜ | → HA-10、HA-9 |

## F. 請求路徑的韌性（已經做到的，改程式時別弄壞）

| 項目 | 狀態 | 位置 |
|---|---|---|
| 逾時有上限（等 header、非 stream 280 秒、stream 最長閒置 300 秒） | ✅ | `_STREAM_TIMEOUT`、`anthropic_adapter.py` |
| Stream 心跳（10 秒 ping），避免中間設備切斷 | ✅ | `anthropic_adapter.py` |
| Client 斷線時釋放連線與 lease | ✅ | `_release_stream`、`test_stream_disconnect_cleanup.py` |
| Stream 期間不佔 DB 連線 | ✅ | `test_auth_session_scope.py` |
| 計費、Langfuse、指標、併發計數失敗都不影響請求 | ✅ | CLAUDE.md *Side effects never break a request* |
| 健康檢查用獨立的連線池，不被流量擠掉 | ✅ | `server_state.get_health_client` |
| 健康狀態不抖動（連續 2 次失敗才標 DOWN） | ✅ | `health.py` |
| 排程工作不會重複執行、不會把例外丟進主程式 | ✅ | `scheduler.py` |
| 計費寫入失敗時用量會遺失 | 🟡 | 只寫一行 `Failed to log usage` 到 log，沒有重試或補寫。資料庫短暫中斷期間的請求不會被計費。→ 和 N-2 一起（對這行 log 發告警） |
| 資料庫中斷時 API 認證失敗 | ⬜ | 沒有認證快取（刻意的：停用帳號、額度要即時生效）。→ HA-1 |

## G. 目標與量測

| 項目 | 狀態 | 現況 / 待辦 |
|---|---|---|
| 可用率目標（SLO） | ⬜ | → N-3 |
| 錯誤率、延遲目標 | ⬜ | 有量測（Langfuse、週報），沒有目標值。→ N-3 |
| 切換時間目標（RTO） | ⬜ | HA-9 演練量出實際值後訂。→ N-3 |
| 事後檢討 | ⬜ | → CMP-5 |

## H. 操作文件

| 文件 | 狀態 | 位置 / 待辦 |
|---|---|---|
| 安裝與部署 | ✅ | `deploy/README.md` |
| 容器化與搬遷 | ✅ | `deploy/migration/README.md`（含演練步驟 1 ~ 7） |
| 備份與還原 | ✅ | `deploy/README.md`、`restore-drill.sh` |
| 冷備切換 | ⬜ | → HA-10 |
| 常見故障排除（vLLM DOWN、連線池滿、雲端 429、登入失敗） | ⬜ | 知識散在 `docs/architecture.md` 和 CLAUDE.md，沒有一份「出事時先看這裡」 |
| 事件處理 | ⬜ | → CMP-5 |
