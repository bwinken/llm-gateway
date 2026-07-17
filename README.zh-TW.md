# LLM Gateway

[English](README.md)

OpenAI 相容的反向代理閘道，可同時整合 [vLLM](https://github.com/vllm-project/vllm) 叢集與 Azure OpenAI 部署。
將客戶端應用程式的請求路由、代理並監控到下游 LLM/VLM/Embedding/Reranker 後端。

```
Client App ──▶ LLM Gateway ──▶ /v1/*      ──▶ vLLM Instance A  [LLM]
                    │                          ──▶ vLLM Instance B  [VLM]
                    │                          ──▶ vLLM Instance C  [Embedding]
                    │                          ──▶ vLLM Instance D  [Reranker]
                    │             /azure/v1/* ──▶ Azure OpenAI 部署
                    │
                    ├── 驗證 (API key / OAuth2 SSO)
                    ├── 路由 (模型別名 → vLLM 實例 / Azure deployment)
                    ├── 智慧容錯 (健康檢查感知，僅 vLLM)
                    ├── 用量記錄 (token 數 + 費用，兩條路徑共用)
                    └── 健康監控 (每 30 秒，僅 vLLM)
```

## 畫面截圖

| 歡迎頁面 | 儀表板 |
|---|---|
| ![Welcome](docs/screenshots/welcome.png) | ![Dashboard](docs/screenshots/dashboard.png) |

| 管理面板 | 模型設定 |
|---|---|
| ![Admin](docs/screenshots/admin.png) | ![Models](docs/screenshots/models.png) |

## 功能特色

- **統一 `/v1/*` 介面** — 同一個 base URL 同時暴露兩個後端。`/v1/chat/completions`、`/v1/messages`、`/v1/messages/count_tokens` 依 `model` alias 分派:預設走 vLLM,當 alias 配置在 `[azure_models.*]` 且 caller 有 `can_use_azure` 時轉向 Azure。`/v1/models` 對有權限的使用者會把 Azure alias 一併列出,讓 Claude Code 之類的 model picker 同時看到兩個後端。所有路由都另外註冊不含 `/v1` 前綴的別名(`/chat/completions`、`/messages`、…)
- **Anthropic Messages API** — `/v1/messages` 與 `/v1/messages/count_tokens`,可直接搭配 Anthropic Python SDK 與 Claude Code(後端可接任何 vLLM LLM/VLM;有 Azure 權限時也涵蓋 Azure 部署)。下游 `reasoning_content`(vLLM `--enable-reasoning`、DeepSeek、Qwen3-thinking)會轉成 Anthropic `thinking` content block;下游靜默時每 10 秒送一次 SSE `ping`,避免 Claude Code 在 reasoning prefill 太長時把連線視為斷線;串流中途下游斷線時會回傳可重試的 `overloaded_error`,而非謊稱該輪已完成
- **Azure OpenAI 後端** — 同一支客戶端、同一把 API key、同一套計費。可透過上面的統一 `/v1/*`(需 `can_use_azure`)或專屬的 `/azure/v1/*` 介面存取(chat completions、responses、Anthropic Messages、count_tokens;刻意不提供 embeddings,那是 vLLM 專責)
- **多模型路由** — LLM、VLM、Embedding、Vision Embedding、Reranker、Vision Reranker
- **SSE 串流** — 完整支援 Server-Sent Events（chat completions 和 responses）
- **智慧容錯** — 可設定各類型的備援模型，依健康檢查自動切換；回應標頭 `X-Model-Fallback`(僅 vLLM 路徑)
- **分級計價** — 各類型預設價格，並可在 vLLM 或 Azure 模型上加上 per-model 覆寫,包含 prompt cache 命中專用的折扣價 `cached_input_price_per_1m`(Azure 的快取 token 以較低費率計價)
- **用量追蹤** — 逐請求記錄每位使用者的 token 數與費用至 PostgreSQL（`/v1/*` 與 `/azure/v1/*` 共用）
- **OAuth2 SSO** — 整合 [AuthCenter](https://github.com/bwinken/authcenter)，RS256 JWT 驗證,自動建立使用者並套用可調整的預設每日額度
- **雙重驗證** — SDK/API 使用 Bearer API key，Web UI 使用 oauth2-proxy + JWT
- **使用者層級存取控制** — 管理員可將使用者停用（API key 與 JWT 認證皆會回 403,瀏覽器看到的是樣式化 HTML 頁、API 客戶端拿到 JSON 403),亦可用 `can_use_azure` 旗標單獨授權 Azure 部署的存取(admin 兩者皆免檢查)
- **Web 儀表板** — 用量統計、剩餘額度顯示、Chart.js 趨勢圖、分組的伺服器健康狀態,模型旁顯示管理員設定的 metadata badge(context window、tools、vision、cache),並即時顯示每台 vLLM 伺服器的負載(`running N · waiting M`,有請求排隊時轉琥珀色,過載時轉紅色警示);授權 Azure 存取後另有獨立的 Azure 存取卡片列出可用 Azure 別名
- **管理面板** — 使用者管理(每列有 Disable / Enable、Azure、Monitor、Delete 四顆按鈕)、排行榜、可在執行期調整的預設每日額度、模型設定 UI(路由/計價/容錯)
- **背景健康檢查** — 定期 ping 所有下游 vLLM 伺服器,並抓取每台存活伺服器的 Prometheus `/metrics`,取得即時的 running/waiting 請求數顯示於儀表板

## 技術堆疊

| 元件 | 技術 |
|---|---|
| 框架 | FastAPI (async ASGI) |
| HTTP 客戶端 | HTTPX (連線池) |
| 資料庫 | PostgreSQL + SQLModel |
| 資料遷移 | Alembic |
| 驗證 | PyJWT (RS256)、[AuthCenter](https://github.com/bwinken/authcenter) OAuth2 |
| 前端 | Jinja2 + Tailwind CSS (CDN) + Chart.js |
| 下游服務 | [vLLM](https://github.com/vllm-project/vllm) (OpenAI 相容) |

---

## 快速開始（開發環境）

### 1. Clone 並安裝

```bash
git clone https://github.com/bwinken/llm-gateway.git
cd llm-gateway
uv sync
```

### 2. 設定

```bash
cp config.toml.example config.toml
cp .env.example .env
```

編輯 `config.toml` — 設定下游 vLLM 伺服器 URL 和 API key（也可選擇加入 Azure OpenAI 部署）：

```toml
[app]
default_daily_limit_usd = 10.0   # 新使用者預設每日額度,管理員可在執行期調整

[models.llm."my-model"]
real_model = "Qwen/Qwen2.5-72B"
base_url = "http://your-llm-server:8000/v1"
api_key = "token-abc123"
# 可選的 per-model 計價覆寫(USD / 每百萬 token);不填則 fallback 到 [pricing.llm] 與 [pricing] 預設值
# input_price_per_1m = 0.50
# output_price_per_1m = 1.50

[azure_models."gpt-4o-mini-azure"]
type        = "llm"
endpoint    = "https://my-resource.openai.azure.com"
deployment  = "gpt-4o-mini"
api_key     = "azure-key-here"
api_version = "2024-08-01-preview"
```

編輯 `.env` — 設定資料庫 URL 和驗證相關設定：

```env
DATABASE_URL=postgresql://llm_gateway:your_password@localhost:5432/llm_gateway
AUTH_BASE_URL=http://auth.example.com
```

### 3. 啟動 PostgreSQL

```bash
bash scripts/start-pg-dev.sh start
```

### 4. 設定 AuthCenter 公鑰

將 AuthCenter RS256 公鑰放置於 `keys/public.pem`（或在 `.env` 中修改 `AUTH_CENTER_PUBLIC_KEY_PATH`）。

### 5. 啟動

```bash
uv run fastapi dev app/main.py
```

Gateway 會在 FastAPI 輸出的 port 啟動（dev 模式預設 8000）。

> **Windows：** 如果出現 `UnicodeEncodeError`，請先設定 `PYTHONUTF8=1`。

---

## 設定說明

### config.toml

模型路由與計價設定。每個模型將別名對應到下游 vLLM 實例：

```toml
[models.llm."model-alias"]
real_model = "actual-model-name"    # 傳送給 vLLM 的模型名稱
base_url = "http://host:port/v1"    # vLLM 伺服器 URL
api_key = "your-key"                # vLLM --api-key（若無則留空）
```

支援的模型類型：`llm`、`vlm`、`embedding`、`vision_embedding`、`reranker`、`vision_reranker`。

各類型計價（USD / 每百萬 token）：

```toml
[pricing.llm]
input_price_per_1m = 0.50
output_price_per_1m = 1.50
```

費用查詢順序：模型項目上的 `input_price_per_1m` / `output_price_per_1m` per-model 覆寫 → `[pricing.<type>]` → `[pricing]` 預設值。模型項目也可設定 `cached_input_price_per_1m`;設定後,後端回報的快取命中 input token(例如 Azure 的 `prompt_tokens_details.cached_tokens`)會以該折扣費率計價,未命中的 input 仍以全價計費。

各類型備援模型（選填，僅 vLLM 路徑）。當模型的伺服器離線時，優先使用此模型作為備援：

```toml
[fallback]
llm = "backup-llm"
vlm = "backup-vlm"
```

Azure OpenAI 部署（選填）。每個項目會以模型別名透過 `/azure/v1/*` 對外服務：

```toml
[azure_models."gpt-4o-mini-azure"]
type        = "llm"          # llm | vlm | embedding
endpoint    = "https://my-resource.openai.azure.com"
deployment  = "gpt-4o-mini"
api_key     = "azure-key"
api_version = "2024-08-01-preview"
```

> 所有模型路由、計價和容錯設定也可以透過 **管理面板 → 模型設定** 的 Web UI 管理，直接讀寫 `config.toml`。

### .env

| 變數 | 說明 | 預設值 |
|---|---|---|
| `APP_TITLE` | 顯示於 UI、瀏覽器分頁和日誌的服務名稱 | `LLM Gateway` |
| `DATABASE_URL` | PostgreSQL 連線字串 | `postgresql://llm_gateway:password@localhost:5432/llm_gateway` |
| `AUTH_CENTER_APP_ID` | JWT audience（AuthCenter 應用程式 ID） | `llm_gateway` |
| `AUTH_CENTER_PUBLIC_KEY_PATH` | RS256 公鑰路徑 | `./keys/public.pem` |
| `AUTH_BASE_URL` | JWT issuer URL（AuthCenter 基底 URL） | `auth-center` |
| `AZURE_HTTP_PROXY` | 選填,僅供 `/azure/v1/*` 下游流量使用的 HTTP proxy;支援內嵌帳密(`http://user:pass@proxy:8080`)。不設定則直連 Azure。vLLM 流量永遠不走 proxy。 | _(未設定)_ |
| `LANGFUSE_HOST` | Langfuse base URL(建議自架)。**三把(HOST + PUBLIC_KEY + SECRET_KEY)都設齊才啟用**,否則完全 no-op、零開銷。 | _(未設定)_ |
| `LANGFUSE_PUBLIC_KEY` | Langfuse public key(`pk-lf-…`)。 | _(未設定)_ |
| `LANGFUSE_SECRET_KEY` | Langfuse secret key(`sk-lf-…`)。 | _(未設定)_ |
| `LANGFUSE_CAPTURE_IO` | 設為 `true` 時,額外把 prompt/回應**內容**送進 Langfuse(Phase 2),含 PII,見下方 Observability。預設只送 metrics。 | `false` |
| `LANGFUSE_SAMPLE_RATE` | 送進 Langfuse 的取樣率,`0.0`–`1.0`(例如 `0.1` ≈ 記錄 10% 的請求)。`0.0` 完全不記錄;超出範圍會被 clamp,無法解析則回退 `1.0`。不影響 `usage_logs` 計費 —— 每筆請求照常計費。 | `1.0` |

> OAuth2 登入設定（OIDC issuer、client secret、redirect URL）在 `deploy/.env` 中設定，供 oauth2-proxy 使用。詳見 [deploy/README.md](deploy/README.md)。

### Observability(Langfuse)

選填。當 `LANGFUSE_HOST` + `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` 都設齊時,gateway 會為每筆計費請求送出一筆 Langfuse **generation** —— 非阻塞、錯誤吞掉、未設定時完全 no-op。

- **Metrics(永遠送,無 PII):** user、model alias、endpoint、token 用量、cost(由 gateway 自行計算,**不**讓 Langfuse 重新定價)、latency,以及 categorical scores:**client 軟體**(`claude-code` / `roo-code` / `openai-compatible` …,由 `User-Agent` + endpoint 推斷)、`empty_turn`、`fallback_used`。可做 per-user / per-model / per-client 分析(在 Users 視圖篩使用者;按 model 或 `client` score 分組;按天/月出圖)。
- **內容(需明確開啟,含 PII):** 設 `LANGFUSE_CAPTURE_IO=true` 後,額外把請求 messages 與助理回應掛到 generation(chat / messages / responses,vLLM + Azure;embedding/rerank/score 刻意只送 metrics)。**治理:** 擷取個別使用者的 prompt 屬於個人層級監看 —— 上 production 前請限制 Langfuse project 存取權限並確認告知/同意。
- **版本提醒:** 建構於 Langfuse Python SDK v4(OTel-based);上線前請確認 SDK ↔ 你的 Langfuse server 版本相容。

完整設計見 [docs/langfuse-observability.md](docs/langfuse-observability.md)。

---

## API 使用方式

所有 API 端點需要 `Authorization: Bearer <api_key>` 標頭。

### Chat Completions

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://your-gateway/v1",
    api_key="sk-your-api-key"
)

resp = client.chat.completions.create(
    model="my-model",
    messages=[{"role": "user", "content": "Hello!"}]
)
```

### Embeddings

```python
resp = client.embeddings.create(
    model="bge-m3",
    input=["The quick brown fox"]
)
```

### Rerank

```bash
curl http://your-gateway/v1/rerank \
  -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "bge-reranker-v2-m3",
    "query": "What is AI?",
    "documents": ["AI is...", "Machine learning is..."]
  }'
```

### Anthropic Messages API

`/v1/messages` 接收 Anthropic 格式的請求,根據 `model` 分派到對應後端:預設轉譯成 OpenAI 格式送往下游 vLLM(任何 LLM/VLM 模型);若 alias 配置在 `[azure_models.*]` 且 caller 有 `can_use_azure`,則改走 Azure Responses API。串流、tool use、圖片輸入兩個後端都支援。

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://your-gateway",
    api_key="sk-your-api-key",  # gateway 的 API key，不是 Anthropic 的
)

resp = client.messages.create(
    model="my-model",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}],
)
```

**Claude Code 支援。** 把 Claude Code 指向 gateway 即可使用任何本地 LLM 當後端：

```bash
ANTHROPIC_BASE_URL=http://your-gateway \
ANTHROPIC_AUTH_TOKEN=sk-your-api-key \
claude
```

Adapter 會處理 tool calls（`tool_use` ↔ OpenAI `tool_calls`）、圖片、system prompt、stop reason 對應、與串流 SSE 事件順序。下游回傳的 `reasoning_content`(vLLM `--enable-reasoning`、DeepSeek、Qwen3-thinking 等)會被轉成 Anthropic 的 `thinking` content block — 串流時送 `thinking_delta`,非串流時在 text block 前面多一個 `thinking` block。轉譯是雙向對稱的:請求歷史中 assistant 訊息內的 `thinking` block 會被帶回下游成為 `reasoning_content`,讓推理內容在多輪對話間保留而非被丟棄。對於在 `config.toml` 中標記 `is_reasoning = true` 的模型,adapter 還會把 Anthropic 的推理偏好(`effort` 字串或 `thinking` token 預算)轉成 OpenAI 的 `reasoning_effort`;非推理模型則永遠不會收到此參數。下游靜默時(reasoning prefill 太久、vLLM 排隊、header turnaround 慢)gateway 每 10 秒會送一個 Anthropic `event: ping`,讓 Claude Code 不會把連線判定為斷線。若串流下游在送出 finish reason 前就中途斷線,gateway 會回傳可重試的 `overloaded_error` 而非正常的 `message_stop`,讓客戶端重試而不是把截斷的回合當成已完成。`/v1/messages/count_tokens` 會轉送到下游的 tokenizer，讓 Claude Code 的 context-window 顯示維持精確。

### 列出模型

```bash
curl http://your-gateway/v1/models \
  -H "Authorization: Bearer sk-your-api-key"
```

### Azure OpenAI

設定在 `[azure_models.*]` 的 Azure 部署可以兩種方式存取,都用同一把 gateway API key:

1. **透過統一 `/v1/*` 介面**(有 `can_use_azure` 的使用者推薦) — 從 `/v1/models` 看到 Azure alias 就直接用,gateway 自動分派到 Azure。同一個 base URL 涵蓋兩個後端。
2. **透過專屬 `/azure/v1/*` 介面** — 適用於「該 client 永遠只該看到 Azure」的情境(例如要把某個 OpenAI 形 client 的 `base_url` 鎖在 Azure)。

Azure alias 只會在 `can_use_azure` 為 True 的使用者(admin 自動 bypass)看 `/v1/models` 時併入清單 — 這就是怎麼讓同一個 Claude Code base URL 同時暴露兩個後端,又不會把 Azure 部署洩漏給沒權限的使用者。

```python
# 方案 1:統一 base URL(有 can_use_azure 時看得到 vLLM + Azure 兩邊 alias)
client = OpenAI(
    base_url="http://your-gateway/v1",
    api_key="sk-your-api-key",
)
resp = client.chat.completions.create(
    model="gpt-4o-mini-azure",   # 即 [azure_models.<alias>] 的 alias
    messages=[{"role": "user", "content": "Hello!"}],
)

# 方案 2:Azure-only base URL
client = OpenAI(
    base_url="http://your-gateway/azure/v1",
    api_key="sk-your-api-key",
)
```

Anthropic SDK / Claude Code 同樣有兩種選擇 — 指向統一介面挑 Azure alias,或鎖在 `/azure`:

```bash
# 統一 — model picker 顯示 vLLM + Azure(有 can_use_azure)
ANTHROPIC_BASE_URL=http://your-gateway \
ANTHROPIC_AUTH_TOKEN=sk-your-api-key \
claude

# Azure-only
ANTHROPIC_BASE_URL=http://your-gateway/azure \
ANTHROPIC_AUTH_TOKEN=sk-your-api-key \
claude
```

如果 client 自己會講 Azure Responses API 原生格式，且想用 Responses-only 的功能（`previous_response_id`、`store: true`、`input` 帶 reasoning items 等等），可以直接打 `/azure/v1/responses` pass-through。Body 整包原樣轉發，gateway 只會把 `body.model` 從 alias 換成 Azure deployment 名稱：

```python
import httpx
resp = httpx.post(
    "http://your-gateway/azure/v1/responses",
    headers={"Authorization": "Bearer sk-your-api-key"},
    json={"model": "gpt-4o-mini-azure", "input": "Hello"},
)
```

### Client 設定建議

兩條路徑（vLLM `/v1/*` 跟 Azure `/azure/v1/*`）對 tool calling 嚴格度不同，該怎麼設要看你接的是哪個 backend。

#### vLLM 路徑（`/v1/*`）— 寬鬆，但模型本身要支援 tool calling

Gateway 對 vLLM 的呼叫是**直接 pass-through chat completions**，不驗證 tool call/result 的配對 — 怎麼丟給 vLLM，vLLM 就怎麼餵給 model，model 自己看著辦。所以這條路徑對 client 端的「混搭」風格也包容。

但是 model 端**得支援 native function calling** 才能用結構化 tool calls。常見支援的 model 有 Qwen 2.5、Llama 3.1+、Hermes、Mistral Large 等。如果你部署的是純 chat model（不會 emit `tool_calls`），只能走 XML inline 風格。

| Client | 模型支援 native function calling | 不支援 native function calling |
|---|---|---|
| **Roo Code** | **OpenAI** provider + Base URL = `http://your-gateway/v1` | **OpenAI Compatible** provider + 同 base URL |
| **Cline / Continue.dev / Cursor** | OpenAI provider + 同上 | 多數沒 XML 後備，需要確保 model 支援 |
| **Claude Code** | `ANTHROPIC_BASE_URL=http://your-gateway` 走 `/v1/messages` | 同左，vLLM 不嚴格驗證 |

#### Azure 路徑（`/azure/v1/*`）— 嚴格，client 不能混搭

Gateway 對 Azure 的所有呼叫都轉成 **Responses API**（`/openai/v1/responses`），這條路徑會嚴格驗證 tool call 跟 tool result 的配對 — 每個 `function_call` 必須有對應的 `function_call_output`。多數正規 client 會自動遵守這個規則，但有些 client 在某些設定下會混搭結構化呼叫跟 inline 文字結果，這種混搭會被 Azure 直接 400。

指錯的話 gateway 會在 log 輸出 `Dropping N orphan function_call(s)` WARNING 提醒，並啟動 safety-net 降級邏輯讓對話勉強跑下去 — 但**正確設定才是長久之計**。

| Client | 建議連線方式 | Gateway endpoint | 備註 |
|---|---|---|---|
| **Claude Code** | `ANTHROPIC_BASE_URL=http://your-gateway/azure` | `/azure/v1/messages` | Anthropic 原生格式，每個 `tool_use` 都嚴格配對 `tool_result` |
| **Anthropic Python SDK** | `Anthropic(base_url="http://your-gateway/azure")` | `/azure/v1/messages` | 同上 |
| **Roo Code「Anthropic」provider** | API base URL 指向 gateway | `/azure/v1/messages` | Roo Code 在 Anthropic 模式下使用嚴格 `tool_use`/`tool_result` 對應 |
| **Roo Code「OpenAI」provider**（推薦） | Base URL = `http://your-gateway/azure/v1`，Custom Model ID 填 alias | `/azure/v1/chat/completions` | 標準 OpenAI 規範，嚴格 `tool_calls`/`role:"tool"` 配對 — **這是 Roo Code 接 Azure 的推薦設定** |
| **Roo Code「OpenAI Compatible」** | ⚠️ **避免使用** | — | 該模式會混搭 native `tool_calls` 跟 user message 裡的 inline `<environment_details>` 文字結果，Azure Responses API 不接受這種混搭 |
| **Cursor / Continue.dev** | `base_url=http://your-gateway/azure/v1` | `/azure/v1/chat/completions` | 標準 OpenAI 格式 |
| **OpenAI Python SDK** | `OpenAI(base_url="http://your-gateway/azure/v1")` | `/azure/v1/chat/completions` | 同上 |
| **OpenAI Python SDK 1.40+ Responses API** | `OpenAI(base_url="http://your-gateway/azure/v1").responses.create(...)` | `/azure/v1/responses` | Responses 直接 pass-through;當你需要用到 `previous_response_id`、`store: true` 或其他 Responses 專屬功能時用。這條路徑**不會** strip sampling params,由 client 自己負責 |

#### Rule of thumb

- **Anthropic-flavour client → `/v1/messages` 或 `/azure/v1/messages`**（走 Anthropic Messages 翻譯）
- **OpenAI-flavour client → `/v1/chat/completions` 或 `/azure/v1/chat/completions`**（走 OpenAI Chat Completions 翻譯）
- **Azure 路徑請務必避免「混搭」mode**（典型例外：Roo Code「OpenAI Compatible」）— 兩種 tool calling 風格擇一，不要混
- **vLLM 路徑混搭沒事**，但前提是 model 跟 client 端對 tool calling 的支援能對上

### Web 儀表板

在瀏覽器開啟 `http://your-gateway`。oauth2-proxy 透過 AuthCenter 處理 SSO 登入。管理功能需要 AuthCenter 中的 `admin` scope。

---

## 部署

使用 user-level systemd 部署，PostgreSQL 以 Docker 執行。詳見 [deploy/README.md](deploy/README.md)。

### 開發用 PostgreSQL

```bash
bash scripts/start-pg-dev.sh start    # 啟動（首次會自動建立容器）
bash scripts/start-pg-dev.sh stop     # 停止（資料保留）
bash scripts/start-pg-dev.sh status   # 查看狀態
bash scripts/start-pg-dev.sh rm       # 刪除容器（資料遺失）
```

使用與 `.env.example` 相同的帳號密碼，無需額外設定。

### 資料遷移（SQLite → PostgreSQL）

```bash
# 1. 預覽遷移（不寫入任何資料）
uv run python scripts/migrate_sqlite_to_pg.py /path/to/llm_gateway.db --dry-run

# 2. 執行完整遷移
uv run python scripts/migrate_sqlite_to_pg.py /path/to/llm_gateway.db

# 3. 正式上線前增量同步（只遷移上次之後的新資料）
uv run python scripts/migrate_sqlite_to_pg.py /path/to/llm_gateway.db --sync
```

> `--sync` 會根據 PostgreSQL 中最新的 `usage_logs.created_at` 作為 cutoff，只遷移之後的記錄，並同步更新有變更的 user 欄位。原始 SQLite 檔案不會被修改。

完整的遷移步驟、注意事項和 checklist 請參考 **[Migration Guide](docs/migration-guide.md)**。

---

## 資料庫遷移

使用 [Alembic](https://alembic.sqlalchemy.org/) 進行 schema 遷移。會自動使用 `.env` 中的 `DATABASE_URL`。

```bash
# 套用所有待執行的遷移
uv run alembic upgrade head

# 修改 models/schema.py 後產生新的遷移
uv run alembic revision --autogenerate -m "describe your change"

# 查看目前遷移狀態
uv run alembic current
```

> 既有部署升級至 Alembic 時，執行一次 `uv run alembic stamp head` 即可將目前 schema 標記為最新，不會重新執行遷移。

---

## 測試

```bash
uv run pytest tests/ -v
```

測試使用記憶體內 SQLite 並模擬所有下游呼叫，不需要 PostgreSQL 或 vLLM 伺服器。

---

## 專案結構

```
llm-gateway/
├── config.toml.example        # 模型路由 + 計價範本
├── .env.example                # 環境變數範本
├── pyproject.toml              # 依賴與專案設定 (uv)
├── uv.lock                     # 鎖定的依賴版本
├── alembic.ini                 # Alembic 遷移設定
├── alembic/
│   ├── env.py                  # 遷移環境（讀取 DATABASE_URL）
│   └── versions/               # 遷移腳本
├── scripts/
│   ├── migrate_sqlite_to_pg.py # SQLite → PostgreSQL 遷移
│   ├── cleanup_usage_logs.py   # 用量記錄保留期清理
│   ├── add_owner_id.py         # 應用程式擁有者指派工具
│   └── start-pg-dev.sh         # 開發用 PostgreSQL 容器管理
├── docs/
│   ├── migration-guide.md      # SQLite → PostgreSQL 遷移指南
│   └── screenshots/            # UI 畫面截圖
├── app/
│   ├── main.py                 # FastAPI 應用程式、lifespan、middleware
│   ├── core/
│   │   ├── auth.py             # JWT 驗證 (get_web_user)
│   │   ├── config.py           # TOML → MODEL_ROUTING + PRICING_MAP
│   │   ├── database.py         # SQLModel engine + session
│   │   ├── deps.py             # Bearer token 驗證
│   │   ├── server_state.py     # httpx 客戶端 + 健康快取
│   │   └── logger.py
│   ├── models/
│   │   └── schema.py           # User + UsageLog + AppOwner 資料表
│   ├── routers/
│   │   ├── v1_api.py           # /v1/* 統一公開 API(vLLM 預設 + Azure 分派)
│   │   ├── azure_api.py        # /azure/v1/* Azure-only API
│   │   ├── web_ui.py           # 儀表板 (Jinja2)
│   │   └── admin.py            # 管理面板 + API
│   ├── services/
│   │   ├── vllm_proxy.py       # vLLM 代理 + 容錯 + 記錄
│   │   ├── azure_proxy.py      # Azure OpenAI 代理(共用認證/計費/觀測)
│   │   ├── anthropic_adapter.py # Anthropic Messages 轉譯(兩個後端共用)
│   │   ├── stats.py            # 儀表板彙總
│   │   └── health.py           # 背景健康檢查迴圈
│   └── templates/
│       ├── base.html
│       ├── welcome.html        # 登入 / 歡迎頁面
│       ├── dashboard.html
│       ├── admin.html          # 使用者管理 + 排行榜
│       └── admin_models.html   # 模型設定 UI
├── deploy/
│   ├── docker-compose.yml      # PostgreSQL + oauth2-proxy
│   ├── .env.example            # Docker 服務環境變數
│   ├── setup.sh                # 部署腳本
│   ├── llm-gateway.service     # systemd 單元
│   ├── llm-gateway-example.nginx.conf  # Nginx 設定 (auth_request)
│   └── README.md               # 部署指南
└── tests/
    ├── conftest.py
    ├── test_chat_completions.py
    ├── test_embeddings.py
    ├── test_rerank_score.py
    ├── test_responses.py
    ├── test_vlm.py
    ├── test_vision_embedding.py
    ├── test_vision_rerank_score.py
    ├── test_admin.py
    └── test_app_ownership.py
```
