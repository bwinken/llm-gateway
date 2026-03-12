# Web UI 前端頁面

所有頁面透過 oauth2-proxy + nginx `auth_request` 保護，使用者需先完成 SSO 登入。頁面使用 Jinja2 模板 + Tailwind CSS (CDN) + Chart.js 渲染。

## 頁面一覽

```
/                    → Welcome 首頁（需 read 或 admin scope）
/dashboard           → 使用者 Dashboard（需 read 或 admin scope）
/admin               → 管理面板（需 admin scope）
/admin/models        → 模型設定（需 admin scope）
/oauth2/sign_out     → 登出（由 oauth2-proxy 處理）
```

---

## `/` — Welcome 首頁

**檔案**：`welcome.html` ← `web_ui.py`
**權限**：JWT scope 含 `read` 或 `admin`

### 頁面區塊

| 區塊 | 說明 |
|---|---|
| **Hero** | 標題 + 副標題 + 前往 Dashboard 按鈕 |
| **Quick Start** | 三步驟引導：取得 API Key → 設定 SDK → 發送請求 |
| **Code Example** | Python Chat Completions 範例，含使用者的 API key 和實際 host |
| **Available Endpoints** | 6 個 API 端點表格（Chat、Responses、Embeddings、Rerank、Score、Models） |
| **Available Models** | 依類型分組的模型清單，顏色標示（來自 `MODEL_ROUTING`） |
| **Feature Cards** | 三張特色卡片：Health-Aware Routing、Usage Tracking、Drop-In Compatible |

---

## `/dashboard` — 使用者 Dashboard

**檔案**：`dashboard.html` ← `web_ui.py`
**權限**：JWT scope 含 `read` 或 `admin`

### 頁面區塊

| 區塊 | 說明 |
|---|---|
| **System Status** | 所有下游伺服器健康狀態，依類型分組（LLM / VLM / Embedding / Reranker），即時顯示 ONLINE / DOWN |
| **Stats Cards** | 當月統計卡片：Requests 數、Estimated Cost、Budget Usage（進度條 + 百分比）、Total Tokens（Input / Output） |
| **Usage Trend** | Chart.js 折線圖，近 30 天用量趨勢，可切換 Requests / Cost (USD) 兩種視圖 |
| **My App Accounts** | 擁有的 App 帳號清單（僅 owner 可見），顯示 API key、月用量，可 Copy / Refresh Key |
| **API Integration Guide** | 內嵌程式碼範例：Chat Completions、Embeddings、Rerank、Score，含使用者的 API key 和實際 host |

### 可執行操作

| 操作 | 路徑 | 說明 |
|---|---|---|
| 查看 API Key | 按鈕 → Modal | 顯示完整 API key，可複製到剪貼簿 |
| Regenerate Key | `POST /dashboard/refresh-key` | 重新產生自己的 API key（舊 key 立即失效） |
| Refresh App Key | `POST /dashboard/app/{id}/refresh-key` | 重新產生所屬 App 帳號的 API key（僅 owner 可操作） |
| Copy App Key | 按鈕 | 複製 App 帳號的完整 API key |

---

## `/admin` — 管理面板

**檔案**：`admin.html` ← `admin.py`
**權限**：JWT scope 含 `admin`

### 頁面區塊

| 區塊 | 說明 |
|---|---|
| **Monthly Summary** | 全平台當月彙總：Total Requests、Total Cost、Input / Output Tokens |
| **App Leaderboard** | App 帳號排行榜（`app_*`），按月費用降序，顯示 Cost / Input / Output / Reqs |
| **User Leaderboard** | 使用者排行榜，按月費用降序，顯示 display_name、org_code badge、Admin badge |
| **Create App Account** | 建立新 App 帳號的表單：username（自動加 `app_` 前綴）、Daily Limit、Owner 下拉選單 |
| **User Management** | 全使用者表格：顯示 display_name（附 username 和 org_code）、角色（Admin / App / User）、API key、Total Spend、Daily Limit |

### 可執行操作

| 操作 | 路徑 | 說明 |
|---|---|---|
| Create App Account | `POST /admin/users/create` | 建立 `app_*` 帳號，可指定 owner 和每日上限 |
| Update Daily Limit | `POST /admin/users/{id}/limit` | 修改使用者的每日費用上限 |
| View API Key | 按鈕 → Modal | 查看任意使用者的完整 API key |
| Regenerate Key | `POST /admin/users/{id}/refresh-key` | 重新產生任意使用者的 API key |
| Delete User | `POST /admin/users/{id}/delete` | 刪除使用者及其所有 usage logs（不可刪除自己） |

---

## `/admin/models` — 模型設定

**檔案**：`admin_models.html` ← `admin.py`
**權限**：JWT scope 含 `admin`

### 頁面區塊

| 區塊 | 說明 |
|---|---|
| **Tab Navigation** | 按模型類型分頁：llm、vlm、embedding、vision_embedding、reranker、vision_reranker |
| **Model Table** | 每個類型下的模型清單，可編輯 Real Model、Base URL、API Key |
| **Fallback 下拉** | 每個類型可指定 fallback 模型（server down 時優先使用） |
| **Pricing Table** | Per-type 定價設定：Default + 各類型的 Input / Output Price (USD per 1M tokens) |

### 可執行操作

| 操作 | 路徑 | 說明 |
|---|---|---|
| Add Model | 按鈕 → prompt | 輸入 alias 後新增模型，預設 base_url 為 localhost:8000 |
| Edit Model | 表格內 inline 編輯 | 直接修改 Real Model、Base URL、API Key |
| Delete Model | 按鈕 | 刪除模型（同時清理相關 fallback） |
| Set Fallback | 下拉選單 | 選擇該類型的 fallback 模型 |
| Edit Pricing | 表格內 inline 編輯 | 修改 Input / Output 定價 |
| Save All | `PUT /admin/api/config` | 所有變更一次寫入 `config.toml` 並即時生效 |

---

## 共用元件

### `base.html` — Base Layout

所有頁面的共用框架：

- **Navbar** — Logo（App title 首字母）+ App title、Admin 連結（admin scope 時顯示）、Hello {display_name} + org_code badge、Logout 按鈕
- **Footer** — Copyright + 年份
- **CDN** — Tailwind CSS、Chart.js、Plus Jakarta Sans + JetBrains Mono 字體

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
| `read` | `/dashboard` |
| `admin` | `/dashboard`、`/admin`、`/admin/models` |

### 登出

Logout 按鈕指向 `/oauth2/sign_out`，由 oauth2-proxy 清除 session cookie 並導回登入頁。
