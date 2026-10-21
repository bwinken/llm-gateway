# Web UI 前端頁面

[English](README.md)

所有頁面透過 oauth2-proxy + nginx `auth_request` 保護，使用者需先完成 SSO 登入。頁面使用 Jinja2 模板 + Tailwind CSS (CDN) + Chart.js 渲染。

## 頁面一覽

```
/                    → Welcome 首頁（需 read 或 admin scope）
/dashboard           → 使用者 Dashboard（需 read 或 admin scope）
/admin               → 管理面板（需 admin scope）
/admin/models        → 模型設定（需 admin scope）
/setup               → Claude Code 安裝頁（需 read 或 admin scope；SSO 保護）
disabled.html        → 已停用使用者開啟任何 HTML 路由時，由 AccountDisabledError handler render
/oauth2/sign_out     → 登出（由 oauth2-proxy 處理）
```

---

## `/` — Welcome 首頁

**檔案**：`welcome.html` ← `web_ui.py`
**權限**：JWT scope 含 `read` 或 `admin`

### 頁面區塊

| 區塊 | 說明 |
|---|---|
| **Hero** | 標題 + 副標題 + Go to Dashboard 與 First-Time Setup（`/setup`）按鈕，以及「Install Claude Code」連結 |
| **Quick Start** | 三步驟引導：Get Your API Key → Point Your SDK → Make Requests |
| **Code Example** | Python Chat Completions 範例，含使用者的 API key 和實際 host |
| **Available Endpoints** | 7 列 API 端點表格：Chat Completions、Responses、Embeddings、Rerank、Score、System One（`POST /v1/systemone`）、Models（`GET /v1/models`），每列標示可服務的模型類型 |
| **Available Models** | 依類型分組、顏色標示的地端模型清單（來自 `MODEL_ROUTING`，略過 `hidden`）；每張卡片顯示管理員設定的 metadata badge（`display_name`、`context_window`、`reasoning`、`tools`、`vision`、`cache`、`max_output_tokens`） |
| **Feature Cards** | 三張特色卡片：Health-Aware Routing、Usage Tracking、Drop-In Compatible |

---

## `/dashboard` — 使用者 Dashboard

**檔案**：`dashboard.html` ← `web_ui.py`
**權限**：JWT scope 含 `read` 或 `admin`

