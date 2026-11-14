# 功能盤點清單

範圍：計費與 chargeback、guardrails、prompt injection 防護、內容過濾、PII 偵測遮罩、稽核與風險文件。每一列是一項能力，標出現在有沒有。標記說明見 [README](README.md)。

## 1. 用量計費與 chargeback

現況：預算在本部門，只看報表、不收費（showback）；未來可能 by BU。

| 能力 | 狀態 | 現況 / 證據 | 待辦 |
|---|---|---|---|
| 每個請求記錄 token 與費用 | ✅ | `usage_logs.cost_usd`，請求當下依設定的價格計算；之後改價不影響舊資料 | |
| 費用依後端區分（地端 / Azure / AWS） | ✅ | `usage_logs.backend` | |
| 快取 token 的價格 | ✅ | `cached_input_price_per_1m` | |
| 每日彙總，報表不掃明細 | ✅ | `usage_daily`、`usage_rollup.aggregate` | |
| 使用者看自己的用量 | ✅ | dashboard *Usage*；`GET /v1/usage` | |
| Admin 月報表 | ✅ | `/admin` 月檢視：總計、排行、各部門 | |
| 匯出 | ✅ | `usage.xlsx`（Summary、By Department、By Service Account、Top 10、Cost by Backend）、`user-backend-costs.csv` | |
| 每週用量信 | ✅ | `weekly_reports`：各後端 token、估算費用、8 週趨勢 | |
| 依部門（`org_code`）彙總 | ✅ | 匯出的 *By Department* | |
| 依 BU 彙總 | ⬜ | | BIL-1（已決定：另外維護對照表） |
| service account 的費用歸屬 | 🟡 | 報表列出擁有者；沒有定義算給誰 | BIL-1 |
| 月結封帳（出帳後數字不變） | ⬜ | 報表是即時計算的 | BIL-2 |
| 自動寄月報給各部門 | ⬜ | | BIL-2 |
| 和雲端實際帳單對帳 | ⬜ | 雲端費用是 token × 設定價格的估算 | BIL-3 |
| 地端模型的真實成本 | ⬜ | 地端價格是名目價格 | BIL-4 |
| 價格變更歷史 | ⬜ | | BIL-5（併入 CMP-4） |
| 每人每日預算 | ✅ | `daily_limit_usd`；超過回 429 與 `Retry-After` | |
| 每人雲端上限 | ✅ | `cloud_daily_limit_usd`，Azure + AWS 合計 | |
| 部門 / BU 預算與告警 | ⬜ | | BIL-6 |
| 整體月預算的追蹤（本部門的總預算用了多少） | ⬜ | 看得到本月總額，沒有預算線與告警 | 併入 BIL-6；預算在本部門，這一項現在就有用 |
| 新台幣換算 | ⬜ | 全部 USD | BIL-7 |

## 2. Guardrails 與內容過濾

現況：地端 Qwen3Guard 已開啟，monitor 模式。

| 能力 | 狀態 | 現況 / 證據 | 待辦 |
|---|---|---|---|
| 地端流量的 prompt 安全篩檢 | ✅ | Qwen3Guard-Gen，使用者最新一輪的文字 | |
| monitor 模式（不增加延遲） | ✅ | 背景執行 | |
| block 模式 | ✅ | 功能有（回 400），目前沒開 | GRD-7 |
| 依類別設定 block / flag / ignore | ✅ | `category_rules` | |
| 依帳號、endpoint 設定範圍與豁免 | ✅ | `apply_to`、`exempt_users`、`endpoints` | |
| 判定紀錄與每週報告 | ✅ | Langfuse 分數；guardrail 週報（類別、每日數量、要注意的帳號） | |
| Admin 測試連線 | ✅ | *Guardrail* 卡片 | |
| 雲端流量的內容過濾 | ⬜ | 刻意不用 Qwen3Guard 篩雲端 | GRD-3（已決定：用供應商原生過濾並記錄結果） |
| 圖片、檔案內容的篩檢 | ⬜ | 只看文字 | 未排 |
| 歷史訊息、system prompt 的篩檢 | ➖ | 刻意只看最新一輪（舊的當時已經看過） | |
| 輸出內容檢查 | ⬜ | | GRD-5 |
| 公司自訂政策（關鍵字、主題） | ⬜ | Qwen3Guard 的政策是固定的 | GRD-6 |
| 誤判申訴 | ⬜ | | GRD-7、N-8 |
| Guard 模型不可用時的處置 | ✅ | fail-open（已決定維持，風險接受） | AUD-3 |
| Guard 模型的備援 | ⬜ | 單一位址 | HA-11 階段 |
| 換用其他 guard 模型 | 🟡 | 要改寫 `build_guard_request` / `parse_qwen3guard` | 未排 |

## 3. Prompt injection 防護

