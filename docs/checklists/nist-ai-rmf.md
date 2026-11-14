# NIST AI RMF 1.0 盤點清單

範圍：四個功能（GOVERN、MAP、MEASURE、MANAGE）的各類別，gateway 能提供什麼。AI RMF 是自願性框架，沒有認證；它的用處是當作討論風險時的共同語言。生成式 AI 的補充文件是 NIST AI 600-1。

標記說明見 [README](README.md)。類別說明是依記憶整理的中文摘要，引用前請對照原文。

## GOVERN　治理

| 類別 | 狀態 | Gateway 現況 / 證據 | 待辦 |
|---|---|---|---|
| GOVERN 1 政策、流程、程序已建立並落實 | 🏢 | gateway 把部分政策變成強制：預算、雲端權限、IP allowlist、guardrail | AI 使用政策 ❓；CMP-1 |
| GOVERN 2 問責架構 | 🏢 | admin 操作都留在 `audit_log` | 寫明各項決定的負責人（模型上架、guardrail 政策、風險接受） |
| GOVERN 3 人力多元與可及性 | 🏢 | | |
| GOVERN 4 風險文化 | 🏢 | client rules 與 dashboard 提醒是對使用者的溝通管道 | |
| GOVERN 5 與相關 AI 行為者的互動 | ⬜ | | CMP-11（使用者回報管道） |
| GOVERN 6 第三方軟體、資料與供應鏈 | 🟡 | 相依套件掃描（pip-audit、dependabot）；雲端供應商的金鑰與區域 | AUD-4（供應商條款、模型來源與授權）；CMP-9 |

## MAP　釐清情境

| 類別 | 狀態 | Gateway 現況 / 證據 | 待辦 |
|---|---|---|---|
| MAP 1 情境已建立 | ⬜ | 沒有寫下系統用途、使用者、不該用在哪裡 | AUD-4 |
| MAP 2 AI 系統分類 | 🟡 | 模型依後端與類型分（`config.toml`、`model_catalog`） | AUD-4 加上風險等級與資料去向 |
| MAP 3 能力、用途、效益與成本 | 🟡 | 成本有（`usage_logs`、週報）；效益沒有量測 | |
| MAP 4 所有元件（含第三方）的風險已對應 | ⬜ | | AUD-3（風險登錄表） |
| MAP 5 對個人、群體、社會的影響 | ⬜ | | AUD-4（影響評估） |

## MEASURE　量測

| 類別 | 狀態 | Gateway 現況 / 證據 | 待辦 |
|---|---|---|---|
| MEASURE 1 選定並採用量測方法與指標 | 🟡 | 已有指標：延遲（p50 / p95、TTFT）、錯誤率、guardrail 判定、用量、異常偵測 | REL-2（訂目標值） |
| MEASURE 2 評估可信賴特性 | 🟡 | 安全：Qwen3Guard（地端、使用者最新一輪、monitor）。可靠：健康檢查、failover。隱私：log 遮蔽 | 雲端沒有篩檢（GRD-3）；輸出沒有檢查（GRD-5）；PII（GRD-1）；模型品質評估（AUD-5） |
| MEASURE 3 持續追蹤已識別的風險 | 🟡 | 每週 guardrail 報告、每小時異常偵測 | AUD-3 讓每項風險有檢視日期；REL-1 |
| MEASURE 4 量測成效的回饋 | ⬜ | 沒有誤判回報；guardrail 的準確度沒有人工抽樣 | GRD-7；CMP-11 |

## MANAGE　管理

| 類別 | 狀態 | Gateway 現況 / 證據 | 待辦 |
|---|---|---|---|
| MANAGE 1 風險排序、回應與處理 | 🟡 | roadmap 有排序；已決定的風險接受有三項 | AUD-3 |
| MANAGE 2 效益最大化、負面影響最小化的策略 | 🟡 | 可用的手段：停用帳號、模型下架、guardrail `block`、預算上限、IP allowlist | GRD-7（何時從 monitor 切到 block）；CMP-5 |
| MANAGE 3 第三方風險管理 | 🟡 | 雲端限流時的同後端 fallback；雲端權限逐帳號開啟；雲端費用上限 | AUD-4；GRD-1、GRD-2（送出前檢查） |
| MANAGE 4 風險處置、回應、復原與溝通計畫 | ⬜ | 備份與還原演練有；事件處理與對使用者的溝通沒有 | CMP-5；HA-10；REL-2 |

## 生成式 AI profile（NIST AI 600-1）列出的風險，和 gateway 的關係

| 風險 | 和 gateway 的關係 | 對應 |
|---|---|---|
| 資料隱私 | prompt 可能含個資並送往雲端 | GRD-1、CMP-6 |
| 資訊安全（prompt injection、資料外洩） | 間接注入沒有偵測；機密可能隨 prompt 送出 | GRD-2、GRD-4 |
| 危險或不當內容 | 地端有 guardrail；雲端靠供應商 | GRD-3、GRD-5 |
| 虛構內容（confabulation） | gateway 無法判斷輸出正確性 | 使用政策 🏢 |
| 智慧財產 | 輸出是否侵權、輸入是否含他人著作，gateway 不判斷 | 使用政策 🏢；AUD-4 記錄模型授權 |
| 價值鏈與元件整合（第三方） | Azure、AWS、開源模型 | AUD-4 |
| 人與 AI 的互動配置（過度信任） | client 端 | 使用政策 🏢 |
| 有害偏見、資訊完整性、環境影響、CBRN、不雅內容 | 內部開發與文件用途下風險低；guardrail 的類別涵蓋一部分 | AUD-4 寫明判斷 |
