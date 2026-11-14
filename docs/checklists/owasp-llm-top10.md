# OWASP Top 10 for LLM Applications（2025）盤點清單

範圍：gateway 是 client（Claude Code、SDK、內部應用）和模型之間的代理。它看得到請求與回應，但**看不到也管不到** client 端做的事：執行工具、把輸出顯示在網頁上、RAG。所以每一項都分成「gateway 能做的」和「要靠 client 端或政策的」。

標記說明見 [README](README.md)。

## 總表

| 項目 | 狀態 | 一句話 | 對應 |
|---|---|---|---|
| LLM01 Prompt Injection | 🟡 | 直接攻擊在地端有偵測；間接注入沒有 | GRD-4 |
| LLM02 Sensitive Information Disclosure | 🟡 | gateway 自己不洩漏；使用者送出的內容沒有檢查 | GRD-1、GRD-2 |
| LLM03 Supply Chain | 🟡 | Python 套件有掃描；模型來源與映像沒有 | AUD-4、N-5 |
| LLM04 Data and Model Poisoning | ➖ | 不訓練、不微調 | AUD-4 記錄模型來源 |
| LLM05 Improper Output Handling | 🟡 | gateway 不解讀輸出；輸出沒有檢查 | GRD-5 |
| LLM06 Excessive Agency | 🟡 | 工具在 client 端執行；gateway 只能檢查 client 設定 | client rules |
| LLM07 System Prompt Leakage | ➖ | gateway 不注入 system prompt | |
| LLM08 Vector and Embedding Weaknesses | ➖ | embeddings / rerank 只是轉送；RAG 在 client 端 | |
| LLM09 Misinformation | 🏢 | gateway 無法判斷正確性 | 使用政策 |
| LLM10 Unbounded Consumption | ✅ | 預算、雲端上限、併發限制、throttle | N-9 |

## LLM01　Prompt Injection

| 檢查點 | 狀態 | 現況 / 證據 | 待辦 |
|---|---|---|---|
| 偵測使用者直接輸入的攻擊（jailbreak） | 🟡 | Qwen3Guard 的 Jailbreak 類別；只限地端流量、只看使用者最新一輪、monitor 模式（`guardrail.py`） | GRD-3（雲端）；GRD-7（block 條件） |
| 偵測間接注入（網頁、檔案、工具輸出） | ⬜ | tool result 一律 `skipped`，而 Claude Code 的請求大多是這種 | GRD-4（已決定：先靠 client 端） |
| system 訊息不被中途插入的內容取代 | ✅ | 對話中途的 system / developer 訊息併入相鄰的 user 輪，不提升到最前面 | |
| 限制被注入後能做的事（最小權限） | 🏢 | 在 client 端（Claude Code 的權限模式） | client rules 檢查設定（CC001） |
| 攻擊紀錄可追溯到帳號 | ✅ | guardrail 判定進 Langfuse，週報列出要注意的帳號（只有次數，沒有內容） | |

## LLM02　Sensitive Information Disclosure

| 檢查點 | 狀態 | 現況 / 證據 | 待辦 |
|---|---|---|---|
| log 不含 prompt | ✅ | `redact.summarize_body` 只記形狀；`LOG_REQUEST_BODIES` 預設關 | |
| 可觀測性工具不存 prompt | ✅ | `LANGFUSE_CAPTURE_IO` 預設關 | ❓ 確認正式環境是關的；開啟時要套遮罩（GRD-1） |
| 錯誤訊息不洩漏內部資訊 | ✅ | client 只看到例外類型（`redact.downstream_error_detail`） | |
| API key 不外洩 | ✅ | admin 列表遮罩；API 端點不回傳任何 key；稽核紀錄會遮蔽機密欄位 | key 在資料庫是明文（CMP-2，風險接受） |
| 一個帳號看不到別人的資料 | ✅ | 帳號端點只回自己的用量；service account 只有擁有者看得到 | |
| 使用者把個資送往雲端 | ⬜ | | GRD-1 |
| 使用者把密碼、金鑰送往雲端 | ⬜ | | GRD-2 |
| 供應商如何處理收到的資料 | ❓ | | AUD-4（Azure / AWS 的保存與訓練條款） |

## LLM03　Supply Chain

| 檢查點 | 狀態 | 現況 / 證據 | 待辦 |
|---|---|---|---|
| 相依套件弱點掃描 | ✅ | pip-audit（PR、push、每週）、dependabot | |
| 相依套件版本鎖定 | ✅ | `uv.lock`，CI 與映像都用 `--locked` | |
| 程式庫裡沒有機密 | ✅ | gitleaks（新 commit；每週掃完整歷史） | |
| 容器映像與 OS 套件掃描 | ⬜ | | N-5 |
| 模型來源與完整性（權重從哪裡下載、有沒有驗證） | ⬜ | vLLM 主機上的事，gateway 不知道 | AUD-4 |
| 模型授權條款 | ⬜ | | AUD-4 |
| 雲端模型版本變更的掌握 | 🟡 | 別名 → 實際模型 / 部署在設定裡，變更進稽核（只記欄位名稱） | CMP-4 |