| 能力 | 狀態 | 現況 / 證據 | 待辦 |
|---|---|---|---|
| 直接攻擊（jailbreak）偵測 | 🟡 | Qwen3Guard 的 Jailbreak 類別；地端、monitor | GRD-3、GRD-7 |
| 間接注入偵測（tool result、網頁、檔案） | ⬜ | tool result 一律略過 | GRD-4（已決定：先靠 client 端） |
| 專用的注入分類模型 | ⬜ | | GRD-4 |
| system 訊息的完整性 | ✅ | 中途的 system 訊息不會被提升 | |
| Client 端安全設定的檢查與提醒 | 🟡 | client rules：CC001（auto mode）、CC003（版本過舊）等，含修正用的設定 patch | 視需要加新的 check |
| 重複攻擊的帳號識別 | ✅ | guardrail 週報：≥ 2 次 jailbreak 會列出 | CMP-5（接到事件流程） |

## 4. PII 與機密的偵測、遮罩

現況：公司規定允許部分資料送雲端；目前靠使用者自律。

| 能力 | 狀態 | 現況 / 證據 | 待辦 |
|---|---|---|---|
| Log 不含 prompt | ✅ | `redact.summarize_body` | |
| Langfuse 預設不存 prompt | ✅ | `LANGFUSE_CAPTURE_IO` 預設關 | |
| Admin 頁面遮罩 API key | ✅ | `mask_api_key` | |
| 稽核紀錄遮蔽機密欄位 | ✅ | `audit.redact` | |
| PII 類別的判定（粗略） | 🟡 | Qwen3Guard 會回報 PII 類別；地端、最新一輪、只判定 | GRD-1 |
| PII 規則偵測（身分證字號、統編、手機、email、信用卡） | ⬜ | | GRD-1（已決定：第一版 monitor，先看雲端） |
| NER 模型偵測（姓名、地址） | ⬜ | | GRD-1 |
| 送出前遮罩（mask） | ⬜ | | GRD-1 第二步 |
| 送出前阻擋（block） | ⬜ | | GRD-1 第二步 |
| 可還原的假名化 | ⬜ | streaming 下很難做對 | 研究項目 |
| 機密偵測（API key、密碼、私鑰、連線字串） | ⬜ | | GRD-2（已決定：第一版 monitor） |
| tool result 裡的機密（讀到 `.env`） | ⬜ | | GRD-2 |
| Langfuse 存 prompt 時的遮罩 | ⬜ | | GRD-1 |
| 偵測結果的報表 | ⬜ | | GRD-1（併入 guardrail 週報） |
| 依資料分級決定可以送哪個模型 | ⬜ | gateway 不知道資料等級；只能依帳號開關雲端權限 | 研究項目，需要公司的分級規定 |

## 5. 稽核與風險文件

現況：沒有認證時程，要能回應內部稽核。

| 能力 | 狀態 | 現況 / 證據 | 待辦 |
|---|---|---|---|
| 管理操作的稽核紀錄 | ✅ | `audit_log`：append-only、涵蓋 `/admin` 與 `/dashboard` 的所有寫入、key 顯示、匯出 | |
| 稽核紀錄查詢與匯出 | ✅ | admin *Audit Log* 卡片、JSON API、CSV（365 天） | |
| 寄信紀錄 | ✅ | `client_mail_log` | |
| 備份與還原演練紀錄 | ✅ | `restore-drill.log`、`drill.log` | |
| Key 的發行與最後使用時間 | ✅ | `users.api_key_created_at` / `api_key_last_used_at` | |
| 特殊權限一覽 | ✅ | admin *Special access* | |
| 控制項對照表 | 🟡 | 本目錄的四份清單是初版 | CMP-1：補上 🏢 / ❓ 的項目 |
| 存取審查報表（可交給主管簽核） | ⬜ | | AUD-2 |
| 稽核證據包一鍵產出 | ⬜ | | AUD-1 |
| 風險登錄表 | ⬜ | 已有三項風險接受待記錄 | AUD-3 |
| AI 系統清冊 | ⬜ | | AUD-4 |
| 影響評估範本 | ⬜ | | AUD-4 |
| 設定變更歷史（改前改後的值） | ⬜ | 稽核只記欄位名稱 | CMP-4 |
| 事件紀錄 | ⬜ | | CMP-5 |
| 資料保存期限表 | ⬜ | | CMP-6 |
| API 端操作的稽核 | ➖ | API 端點全部唯讀或是模型請求（記在 `usage_logs`）；會改資料的操作都在網頁端 | |

## 6. 存取控制（其他框架清單會連過來的功能）

| 能力 | 狀態 | 現況 / 證據 | 待辦 |
|---|---|---|---|
| SSO 登入、admin 權限來自 token | ✅ | `auth.py` | |
| 終端工具以 SSO 換取 key | ✅ | `POST /auth/api-key` | |
| IP allowlist（依帳號 / 部門 / 時間） | ✅ | 預設 `off`。❓ 正式環境的模式 | |
| 失敗認證 throttle | ✅ | 每個 worker 各算 | HA-5 |
| 雲端權限逐帳號開啟 | ✅ | `can_use_azure` / `can_use_bedrock` | |
| 帳號停用 | ✅ | `is_disabled`（手動） | |
| 離職者自動停用 | ⬜ | | N-1 |
| 長期未用的 key 自動停用 | ⬜ | | CMP-3（已決定要做） |
| Key 強制效期 | ⬜ | | CMP-3（已決定先不做） |
| Key 以 hash 儲存 | ⬜ | | CMP-2（已決定：風險接受） |
| 下游金鑰加密存放 | ⬜ | | CMP-2 |