預算主卡固定在上方；下半部由 `gwTabs('dashTabs', 'dashTab')` 切成兩個分頁（**Models** 為預設，另一個是 **Usage**，見 [`base.html`](#basehtml--base-layout)）。Models 分頁標籤顯示「N down」/「N queueing」，Usage 分頁標籤顯示本月費用。

### 頁面區塊

| 區塊 | 分頁 | 說明 |
|---|---|---|
| **Header** | — | 「Set up Claude Code」連結（→ `/setup`）與 **My API Key** 按鈕（開啟 key modal） |
| **Get started** | — | 帳號本月尚無任何請求時才顯示，連到 `/setup` |
| **Spent today（預算主卡）** | — | 今日費用對比 `daily_limit_usd`、Remaining、距重置（當地午夜）時間，以及依比例變色的進度條（ok < 70 % · warn < 90 % · crit < 100 % · over）。`daily_limit_usd = 0` 時顯示 "No daily budget on this account"。使用者可用任一雲端後端時（admin 一律可用），下方多一列 **On-prem / Azure / AWS Bedrock** 分欄，只列出使用者有權使用的後端；另有 **Cloud (Azure + AWS)** 列，顯示今日雲端合計費用對比使用者的雲端上限（`deps.effective_cloud_limit`；無上限時不顯示） |
| **This month** | — | 本月費用、請求數、Tokens in / out（附今日數字） |
| **On-Prem Models** | Models | 所有未隱藏的 vLLM 模型，依類型分組、依價格排序：ONLINE / DOWN、即時負載（來自 `/metrics` 的 running / queued）、Context、Max Out、實際計費的 In / Out $/1M（per-model → per-type → default，與計費一致）、能力 badge（`reasoning`、`tools`、`vision`、`cache`）。標題列顯示 online / queueing / down 數量與 base URL |
| **Azure OpenAI Models** | Models | 僅當 `can_use_azure or is_admin` 時顯示。未隱藏的 `[azure_models.*]` 依類型分組、依價格排序（Context、In / Out $/1M、能力 badge），base URL 為 `/azure/v1`。有權限但尚未設定任何模型時顯示 "Contact your administrator" |
| **AWS Bedrock Models** | Models | 同 Azure，條件為 `can_use_bedrock or is_admin`、來源為 `[bedrock_models.*]`，base URL 為 `/aws/v1` |
| **API examples** | Models | 預設收合的 `<details>`：Chat Completions、Embeddings、Rerank、Score 範例，含使用者的 API key 和 host；有設定 `systemone` 模型時才顯示 System One（TypeSafe SDK） |
| **Usage Trend** | Usage | Chart.js 圖表，近 30 天趨勢，可切換 Requests / Cost (USD) |
| **Cost by Model** | Usage | 依後端（On-Prem / Azure / AWS Bedrock）分組的各模型費用，含占比條與甜甜圈圖；This Month / Today 切換與 Refresh 按鈕都會呼叫 `GET /dashboard/api/model-breakdown?period=month\|day` 並原地重繪。使用者無權使用的後端不列入 |
| **Daily Breakdown** | Usage | 近 30 天有請求的日子：請求數、費用、input / output tokens |
| **My App Accounts** | Usage | 擁有的 App 帳號（僅 owner 可見）：截斷的 API key 與 Copy、本月費用 / tokens / 請求數、Refresh key |

### 可執行操作

| 操作 | 路徑 | 說明 |
|---|---|---|
| 查看 API Key | 按鈕 → Modal | 顯示完整 API key，可複製到剪貼簿 |
| Regenerate Key | `POST /dashboard/refresh-key` | 重新產生自己的 API key（舊 key 立即失效）；在 key modal 內操作 |
| Refresh App Key | `POST /dashboard/app/{id}/refresh-key` | 重新產生所屬 App 帳號的 API key（僅 owner 可操作） |
| Copy App Key | 按鈕 | 複製 App 帳號的完整 API key |
| Cost by Model 重新整理 | `GET /dashboard/api/model-breakdown?period=` | Cost by Model 卡片用的 JSON（`month` 或 `day`） |
| 下載安裝腳本 | `GET /dashboard/install-claude-code.bat` | 個人化的 Claude Code 安裝腳本（由 `/setup` 連結） |

---

## `/admin` — 管理面板

**檔案**：`admin.html` ← `admin.py`
**權限**：JWT scope 含 `admin`

分頁列上方固定是 **Needs attention** 面板與關鍵數字卡片。下方是共用的 `_admin_tabs.html` 分頁列：**Overview / Users / Settings**（透過 `gwTabs('adminTabs', 'adminTab')` 在頁內切換），以及 **Models**（連到獨立的 `/admin/models` 頁面）。分頁標籤：Users 顯示「N blocked」/「N near limit」（否則顯示帳號總數）；並行上限開啟時 Settings 顯示「limit: monitor|enforce」。

### 頁面區塊

| 區塊 | 分頁 | 說明 |
|---|---|---|
| **Needs attention** | — | 三欄：`scripts/scan_anomalies.py` 找出的未結用量異常（嚴重度、使用者/模型、規則、摘要；Ack / Resolve 按鈕）、DOWN 或排隊中的地端模型、今日用量 ≥ 90 % 每日上限的帳號（點擊連到 `?q=<username>#users`）。都沒有時收合為 "All clear" |
| **關鍵數字** | — | Spend today、Requests today、Active users today、In flight now（並行 lease 數；上限為 `off` 時顯示 "tracking off"）、本月費用 / 請求數 / tokens |
| **DAU Trend** | Overview | Chart.js 柱狀圖，近 30 天 Daily Active Users，右上顯示今日 DAU |
| **App Leaderboard** | Overview | App 帳號排行榜（`app_*`），按月費用降序 top-10 |
| **User Leaderboard** | Overview | 使用者排行榜，按月費用降序 top-10，顯示 display_name、org_code badge |
| **Department Usage** | Overview | 依 org_code 分組：Users、Cost、Input / Output Tokens、Reqs；超過 15 列時前端分頁 |
| **Export Usage Report** | Overview | 選擇月份區間（最多 24 個月）→ **Download xlsx**（`GET /admin/api/export/usage.xlsx`，4 個工作表）或 **Backend costs (CSV)**（`GET /admin/api/export/user-backend-costs.csv`，每列為「月份 × 帳號」，分 On-prem / Azure / Bedrock 費用欄） |
| **User Management** | Users | Users / Apps 子切換、後端搜尋（`q`，對 username / display_name / org_code 做 ILIKE）、後端分頁（`limit/offset`、`app_limit/app_offset`）、**Export Users (CSV)**（`GET /admin/api/export/users.csv`）。Users 表格：User、API Key（View Key）、Total Spend、Daily Limit（+ *Cloud $*）、Actions。Apps 表格另有可編輯的 Owner 欄 |
| **Concurrency Limit** | Settings | 模式（`off` / `monitor` / `enforce`）+ 每帳號上限（≥ 1）→ `POST /admin/concurrency-limit`；顯示目前進行中的請求數、帳號數與最忙的帳號。切到 Enforce 前會要求確認 |
| **Default Daily Limit** | Settings | `POST /admin/default-limit` — 寫入 `[app].default_daily_limit_usd`，並批次將 `0 < daily_limit_usd < new_floor` 的使用者拉高到新 floor（`daily_limit_usd = 0` 的使用者是刻意設成 $0，不會被改動） |
| **Cloud Daily Limit** | Settings | `POST /admin/cloud-limit` — 每位使用者每日 Azure + AWS 合計費用的預設上限（`[app].cloud_daily_limit_usd`），套用於有雲端權限且沒有自訂 *Cloud $* 的使用者。留空 = 無預設上限；0 = 拒絕雲端 |
| **Cloud Budget Fallback** | Settings | 勾選框 → `POST /admin/cloud-budget-fallback`。開啟時，超過雲端上限的 `/v1/*` 雲端請求改由地端模型服務，而非回 429（整體每日上限仍然有效；`/azure/v1/*` 與 `/aws/v1/*` 永不 fallback） |
| **Create App Account** | Settings | Username（自動加 `app_` 前綴）、Daily Limit、Owner → `POST /admin/users/create`。App 帳號建立時即免除並行上限 |
| **Site Links** | Settings | Support Bot URL（每頁右下角的浮動按鈕）與 Install Guide URL（`/setup` 上的卡片）→ `POST /admin/site-links`。只接受 `http(s)://` 或站內相對路徑；留空則隱藏 |

### Actions 欄

每筆 user / app 列由左至右有以下按鈕：

| 按鈕 | 路徑 | 顯示於 | 行為 |
|---|---|---|---|
| **Azure** | `POST /admin/users/{id}/toggle-azure` | 每一列 | 翻轉 `can_use_azure`（已授權為紫色，未授權為 ghost）。原地更新 |
| **AWS** | `POST /admin/users/{id}/toggle-bedrock` | 每一列 | 翻轉 `can_use_bedrock`（已授權為橘色，未授權為 ghost）。原地更新 |
| **Waive** | `POST /admin/users/{id}/toggle-concurrency-waive` | 非 admin 列 | 翻轉 `concurrency_waived`（已免除為綠色）。原地更新 |
| **Disable / Enable** | `POST /admin/users/{id}/toggle-disable` | 非 admin 的 user 列、所有 app 列 | 翻轉 `is_disabled`（停用時為警示黃的 "Enable"）。後端禁止停用自己。完整 POST → redirect |
| **Delete** | `POST /admin/users/{id}/delete` | 非 admin 的 user 列、所有 app 列 | 刪除使用者及所有參照它的資料（usage logs、擁有關係、異常紀錄、lease）；不可刪除自己。完整 POST → redirect |

Disable / Enable 與 Delete 會先要求確認。原地更新的切換按鈕（Azure、AWS、Waive）以 `Accept: application/json` 送出 POST，取回新的旗標值後只重繪該按鈕；沒有這個 header 時端點仍回傳傳統的 303 → `/admin`。

### 其他操作

| 操作 | 路徑 | 說明 |
|---|---|---|
| Update Daily Limit | `POST /admin/users/{id}/limit` | 修改使用者的每日費用上限（有限值且 ≥ 0；0 代表 $0 預算） |
| Update Cloud $ | `POST /admin/users/{id}/cloud-limit` | 使用者自己的 Azure + AWS 每日上限。只對有 Azure 或 AWS 權限的使用者及 admin 顯示；placeholder 顯示預設值；留空則回到預設，0 為拒絕雲端 |
| Set Owners | `POST /admin/users/{id}/owner` | Apps 表格：替換 App 的 owner（以逗號分隔的 username） |
| View API Key | `GET /admin/users/{id}/api-key` | 清單只帶遮罩後的 key；modal 每次針對單一帳號取得完整 key（會記錄 log） |
| Regenerate Key | `POST /admin/users/{id}/refresh-key` | 重新產生任意使用者的 API key（在 key modal 內操作） |
| Ack / Resolve 異常 | `POST /admin/api/anomalies/{id}/ack` · `/resolve` | 在 Needs attention 面板操作 |

> Limit 與 Cloud $ 以 `fetch` 送出，成功 / 失敗時輸入框閃綠 / 紅，不重新載入頁面。Cloud Budget Fallback 勾選框送出明確的 `enabled=on|off`（停用中的控制項不會出現在 `FormData` 裡）。會重新載入的操作（Disable、Delete、搜尋、分頁、表單 POST）會從 `sessionStorage` 還原捲動位置與 Users / Apps 子切換；目前分頁則由 `gwTabs` 的 `sessionStorage` 備援還原，因為每個 POST 都會 redirect 到不帶 hash 的 `/admin`。

同一個 router 另提供頁面本身不使用的 JSON 端點：`GET` / `POST /admin/users`、`PATCH /admin/users/{id}`、`GET /admin/api/anomalies`。

---

## `/admin/models` — 模型設定

**檔案**：`admin_models.html` ← `admin.py`
**權限**：JWT scope 含 `admin`

建構在 `GET` / `PUT /admin/api/config` 之上的主從式（master–detail）編輯器，上方使用同一個 `_admin_tabs.html` 分頁列（Overview / Users / Settings 連回 `/admin#<tab>`；Models 為作用中）。刻意做成獨立頁面：它的 URL hash 就是目前選取的項目，而且未儲存變更的離頁警告必須在換頁時觸發。

### 頁面區塊

| 區塊 | 說明 |
|---|---|
| **模型清單**（左） | 可搜尋（alias、real model / deployment / model ID、display name）。分組：On-prem (vLLM)（再依類型分組）、Azure OpenAI、AWS Bedrock，各自顯示數量與 **+ Add**。Badge：`vlm`（雲端）、`think`（`is_reasoning`）、`429→<alias>`（rate-limit fallback）、`hidden`。琥珀色圓點 = 上次儲存後有修改；紅點 = 缺少必填欄位。最上方是 **Fallbacks & pricing** 項目 |
| **模型編輯器**（右） | 標題列顯示後端、類型、服務路徑與「被哪些模型當作 fallback」；**Duplicate** / **Rename** / **Delete**。區段：**Connection**（類型 + vLLM `real_model` / `base_url` / `api_key`；Azure `endpoint` / `deployment` / `api_key` / `api_version`；Bedrock `model_id` / `region` / `api_key` / `endpoint` / IAM 金鑰組，機密欄位遮罩並可 Show）、**Routing & visibility**（是否列在 `/v1/models` ↔ `hidden`；vLLM 的 `native_messages`；雲端的 `rate_limit_fallback` 與解析後的鏈，最多 3 跳）、**Pricing overrides**（input / output / cached input $/1M；留空 = 類型預設）、**Reasoning**（`is_reasoning`、接受的 `reasoning_efforts`、針對不接受等級的 `reasoning_effort_map`：對應或移除）、**Metadata**（`display_name`、`context_window`、`max_output_tokens`、`supports_tools` / `vision` / `prompt_caching`） |
| **Fallbacks & pricing** | vLLM、Azure、Bedrock 各自的 per-type fallback 下拉選單（`fallback`、`azure_fallback`、`bedrock_fallback`；留空 = 自動），以及類型定價表（Default + 各類型的 input / output $/1M） |
| **未儲存變更列** | config 與上次載入的快照不同時，固定顯示 Discard / Save |

### 行為

- 選取狀態存在 URL hash（`#vllm/<alias>`、`#azure/<alias>`、`#bedrock/<alias>`、`#settings`），重新整理後仍停在原處。
- 每個輸入欄位都透過 `setField` 直接寫入頁面的 `config` 物件（依 `data-kind` / `data-alias` / `data-field` / `data-t` 轉型；選填欄位留空即刪除該 key）。儲存時不會重新從 DOM 收集 — DOM 中只有目前選取模型的表單。
- 是否有未儲存變更以 `JSON.stringify(config)` 與上次儲存的快照比對；Ctrl/Cmd+S 儲存，`beforeunload` 會提出警告。
- Add / Duplicate / Rename 共用同一個對話框，強制 alias 在三種後端間不可重複。Rename 與 Delete 會一併更新或清除類型 fallback 及 `rate_limit_fallback` 的參照。

### 可執行操作

| 操作 | 路徑 | 說明 |
|---|---|---|
| Add Model | 按鈕 → 對話框 | 選擇後端、類型與 alias，建立空白項目後再填寫 |
| Duplicate / Rename / Delete | 編輯器標題列 | 將所有設定複製到新 alias（預設 `<alias>-copy`）/ 改名 / 刪除（儲存後生效） |
| Set Fallback | Fallbacks & pricing | 各後端的 per-type fallback 模型 |
| Edit Pricing | Fallbacks & pricing / 模型編輯器 | 類型定價表，或 per-model 覆寫 |
| Save | `PUT /admin/api/config` | 任何模型缺必填欄位時前端拒絕儲存；後端驗證（含跨後端 alias 衝突）通過後才寫入 `config.toml` 並重新載入 |
| Discard | 按鈕 | 還原為上次載入的設定 |

---

## `/setup` — Claude Code 安裝頁

**檔案**：`setup.html` ← `web_ui.py`
**權限**：JWT scope 含 `read` 或 `admin`（SSO 保護；nginx 不對 `/setup` 跳過 oauth2-proxy）

單一用途頁面，引導使用者安裝 Claude Code CLI：(1) 下載個人化安裝腳本（`GET /dashboard/install-claude-code.bat`，伺服器端把使用者的 API key 填進去；伺服器上缺少 `setup/install-claude-code.bat` 時，頁面改為請使用者聯絡維運人員），(2) 執行，(3) `~/.claude/settings.json` 設定參考，依主題列出支援的 `env` key。下方另有收合的 Troubleshooting 區塊。若 admin 有設定 `install_guide_url`（Admin → Settings → Site Links），步驟上方會多一張醒目的「Step-by-step Install Guide」卡片。

原本的 CA 憑證分頁（以及 `/setup/files/<name>` 下載路由）已移除 — gateway 不再提供憑證安裝程式。

---

## `disabled.html` — 帳號停用頁面

**檔案**：`disabled.html` ← 由 `app/main.py` 內 `AccountDisabledError` 全域 handler render

獨立的樣式化錯誤頁面（不含 `base.html` 框架），當已停用的使用者透過瀏覽器開啟任何路由時，以 HTTP 403 回傳。Handler 依 `Accept` header 區分：

- `Accept` 含 `text/html` → render 此頁（Account Disabled 標題 + 說明 + Sign Out 按鈕，連到 `/oauth2/sign_out`）
- 其他 → JSON 403 `{"detail": "Account disabled. Contact your administrator."}`（SDK 與 curl 用）

Admin 在 auth 階段免檢查停用旗標，因此這頁永遠不會擋到 admin 登入修復狀態。

---

## 共用元件

### `base.html` — Base Layout

所有頁面的共用框架：

- **Navbar** — Logo（App title 首字母）+ App title（→ `/dashboard`）、Setup 連結、Admin 連結（admin scope 時顯示）、顯示名稱 + org_code badge、Logout 按鈕
- **Footer** — Copyright + 年份 + App title
- **Support Bot 按鈕** — 右下角浮動連結，只在有設定 `support_bot_url`（Admin → Site Links）時顯示；透過 `get_site_links` 模板全域函式讀取
- **`gwTabs(barId, storageKey)`** — 頁面分頁：分頁列中的按鈕帶 `data-tab="x"`，面板帶 `data-panel="x"`（多個面板可共用同一名稱）。目前分頁取自 URL hash，否則取 `sessionStorage[storageKey]`（表單 POST 會 redirect 到不帶 hash 的網址），再否則為第一個分頁。每次切換後觸發 `resize` 事件，讓隱藏分頁內的圖表正確計算尺寸
- **Tooltip** — 任何帶 `gw-tip` + `data-tip` 的元素（長文字加 `gw-tip-wide`）
- **CDN** — Tailwind CSS、Chart.js、Plus Jakarta Sans + JetBrains Mono 字型

### `_admin_tabs.html` — 管理分頁列

由 `admin.html`（`on_admin_page = true`：Overview / Users / Settings 為 `data-tab` 按鈕）與 `admin_models.html`（`on_admin_page = false`：改為連到 `/admin#<tab>` 的連結）引入。第四個分頁 **Models** 一律是連到 `/admin/models` 的連結。

### 認證流程

```
Browser → Nginx → auth_request /oauth2/auth → oauth2-proxy 驗證 cookie
                                ↓ 通過
                  Nginx 注入 Authorization: Bearer <JWT>
                                ↓
                  FastAPI → get_web_user() 解析 JWT
                                ↓
                  根據 scopes 決定頁面存取權限
```

| Scope | 可存取頁面 |
|---|---|
| `read` | `/`、`/dashboard`、`/setup` |
| `admin` | `/`、`/dashboard`、`/setup`、`/admin`、`/admin/models` |

### 登出

Logout 按鈕指向 `/oauth2/sign_out`，由 oauth2-proxy 清除 session cookie 並導回登入頁。
