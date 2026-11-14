# NIST AI RMF 1.0

每個類別先列 NIST 的英文原文，再說明這個 gateway 現在的狀態與缺口。標記說明見 [README](README.md)。

**關於文字來源**：

- 引文是 *Artificial Intelligence Risk Management Framework (AI RMF 1.0)*（NIST AI 100-1，2023 年 1 月）第 5 節的類別敘述。NIST 的出版品是美國政府著作，可以自由引用。
- **這些引文是憑記憶寫的，還沒有逐字核對。** 寫這份文件的環境連不上 `nist.gov`。交出去之前，請對照 [NIST AI 100-1](https://doi.org/10.6028/NIST.AI.100-1) 的 Table 1 ~ 4 核對一次。
- 這裡只到「類別」這一層（19 個）。每個類別底下還有子類別（共 72 個），需要時再展開。

**AI RMF 是什麼**：自願性的框架，沒有認證。它把 AI 風險管理分成四個功能：GOVERN（治理）、MAP（釐清情境）、MEASURE（量測）、MANAGE（管理）。它的用處是當作討論風險時的共同語言，以及檢查有沒有漏掉哪一塊。

**範圍**：框架是寫給整個組織的，大部分類別是組織層級。下面說的是 gateway 在每個類別裡能提供什麼。

## 總覽

| 功能 | 結論 |
|---|---|
| GOVERN | 幾乎都是組織層級。gateway 把一部分政策變成強制，並留下紀錄 |
| MAP | 最弱的一塊：系統的用途、分類、風險、影響都沒有寫下來 → AUD-3、AUD-4 |
| MEASURE | 指標很齊（延遲、錯誤率、guardrail 判定、用量），但沒有目標值，也沒有驗證量測準不準 → REL-2、GRD-7 |
| MANAGE | 處置的手段都有，沒有寫成流程 → CMP-5 |

---

## GOVERN　治理

### GOVERN 1　🏢

> Policies, processes, procedures, and practices across the organization related to the mapping, measuring, and managing of AI risks are in place, transparent, and implemented effectively.

- **意思**：AI 風險管理的政策與流程已經建立、公開、有效落實。
- **現況**：政策屬於組織層級。gateway 把其中一部分變成系統會強制的規則：每人預算、雲端權限、IP 限制、安全篩檢。這些設定的每次變更都留在稽核紀錄裡。
- **缺口**：確認公司有沒有 AI 使用政策（CMP-1）；四份清單就是「實際做法」的書面紀錄。

### GOVERN 2　🏢

> Accountability structures are in place so that the appropriate teams and individuals are empowered, responsible, and trained for mapping, measuring, and managing AI risks.

- **意思**：權責分明，負責的人有權限、有責任、受過訓練。
- **現況**：系統內的權責很單純：admin 由 AuthCenter 授予，所有管理操作可追溯到個人。誰負責決定模型上架、guardrail 政策、風險接受，沒有寫下來。
- **缺口**：AUD-3（每項風險的負責人）、AUD-5（上架的核准人）。

### GOVERN 3　🏢

> Workforce diversity, equity, inclusion, and accessibility processes are prioritized in the mapping, measuring, and managing of AI risks throughout the lifecycle.

- **意思**：風險管理過程納入多元觀點。
- **現況**：組織層級，和 gateway 無關。

### GOVERN 4　🏢

> Organizational teams are committed to a culture that considers and communicates AI risk.

- **意思**：團隊有重視並溝通 AI 風險的文化。
- **現況**：gateway 提供對使用者的溝通管道：dashboard 的提醒、終端機裡的提醒、信件。目前用在 client 設定不安全的時候。
- **缺口**：無（程式面）。

### GOVERN 5　⬜

> Processes are in place for robust engagement with relevant AI actors.

- **意思**：有機制蒐集使用者與受影響者的回饋，並納入決策。
- **現況**：沒有。使用者遇到模型輸出有問題、被誤擋、有安全疑慮時，沒有固定的回報入口。
- **缺口**：CMP-11。

### GOVERN 6　🟡

> Policies and procedures are in place to address AI risks and benefits arising from third-party software and data and other supply chain issues.

- **意思**：第三方軟體、資料與供應鏈的風險有政策與程序處理。
- **現況**：軟體相依套件管得好：版本鎖定、每週弱點掃描、自動更新 PR。模型這一段沒有：開源權重的來源與授權、Azure / AWS 的資料處理條款，都沒有紀錄。
- **證據**：`.github/workflows/security.yml`；`uv.lock`。
- **缺口**：AUD-4、AUD-5、CMP-9。

---

## MAP　釐清情境

### MAP 1　⬜

> Context is established and understood.

- **意思**：系統的用途、使用者、適用的法規、不該用在哪裡，都已釐清並寫下來。
- **現況**：沒有寫下來。實際情況是：內部員工使用，主要用途是寫程式（Claude Code）與一般問答，另有內部應用透過 service account 呼叫。
- **缺口**：AUD-4。

### MAP 2　🟡

> Categorization of the AI system is performed.

- **意思**：系統做什麼任務、用什麼方法，已經分類。
- **現況**：模型依後端（地端、Azure、AWS）與類型（LLM、VLM、embedding、rerank 等）分類。沒有依風險或資料去向分類。
- **證據**：`config.toml`；`model_catalog.py`。
- **缺口**：AUD-4（加上資料是否離開公司、風險等級）。

### MAP 3　🟡

> AI capabilities, targeted usage, goals, and expected benefits and costs compared with appropriate benchmarks are understood.

- **意思**：了解系統的能力、目標用途、預期效益與成本。
- **現況**：成本很清楚：每個請求的 token 與費用、每週報告、各部門的統計。效益沒有量測。
- **證據**：`usage_logs`；`weekly_reports.py`。
- **缺口**：效益怎麼衡量是組織層級的問題。

### MAP 4　⬜

> Risks and benefits are mapped for all components of the AI system including third-party software and data.

- **意思**：系統每個元件（含第三方）的風險都已對應。
- **現況**：沒有風險登錄表。已知的風險散在 roadmap 與這幾份清單裡。
- **缺口**：AUD-3。

### MAP 5　⬜

> Impacts to individuals, groups, communities, organizations, and society are characterized.

- **意思**：對個人、群體、組織與社會的影響已經描述。
- **現況**：沒有。
- **缺口**：AUD-4（影響評估）。

---

## MEASURE　量測

### MEASURE 1　🟡

> Appropriate methods and metrics are identified and applied.

- **意思**：選定並採用合適的量測方法與指標。
- **現況**：指標已經在收集：每個模型的延遲（p50、p95、首字時間）、錯誤率、guardrail 判定與類別、用量與費用、用量異常。都沒有目標值，所以數字出來後沒有「合格或不合格」可言。
- **證據**：`observability.py`；`report_content.py`；`anomaly_scan.py`。
- **缺口**：REL-2。

### MEASURE 2　🟡

> AI systems are evaluated for trustworthy characteristics.

- **意思**：針對各項可信賴特性評估系統：安全、可靠、隱私、公平、可解釋等。
- **現況**：
  - 安全：地端流量的 prompt 有 Qwen3Guard 判定（只記錄）。雲端流量沒有；模型輸出沒有。
  - 可靠：模型主機有健康檢查與自動 failover。
  - 隱私：log 與觀測工具預設不存 prompt。送出的內容沒有個資檢查。
  - 模型品質、公平性：沒有評估。
- **證據**：`guardrail.py`；`health.py`；`redact.py`。
- **缺口**：GRD-3（雲端）、GRD-5（輸出）、GRD-1（個資）、AUD-5（上線前評估）。

### MEASURE 3　🟡

> Mechanisms for tracking identified AI risks over time are in place.

- **意思**：有機制持續追蹤已識別的風險。
- **現況**：每週的 guardrail 報告和前一週比較，並指出新出現的類別與要注意的帳號；每小時掃描用量異常。但這些只在有人看報告時才有用，沒有即時通知，也沒有對應到一張風險清單。
- **證據**：`weekly_reports.py`；`anomaly_scan.py`。
- **缺口**：AUD-3（讓每項風險有檢視日期）、REL-1（告警）。

### MEASURE 4　⬜

> Feedback about efficacy of measurement is gathered and assessed.

- **意思**：蒐集並評估「量測本身準不準」的回饋。
- **現況**：沒有。guardrail 的判定沒有人工抽樣核對，使用者也無法回報誤判，所以不知道誤判率。
- **缺口**：GRD-7（切到 block 之前要先知道誤判率）、CMP-11。

---

## MANAGE　管理

### MANAGE 1　🟡

> AI risks based on assessments and other analytical output from the MAP and MEASURE functions are prioritized, responded to, and managed.

- **意思**：依評估結果排定風險的優先順序並處理。
- **現況**：roadmap 有排序與決策紀錄。已決定接受的風險有三項：API key 明文儲存、guardrail 失敗時放行、間接 prompt injection 不在 gateway 偵測。這些還沒寫進正式的風險登錄表。
- **證據**：`docs/roadmap.md` 的「已定的決策」。
- **缺口**：AUD-3。

### MANAGE 2　🟡

> Strategies to maximize AI benefits and minimize negative impacts are planned, prepared, implemented, documented, and informed by input from relevant AI actors.

- **意思**：有規劃好、寫下來的策略，來擴大效益、降低負面影響，包括必要時停用系統的機制。
- **現況**：手段都有：停用帳號、下架模型、把 guardrail 從記錄改成阻擋、調整預算與上限、限制來源 IP。都是 admin 在頁面上就能做的。沒有寫成「什麼情況用哪一個」。
- **證據**：admin 頁面；`guardrail.py` 的 `block` 模式。
- **缺口**：GRD-7、CMP-5。

### MANAGE 3　🟡

> AI risks and benefits from third-party entities are managed.

- **意思**：來自第三方的風險有在管理。
- **現況**：對雲端供應商的管控：誰能用要逐帳號開啟、費用有上限、被限流時會換同一家的其他模型。對送出去的資料內容沒有管控。
- **證據**：`deps.effective_cloud_limit`；`rate_limit_fallback.py`。
- **缺口**：GRD-1、GRD-2（送出前檢查）、AUD-4（供應商條款）。

### MANAGE 4　⬜

> Risk treatments, including response and recovery, and communication plans for the identified and measured AI risks are documented and monitored regularly.

- **意思**：風險處置、回應與復原、溝通計畫都寫成文件並定期檢視。
- **現況**：復原的一部分有：資料庫備份與還原演練。事件的回應流程、對使用者的溝通計畫、可接受的復原時間，都沒有。
- **證據**：`deploy/backup-db.sh`；`deploy/restore-drill.sh`。
- **缺口**：CMP-5、REL-2、HA-10。

---

## 生成式 AI 的風險（NIST AI 600-1）

NIST 另有一份生成式 AI 的補充文件，列出 12 類風險。和這個 gateway 的關係：

| 風險 | 和 gateway 的關係 | 待辦 |
|---|---|---|
| Data Privacy | prompt 可能含個資並送往雲端 | GRD-1、CMP-6 |
| Information Security | 間接 prompt injection 沒有偵測；機密可能隨 prompt 送出 | GRD-2、GRD-4 |
| Dangerous, Violent, or Hateful Content | 地端有 guardrail（只記錄）；雲端靠供應商 | GRD-3、GRD-5 |
| Confabulation | gateway 無法判斷輸出正確性 | 使用政策；dashboard 提醒（AUD-4） |
| Intellectual Property | 輸出是否侵權、輸入是否含他人著作，gateway 不判斷 | 使用政策；AUD-4 記錄模型授權 |
| Value Chain and Component Integration | Azure、AWS、開源模型 | AUD-4、AUD-5 |
| Human-AI Configuration | 過度信任輸出，是 client 端與使用者的問題 | 使用政策 |
| Harmful Bias and Homogenization、Information Integrity、Environmental Impacts、CBRN Information or Capabilities、Obscene, Degrading, and/or Abusive Content | 內部寫程式與文件的用途下風險低；guardrail 的類別涵蓋其中一部分 | 在 AUD-4 寫明這個判斷 |

風險名稱也是憑記憶寫的，請一併核對。
