# OWASP Top 10 for LLM Applications（2025）

每一項先引用 OWASP 的原文定義，再逐條對照 OWASP 建議的緩解措施，說明這個 gateway 現在做到哪裡。標記說明見 [README](README.md)。

**引用來源**：英文引文與緩解措施的標題取自 *OWASP Top 10 for LLM Applications 2025*（[專案 repo](https://github.com/OWASP/www-project-top-10-for-large-language-model-applications)，`2_0_vulns/`，2026-11-14 取得），© OWASP Foundation，以 [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) 授權。中文說明是本文件所加。

**先讀這段**：gateway 是 client（Claude Code、SDK、內部應用）和模型之間的代理。它看得到請求與回應，但管不到 client 端做的事：執行工具、把輸出顯示在網頁上、RAG。OWASP 的許多緩解措施是寫給「應用程式開發者」的，對 gateway 來說屬於 client 端或使用政策（標 🏢），或根本不適用（標 ➖）。

## 總覽

| 項目 | 狀態 | 結論 | 待辦 |
|---|---|---|---|
| [LLM01 Prompt Injection](#llm012025-prompt-injection) | 🟡 | 直接攻擊在地端有偵測；間接注入沒有 | GRD-4、GRD-3 |
| [LLM02 Sensitive Information Disclosure](#llm022025-sensitive-information-disclosure) | 🟡 | gateway 自己不洩漏；使用者送出的內容沒有檢查 | GRD-1、GRD-2 |
| [LLM03 Supply Chain](#llm032025-supply-chain) | 🟡 | Python 套件有掃描；模型來源、授權、映像沒有 | AUD-4、AUD-5、CMP-9 |
| [LLM04 Data and Model Poisoning](#llm042025-data-and-model-poisoning) | ➖ | 不訓練、不微調 | AUD-5 |
| [LLM05 Improper Output Handling](#llm052025-improper-output-handling) | 🟡 | gateway 不執行輸出；輸出沒有檢查 | GRD-5 |
| [LLM06 Excessive Agency](#llm062025-excessive-agency) | 🏢 | 工具都在 client 端 | client rules |
| [LLM07 System Prompt Leakage](#llm072025-system-prompt-leakage) | ➖ | gateway 不注入 system prompt | |
| [LLM08 Vector and Embedding Weaknesses](#llm082025-vector-and-embedding-weaknesses) | ➖ | 沒有向量資料庫 | |
| [LLM09 Misinformation](#llm092025-misinformation) | 🏢 | gateway 無法判斷正確性 | 使用政策 |
| [LLM10 Unbounded Consumption](#llm102025-unbounded-consumption) | ✅ | 預算、上限、併發、throttle 都有 | REL-3 |

---

## LLM01:2025 Prompt Injection

> A Prompt Injection Vulnerability occurs when user prompts alter the LLM's behavior or output in unintended ways.
>
> Direct prompt injections occur when a user's prompt input directly alters the behavior of the model in unintended or unexpected ways. […] Indirect prompt injections occur when an LLM accepts input from external sources, such as websites or files.

**對 gateway 的意思**：分兩種。使用者自己輸入的攻擊（direct）gateway 看得到也能判定；從網頁、檔案、工具輸出混進來的（indirect）目前完全不檢查，而 Claude Code 的請求大多是後者。

| OWASP 緩解措施 | 狀態 | Gateway 現況 |
|---|---|---|
| 1. Constrain model behavior | 🏢 | system prompt 由 client 決定，gateway 不注入。gateway 只保證中途的 system 訊息不會被提升到最前面 |
| 2. Define and validate expected output formats | 🏢 | client 端 |
| 3. Implement input and output filtering | 🟡 | **輸入**：Qwen3Guard 篩檢地端流量中使用者最新一輪的文字（含 Jailbreak 類別），monitor 模式（`guardrail.py`）。雲端流量不篩檢；tool result 不篩檢。**輸出**：沒有 |
| 4. Enforce privilege control and least privilege access | 🏢 | 工具權限在 client 端。gateway 這邊：每個帳號只能用被授權的模型與後端 |
| 5. Require human approval for high-risk actions | 🏢 | Claude Code 的權限模式。client rules 的 CC001 會提醒 auto mode 的安全檢查要在 client 端設定 |
| 6. Segregate and identify external content | 🏢 | client 端組訊息時的責任 |
| 7. Conduct adversarial testing and attack simulations | ⬜ | 沒有做過 |

**缺口**：間接注入的偵測 → GRD-4（已決定先靠 client 端）。雲端流量 → GRD-3。輸出 → GRD-5。從 monitor 切到 block 的條件 → GRD-7。對抗測試 → CMP-9。

---

## LLM02:2025 Sensitive Information Disclosure

> Sensitive information can affect both the LLM and its application context. This includes personal identifiable information (PII), financial details, health records, confidential business data, security credentials, and legal documents.
>
> Consumers should be aware of how to interact safely with LLMs. They need to understand the risks of unintentionally providing sensitive data, which may later be disclosed in the model's output.

**對 gateway 的意思**：兩個方向。一是 gateway 自己會不會洩漏（log、錯誤訊息、API key）：這部分做得完整。二是使用者把敏感資料送進模型，尤其是送往雲端：這部分沒有任何檢查，是最大的缺口。

| OWASP 緩解措施 | 狀態 | Gateway 現況 |
|---|---|---|
| Sanitization：Integrate Data Sanitization Techniques | ➖ | 指的是訓練資料；gateway 不訓練模型。供應商會不會拿去訓練 → AUD-4 |
| Sanitization：Robust Input Validation | ⬜ | 沒有偵測或過濾敏感內容 |
| Access Controls：Enforce Strict Access Controls | ✅ | 每人一把 key、雲端權限逐帳號開啟、帳號端點只回自己的資料、service account 的 key 只有擁有者看得到 |
| Access Controls：Restrict Data Sources | ➖ | gateway 不替模型取資料 |
| Federated Learning、Differential Privacy | ➖ | 訓練技術 |
| User Education：Educate Users on Safe LLM Usage | 🟡 | `docs/client-guide.md`、dashboard 的 client notices。沒有「哪些資料可以送哪個模型」的明文 |
| User Education：Ensure Transparency in Data Usage | ⬜ | 使用者看不到資料會保存多久、會不會離開公司 |
| Secure System Configuration：Conceal System Preamble | ➖ | gateway 沒有 system prompt |
| Secure System Configuration：Security Misconfiguration Best Practices | ✅ | client 只看到例外類型，不含內部主機與網址（`redact.downstream_error_detail`）；log 只記請求形狀不記內容（`redact.summarize_body`）；admin 列表遮罩 API key |
| Advanced：Homomorphic Encryption | ➖ | |
| Advanced：Tokenization and Redaction | ⬜ | 送出前沒有遮罩 |

**另外已做到的**：Langfuse 預設不存 prompt（`LANGFUSE_CAPTURE_IO` 關閉）；沒有任何 API 端點會回傳 API key；稽核紀錄會遮蔽機密欄位。

**缺口**：PII 偵測與遮罩 → GRD-1。密碼、金鑰的偵測 → GRD-2。資料保存期限 → CMP-6。供應商如何處理資料、在 dashboard 上告訴使用者 → AUD-4。API key 在資料庫是明文 → CMP-2（已決定風險接受）。

---

## LLM03:2025 Supply Chain

> LLM supply chains are susceptible to various vulnerabilities, which can affect the integrity of training data, models, and deployment platforms. […] While traditional software vulnerabilities focus on issues like code flaws and dependencies, in ML the risks also extend to third-party pre-trained models and data.

**對 gateway 的意思**：供應鏈有三段：gateway 的 Python 相依套件（管得好）、容器映像（沒掃）、模型本身（開源權重從哪裡來、雲端模型的條款，完全沒有紀錄）。

| OWASP 緩解措施（節錄） | 狀態 | Gateway 現況 |
|---|---|---|
| 1. Carefully vet data sources and suppliers, including T&Cs and their privacy policies | ⬜ | Azure、AWS 的資料保存與訓練條款沒有書面紀錄 |
| 2. Vulnerability scanning, management, and patching components | 🟡 | pip-audit（每個 PR、push、每週）、dependabot、`uv.lock` 鎖定版本。容器基底映像與 OS 套件沒有掃描 |
| 3. AI Red Teaming and Evaluations when selecting a third party model | ⬜ | 新模型上線前沒有評估 |
| 4. Maintain an up-to-date inventory of components using an SBOM | 🟡 | `uv.lock` 是完整且鎖定的相依清單；沒有產出 SBOM；模型沒有清冊 |
| 5. Create an inventory of all types of licenses involved | ⬜ | 模型授權沒有紀錄 |
| 6. Only use models from verifiable sources … integrity checks with signing and file hashes | ❓ | vLLM 主機上的事，gateway 不知道權重怎麼下載、有沒有驗證 |
| 7. Monitoring and auditing for collaborative model development environments | ➖ | 不開發模型 |
| 8. Anomaly detection and adversarial robustness tests on supplied models | ⬜ | |
| 9. Implement a patching policy | 🟡 | dependabot 會開 PR；沒有寫成政策（多久內要修） |
| 10. Encrypt models deployed at AI edge | ➖ | 沒有邊緣裝置 |

**另外已做到的**：gitleaks 掃描 commit 裡的機密；映像以 `uv sync --locked` 建置、非 root。

**缺口**：模型清冊、授權、供應商條款 → AUD-4。上線前評估 → AUD-5。映像掃描與 SAST → CMP-9。

---

## LLM04:2025 Data and Model Poisoning

> Data poisoning occurs when pre-training, fine-tuning, or embedding data is manipulated to introduce vulnerabilities, backdoors, or biases.

**對 gateway 的意思**：不訓練、不微調、沒有 embedding 資料庫，所以十條緩解措施幾乎都不適用。剩下的風險是「拿來用的模型本身有沒有問題」，和 LLM03 是同一件事。

| OWASP 緩解措施（節錄） | 狀態 | Gateway 現況 |
|---|---|---|
| 1. Track data origins and transformations | ➖ | |
| 2. Vet data vendors rigorously, and validate model outputs against trusted sources | ⬜ | 併入 AUD-5 |
| 3 ~ 9. Sandboxing、資料版本控制、訓練監控等 | ➖ | 訓練流程 |
| 10. RAG and grounding techniques | 🏢 | client 端 |

**缺口**：AUD-5（上架清單裡確認模型來源）。之後如果做微調或 RAG，這一項要重新盤點。

---

## LLM05:2025 Improper Output Handling

> Improper Output Handling refers specifically to insufficient validation, sanitization, and handling of the outputs generated by large language models before they are passed downstream to other components and systems.

**對 gateway 的意思**：OWASP 講的是「拿到模型輸出後怎麼用」。gateway 自己不解讀輸出，只是原樣轉給 client，所以風險在 client 端。gateway 能加的只有輸出內容的檢查。

| OWASP 緩解措施（節錄） | 狀態 | Gateway 現況 |
|---|---|---|
| 1. Treat the model as any other user, adopting a zero-trust approach | ✅ | gateway 不把模型輸出當成指令，也不會依輸出做任何動作 |
| 2. Follow the OWASP ASVS guidelines | 🏢 | client 端 |
| 3. Encode model output back to users | ✅ | dashboard 與 admin 頁面不顯示模型輸出。頁面上顯示的外部文字（admin 寫的訊息、模型名稱、錯誤文字）都有跳脫 |
| 4. Context-aware output encoding | 🏢 | client 端 |
| 5. Parameterized queries for all database operations involving LLM output | ✅ | 模型輸出不會進任何 SQL；資料庫只存 token 數、費用等數值 |
| 6. Strict Content Security Policies | 🟡 | nginx 範例有 `X-Frame-Options`、`X-Content-Type-Options`；沒有 CSP。因為頁面不顯示模型輸出，影響低 |
| 7. Robust logging and monitoring to detect unusual patterns in LLM outputs | ⬜ | 輸出內容沒有檢查 |

**缺口**：輸出內容檢查 → GRD-5。提醒使用者輸出要當成不可信資料處理 → `docs/client-guide.md`。

---

## LLM06:2025 Excessive Agency

> Excessive Agency is the vulnerability that enables damaging actions to be performed in response to unexpected, ambiguous or manipulated outputs from an LLM, regardless of what is causing the LLM to malfunction.

**對 gateway 的意思**：工具的定義、權限、執行全部在 client 端，gateway 只轉送 `tools` 欄位。所以八條預防措施都是 client 的責任；gateway 做得到的是 OWASP 列在後面的兩條「限制損害」的措施。

| OWASP 緩解措施 | 狀態 | Gateway 現況 |
|---|---|---|
| 1 ~ 4. Minimize extensions / functionality / permissions、Avoid open-ended extensions | 🏢 | client 端 |
| 5. Execute extensions in user's context | 🏢 | client 端 |
| 6. Require user approval | 🏢 | Claude Code 的權限模式。gateway 刻意不代答 auto mode 的安全檢查（代答等於把檢查關掉），CC001 會告訴使用者怎麼在 client 端設定 |
| 7. Complete mediation | 🏢 | 下游系統的責任 |
| 8. Sanitise LLM inputs and outputs | 🏢 | client 端 |
| （限制損害）Log and monitor the activity | 🟡 | 每個請求記在 `usage_logs`（誰、何時、哪個模型）；內容預設不記 |
| （限制損害）Implement rate-limiting | ✅ | 每日預算、併發上限 |

**缺口**：沒有 gateway 端的缺口。可以做的是用 client rules 多檢查幾項 client 端的安全設定。

---

## LLM07:2025 System Prompt Leakage

> It's important to understand that the system prompt should not be considered a secret, nor should it be used as a security control. Accordingly, sensitive data such as credentials, connection strings, etc. should not be contained within the system prompt language.

**對 gateway 的意思**：gateway 不注入 system prompt，所以沒有東西可洩漏。它會回給使用者的固定文字（client rules 的提醒、guardrail 的拒絕訊息）是 admin 寫的公開訊息。

| OWASP 緩解措施 | 狀態 | Gateway 現況 |
|---|---|---|
| 1. Separate Sensitive Data from System Prompts | 🏢 | client 端。GRD-2 上線後能偵測到 system prompt 裡的金鑰 |
| 2. Avoid Reliance on System Prompts for Strict Behavior Control | ✅ | gateway 的所有管控（預算、權限、guardrail）都在模型之外執行 |
| 3. Implement Guardrails | 🟡 | 獨立於模型的 guardrail 有（地端、輸入）；輸出沒有 |
| 4. Ensure that security controls are enforced independently from the LLM | ✅ | 認證、授權、額度都是程式判斷，不經過模型 |

**缺口**：無。

---

## LLM08:2025 Vector and Embedding Weaknesses

> Vectors and embeddings vulnerabilities present significant security risks in systems utilizing Retrieval Augmented Generation (RAG) with Large Language Models (LLMs).

**對 gateway 的意思**：gateway 沒有向量資料庫，`/v1/embeddings` 與 `/v1/rerank` 只是轉送。用 gateway 做 RAG 的內部應用要自己負責這一項。

| OWASP 緩解措施 | 狀態 | Gateway 現況 |
|---|---|---|
| 1. Permission and access control | ➖ | 沒有向量資料庫 |
| 2. Data validation & source authentication | ➖ | |
| 3. Data review for combination & classification | ➖ | |
| 4. Monitoring and Logging | 🟡 | embeddings / rerank 的呼叫記在 `usage_logs`；Langfuse 只記指標、不存內容 |

**缺口**：無。

---

## LLM09:2025 Misinformation

> Misinformation occurs when LLMs produce false or misleading information that appears credible.

**對 gateway 的意思**：gateway 無法判斷輸出對不對。能做的是 OWASP 的第 5、7 條：把限制講清楚。

| OWASP 緩解措施 | 狀態 | Gateway 現況 |
|---|---|---|
| 1. Retrieval-Augmented Generation | 🏢 | client 端 |
| 2. Model Fine-Tuning | ➖ | |
| 3. Cross-Verification and Human Oversight | 🏢 | 使用政策 |
| 4. Automatic Validation Mechanisms | 🏢 | client 端 |
| 5. Risk Communication | ⬜ | dashboard 與文件沒有提醒輸出可能有錯 |
| 6. Secure Coding Practices | 🏢 | 使用模型產生的程式碼時要審查：使用政策 |
| 7. User Interface Design | 🟡 | dashboard 列出模型、價格、狀態；沒有各模型的適用範圍與限制 |
| 8. Training and Education | 🏢 | |

**缺口**：在 dashboard 與 `docs/client-guide.md` 加上限制說明，各模型的適用範圍 → AUD-4。

---

## LLM10:2025 Unbounded Consumption

> Unbounded Consumption occurs when a Large Language Model (LLM) application allows users to conduct excessive and uncontrolled inferences, leading to risks such as denial of service (DoS), economic losses, model theft, and service degradation.

**對 gateway 的意思**：這是 gateway 存在的主要理由之一，也是十項裡做得最完整的。

| OWASP 緩解措施 | 狀態 | Gateway 現況 |
|---|---|---|
| 1. Input Validation | ✅ | nginx `client_max_body_size 50m` |
| 2. Limit Exposure of Logits and Logprobs | 🟡 | 直接轉送，沒有限制。地端是開源模型（權重本來就公開），雲端由供應商控管，影響低 |
| 3. Rate Limiting | 🟡 | 每人每日預算（`daily_limit_usd`，0 就是 0 元）、雲端上限、每人併發上限（預設 `off`）。沒有每分鐘請求數的限制 |
| 4. Resource Allocation Management | 🟡 | 併發上限地端與雲端分開計；看得到 vLLM 排隊數。GPU 滿載時不會主動分流 |
| 5. Timeouts and Throttling | ✅ | 等 header 有上限、非 stream 280 秒、stream 最長閒置 300 秒；失敗認證 throttle |
| 6. Sandbox Techniques | ➖ | gateway 不讓模型存取任何資源 |
| 7. Comprehensive Logging, Monitoring and Anomaly Detection | 🟡 | `usage_logs`；每小時異常偵測（成本暴增、非上班時間、爆量、行為變化）；Prometheus 指標。沒有告警 |
| 8. Watermarking | ➖ | |
| 9. Graceful Degradation | ✅ | vLLM failover、雲端 429 時換模型、未知的模型名稱會 fallback、副作用失敗不影響請求 |
| 10. Limit Queued Actions and Scale Robustly | ⬜ | 單台，沒有負載平衡 |
| 11. Adversarial Robustness Training、12. Glitch Token Filtering | ➖ | 模型訓練 |
| 13. Access Controls | ✅ | SSO、API key、IP allowlist |
| 14. Centralized ML Model Inventory | 🟡 | 模型都在 `config.toml` 一處設定；沒有含來源、授權、負責人的清冊 |
| 15. Automated MLOps Deployment | ➖ | 不部署模型 |

**缺口**：告警 → REL-1。容量與壓測 → REL-3。多台 → HA 主題。清冊 → AUD-4。確認正式環境的併發上限開在哪個模式 → CMP-1 的待確認事項。