## LLM04　Data and Model Poisoning

| 檢查點 | 狀態 | 現況 / 證據 | 待辦 |
|---|---|---|---|
| 訓練 / 微調資料的管控 | ➖ | 不訓練、不微調 | 之後若做微調或 RAG，重新盤點 |
| 使用的模型是否可信 | 🏢 | | AUD-4；N-7（上架清單） |

## LLM05　Improper Output Handling

| 檢查點 | 狀態 | 現況 / 證據 | 待辦 |
|---|---|---|---|
| gateway 自己不把模型輸出當成程式或 HTML 執行 | ✅ | dashboard 與 admin 頁面不顯示模型輸出；輸出只會原樣轉給 client | |
| 頁面上顯示的外部文字有跳脫 | ✅ | Jinja autoescape；信件用 `html.escape`；報告用 `_esc` | |
| 輸出內容檢查（不當內容、機密） | ⬜ | | GRD-5 |
| client 端如何處理輸出（執行指令、顯示在網頁） | 🏢 | client 端的責任 | 寫進使用政策與 `docs/client-guide.md` |

## LLM06　Excessive Agency

| 檢查點 | 狀態 | 現況 / 證據 | 待辦 |
|---|---|---|---|
| 模型能呼叫的工具與權限 | 🏢 | 工具定義與執行都在 client 端；gateway 只轉送 `tools` | |
| 高風險動作需要人確認 | 🟡 | CC001 會告訴使用者 auto mode 的安全檢查需要 client 端設定；gateway 刻意不代答（代答等於把安全檢查關掉） | 視需要加新的 client check |
| service account 的權限範圍 | 🟡 | 有擁有者、有每日預算、可限 IP | AUD-2 |

## LLM07　System Prompt Leakage

| 檢查點 | 狀態 | 現況 / 證據 | 待辦 |
|---|---|---|---|
| gateway 是否注入含機密的 system prompt | ➖ | 不注入。client rules 的提醒與 guardrail 的拒絕訊息是 admin 寫的公開文字 | |
| client 的 system prompt 是否含機密 | 🏢 | client 端的責任 | GRD-2 上線後會偵測到金鑰類內容 |

## LLM08　Vector and Embedding Weaknesses

| 檢查點 | 狀態 | 現況 / 證據 | 待辦 |
|---|---|---|---|
| 向量資料庫的存取控制、資料隔離 | ➖ | gateway 沒有向量資料庫；`/v1/embeddings`、`/v1/rerank` 只轉送 | 內部應用自己做 RAG 時，由該應用負責 |
| embeddings 的輸入輸出不被保存 | ✅ | Langfuse 對 embeddings / rerank 只記指標，不存內容 | |

## LLM09　Misinformation

| 檢查點 | 狀態 | 現況 / 證據 | 待辦 |
|---|---|---|---|
| 使用者知道輸出可能有錯、要自行確認 | 🏢 | | 使用政策；dashboard 可加一行說明 |
| 重要用途有人工覆核 | 🏢 | | AUD-4（依用途評估） |

## LLM10　Unbounded Consumption

| 檢查點 | 狀態 | 現況 / 證據 | 待辦 |
|---|---|---|---|
| 每個帳號的費用上限 | ✅ | `daily_limit_usd`（0 就是 0 元，不是無上限）；雲端另有一個上限 | |
| 每個帳號的併發上限 | 🟡 | 功能有，地端與雲端分開計；預設 `off` | ❓ 確認正式環境的模式 |
| 單一請求的大小上限 | ✅ | nginx `client_max_body_size 50m` | |
| 猜 key 的防護 | ✅ | 失敗認證 throttle（依不同的錯誤 key 計數） | 每 worker 各算（HA-5） |
| 異常用量偵測 | ✅ | 每小時：成本暴增、非上班時間、爆量、行為變化（只偵測、不阻擋） | N-2（通知） |
| 每分鐘請求數 / token 數的限制 | ⬜ | 沒有；靠每日預算與併發上限間接限制 | 視 N-9 壓測結果決定要不要做 |
| 整體容量保護（GPU 排隊） | 🟡 | 看得到 vLLM 排隊數；滿載時沒有主動拒絕或排隊機制 | N-9 |
| 模型被大量查詢以複製（model extraction） | ➖ | 開源模型本來就公開；雲端模型由供應商防護 | |
