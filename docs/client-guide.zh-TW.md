# Client 使用指南

[English](client-guide.md) · [回到 README](../README.zh-TW.md)

說明各種 client 怎麼接上 gateway：base URL、SDK 範例、各 client 的 tool calling 設定，以及會收到哪些回應需要處理。

所有請求都用你的 gateway API key 認證，可以放在 `Authorization: Bearer <key>` 或 `x-api-key: <key>` 標頭。API key 在 Web 儀表板上取得。

請使用 `https://`。打到 `http://` 的請求會被轉址（308），但有些 client 在轉址時會丟掉 API key 或 request body，所以請直接把 base URL 設成 `https://`，不要依賴轉址。

## Base URL

| Base URL | 服務範圍 | 列出的模型 |
|---|---|---|
| `https://your-gateway/v1` | 全部：地端 vLLM，加上你有權限的 Azure / AWS Bedrock 模型 | vLLM 模型；帳號有授權時另含 Azure、Bedrock 模型 |
| `https://your-gateway/azure/v1` | 只有 Azure OpenAI | 只有 Azure 模型 |
| `https://your-gateway/aws/v1` | 只有 AWS Bedrock | 只有 Bedrock 模型 |

**沒有特殊理由就用 `/v1`。** 選模型名稱就等於選後端，一個 base URL 全部涵蓋。只有在某個 client 絕對不能碰到其他後端時，才用 `/azure/v1` 或 `/aws/v1`。

所有路由也都接受不帶 `/v1` 的路徑（`/chat/completions`、`/messages`…），base URL 少寫 `/v1` 的 client 一樣能用。

模型名稱打錯在任何介面上都不會報錯：請求會由該介面的預設模型回答，回應帶 `X-Model-Fallback` 標頭說明。

## OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="https://your-gateway/v1", api_key="sk-your-api-key")

resp = client.chat.completions.create(
    model="my-model",              # GET /v1/models 列出的任一 alias
    messages=[{"role": "user", "content": "Hello!"}],
)

emb = client.embeddings.create(model="bge-m3", input=["The quick brown fox"])
```

## Anthropic SDK 與 Claude Code

```python
from anthropic import Anthropic

client = Anthropic(base_url="https://your-gateway", api_key="sk-your-api-key")  # gateway 的 key，不是 Anthropic 的
resp = client.messages.create(
    model="my-model",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}],
)
```

```bash
ANTHROPIC_BASE_URL=https://your-gateway \
ANTHROPIC_AUTH_TOKEN=sk-your-api-key \
claude
```

`/v1/messages` 適用所有後端：vLLM 模型會轉成 OpenAI chat completions，Azure 模型走 Responses API，Bedrock 模型走 Converse。串流、tool use、圖片、推理內容（`thinking` block）在每個後端都支援。下游長時間沒有輸出時，gateway 每 10 秒送一次 `ping`，Claude Code 不會因此斷線。串流若在生成途中被切斷，會以可重試的 `overloaded_error` 結束，不會假裝這一輪已經完成。

Windows 使用者可以在 Web UI 的 **Setup** 頁面下載已經設定好的 Claude Code 安裝程式（使用者不在 Windows 上的站台可能已關閉這個頁面）。

## 其他端點

| 端點 | 用途 |
|---|---|
| `GET /v1/models` | 你的帳號可以用的模型 |
| `GET /v1/me`、`/me` | 你的權限（地端 / Azure / AWS）和今日額度：上限、已用、剩餘、重置時間。額度用完時一樣可以查。見[查詢額度](#查詢額度) |
| `GET /v1/status` | 你可以用的模型，以及每個模型是否正常、目前負載和價格。見[模型狀態](#模型狀態) |
| `GET /v1/usage` | 你每天的用量，以及今天和本月各模型的用量。見[用量紀錄](#用量紀錄) |
| `GET /v1/apps` | 你名下的 app 帳號，以及各自的上限和本月用量。見[App 帳號](#app-帳號) |
| `GET /v1/notices` | dashboard 要你修正的 client 設定。見[通知](#通知) |
| `POST /auth/api-key` | 用 AuthCenter 的 access token 換你的 API key，terminal 工具的登入方式。見[從 terminal 工具登入](#從-terminal-工具登入) |
| `POST /v1/embeddings` | Embeddings（僅 vLLM） |
| `POST /v1/rerank`、`/v1/score` | Rerank |
| `POST /v1/messages/count_tokens` | 計算 Anthropic 請求的 token 數（不計費） |
| `POST /v1/tokenize` | vLLM tokenizer pass-through（不計費） |
| `POST /v1/chat/completions/render` | 查看模型實際收到的內容：套用 chat template 後的 prompt，不做生成，不計費。用法寫在 `/docs` |
| `POST /azure/v1/responses` | Azure Responses API 原生 pass-through，需要 `previous_response_id`、`store` 等功能時使用 |
| `POST /v1/systemone` | 型別化決策，見 [System One](#system-one) |

```bash
curl https://your-gateway/v1/rerank \
  -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"model": "bge-reranker-v2-m3", "query": "What is AI?", "documents": ["AI is...", "Machine learning is..."]}'
```

## 查詢額度

```bash
curl https://your-gateway/v1/me -H "Authorization: Bearer sk-your-api-key"
```

```json
{
  "object": "account",
  "username": "alice",
  "display_name": "Alice",
  "is_admin": false,
  "legacy_key": false,
  "daily": {
    "limit_usd": 10.0, "used_usd": 3.25, "remaining_usd": 6.75,
    "requests": 42, "input_tokens": 120000, "output_tokens": 18000,
    "enforced": true, "timezone": "Asia/Taipei",
    "resets_at": "2026-10-14T00:00:00+08:00", "resets_in_seconds": 30120
  },
  "backends": {
    "onprem": {"access": true,                        "used_usd": 1.25, "remaining_usd": 6.75},
    "azure":  {"access": true,  "used_usd": 2.0,  "remaining_usd": 0.5},
    "aws":    {"access": false,                       "used_usd": 0.0,  "remaining_usd": 0.0}
  },
  "cloud": {"limit_usd": 2.5, "used_usd": 2.0, "remaining_usd": 0.5},
  "cloud_budget_fallback": false,
  "concurrency": {"mode": "enforce", "limit": 8}
}
```

- `daily` 是今日的總額度，所有後端的花費都算在裡面。一天以 `Asia/Taipei` 的日曆日計算。
- 每個帳號都有每日上限，沒有「不限」。上限為 `0` 代表額度是 $0，所有請求都會被拒絕。
- `backends.<name>.remaining_usd` 是今天在該後端還能花多少：Azure / AWS 取雲端額度剩餘和總額度剩餘中較小的那個，地端就是總額度剩餘；沒有權限時為 `0`。各後端的剩餘共用同一份額度，不能相加：以上面的例子，在 Azure 花掉 $0.50 之後，地端只剩 $6.25。
- `cloud` 只在你有雲端權限、且有雲端每日上限（管理員設的預設值，或單獨為你設的值）時才會出現：今天 Azure 和 AWS 合計還能花多少，兩邊自由分配。它也會限制兩個雲端後端的 `remaining_usd`（以上面的例子，雲端只剩 $0.50）。沒有這個欄位時，雲端只受 `daily` 總額度約束。地端沒有自己的上限。
- 只有在 `used_usd` 已達上限時才會拒絕請求，所以當天最後一個請求可能會稍微超出上限。
- `enforced: false` 代表超額時 gateway 只記 log，不會回 429。
- `cloud_budget_fallback: true` 代表雲端每日上限用完時，`/v1/*` 的請求會改由地端模型回答，而不是回 429。
- `legacy_key: true` 代表你的 key 是較舊、較弱的格式，方便時請到 dashboard 重新產生。任何端點都不會回傳 key 本身。
- `concurrency.limit` 是你的 key 同時可以進行幾個請求。`"mode": "off"` 代表不限，此時不帶 `limit`。

## 模型狀態

```bash
curl https://your-gateway/v1/status -H "Authorization: Bearer sk-your-api-key"
```

```json
{
  "object": "status",
  "onprem": {"total": 3, "up": 2, "down": ["glm-4.6"]},
  "models": [
    {"id": "qwen3-coder", "backend": "onprem", "type": "llm", "state": "up",
     "running": 2, "waiting": 0, "display_name": "Qwen3 Coder", "context_window": 131072,
     "input_price_per_1m": 0.5, "output_price_per_1m": 1.5},
    {"id": "glm-4.6", "backend": "onprem", "type": "llm", "state": "down",
     "input_price_per_1m": 0.5, "output_price_per_1m": 1.5},
    {"id": "bge-m3", "backend": "onprem", "type": "embedding", "state": "up",
     "input_price_per_1m": 0.02, "output_price_per_1m": 0.0},
    {"id": "gpt-5", "backend": "azure", "type": "llm", "state": "unchecked",
     "context_window": 400000, "input_price_per_1m": 1.25, "output_price_per_1m": 10.0}
  ]
}
```

- `models` 列出你的 key 看得到的每個模型，所有類型都有（`/v1/models` 只列對話模型）：先是地端，再來是你有權限的雲端後端，順序和 dashboard 一樣。
- `backend` 是 `onprem`、`azure` 或 `aws`，和 `/v1/me` 用的名稱相同。
- `state`：地端模型是 `up` 或 `down`，也就是 gateway 健康檢查最近一次看到的狀態；雲端模型是 `unchecked`，因為 gateway 不探測雲端。
- `running` / `waiting` 是地端伺服器正在處理和排隊中的請求數，只有伺服器有回報時才會出現。
- `input_price_per_1m` / `output_price_per_1m` 是實際計費的價格，單位是每 100 萬 token 的美元。
- `display_name`、`context_window`、`max_output_tokens` 和能力旗標（`supports_tools`、`supports_vision`、`supports_prompt_caching`、`is_reasoning`）在管理員有設定時才會出現，和 `/v1/models` 相同。欄位不會是 `null`：沒有值時就沒有這個 key。
- `onprem` 是地端模型的統計：總數、正常的數量、以及離線模型的名稱。
- 指定的模型離線時，只要同類型還有模型正常，請求會由那個模型回答（回應會帶 `X-Model-Fallback`）。

## 用量紀錄

```bash
curl "https://your-gateway/v1/usage?days=3" -H "Authorization: Bearer sk-your-api-key"
```

```json
{
  "object": "usage",
  "timezone": "Asia/Taipei",
  "days": [
    {"date": "2026-10-11", "requests": 0,  "input_tokens": 0,      "output_tokens": 0,     "cost_usd": 0.0},
    {"date": "2026-10-12", "requests": 31, "input_tokens": 98000,  "output_tokens": 12500, "cost_usd": 4.1},
    {"date": "2026-10-13", "requests": 42, "input_tokens": 120000, "output_tokens": 18000, "cost_usd": 3.25}
  ],
  "today": {
    "date": "2026-10-13",
    "requests": 42, "input_tokens": 120000, "output_tokens": 18000, "cost_usd": 3.25,
    "models": [
      {"model": "qwen3-coder", "backend": "onprem", "type": "llm",
       "requests": 30, "input_tokens": 90000, "output_tokens": 13000, "cost_usd": 1.25},
      {"model": "gpt-5", "backend": "azure", "type": "llm",
       "requests": 12, "input_tokens": 30000, "output_tokens": 5000, "cost_usd": 2.0}
    ]
  },
  "month": {
    "month": "2026-10",
    "requests": 412, "input_tokens": 1310000, "output_tokens": 164000, "cost_usd": 42.1,
    "models": [
      {"model": "qwen3-coder", "backend": "onprem", "type": "llm",
       "requests": 380, "input_tokens": 1200000, "output_tokens": 150000, "cost_usd": 30.0},
      {"model": "gpt-5", "backend": "azure", "type": "llm",
       "requests": 32, "input_tokens": 110000, "output_tokens": 14000, "cost_usd": 12.1}
    ]
  }
}
```

- `days` 每個 `Asia/Taipei` 日曆日一筆，由舊到新，最後一筆是今天（仍在累計）。沒有用量的日子也會出現，數值為 0。`?days=` 決定回傳幾天：1 到 90，預設 7。
- `today` 和 `month` 分別是今天和本月到目前為止的合計，`models` 依模型和後端分列，順序和 dashboard 一樣。
- 和 dashboard 一樣，`today` 和 `month` 不包含你已經沒有權限的雲端後端；`days` 則全部都算，和每日額度的算法一致。

## App 帳號

```bash
curl https://your-gateway/v1/apps -H "Authorization: Bearer sk-your-api-key"
```

```json
{
  "object": "apps",
  "month": "2026-10",
  "apps": [
    {"username": "app_batch", "daily_limit_usd": 50.0, "legacy_key": false,
     "month": {"requests": 1200, "input_tokens": 4100000, "output_tokens": 380000, "cost_usd": 61.5}}
  ]
}
```

- `apps` 列出你名下的 app 帳號（沒有的話是空陣列），各自有自己的每日上限和本月用量。
- 不包含 app 的 API key，任何端點都不會回傳 key。要查看或重新產生 app 的 key，請到 dashboard。

## 通知

```bash
curl https://your-gateway/v1/notices -H "Authorization: Bearer sk-your-api-key"
```

```json
{
  "object": "notices",
  "window_days": 15,
  "notices": [
    {"id": "CC001", "title": "Auto mode needs a setting on this gateway",
     "message": "Claude Code is waiting for a server-side check this gateway does not provide.",
     "last_seen_at": "2026-10-13T09:12:00+08:00", "client_version": "2.1.280",
     "patch": {"set": {"CLAUDE_CODE_AUTO_MODE_SERVER": "0", "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"},
               "unset": []}},
    {"id": "CC003", "title": "Update Claude Code",
     "message": "Your Claude Code is older than this gateway supports.",
     "last_seen_at": "2026-10-12T17:40:00+08:00", "client_version": "2.1.280"}
  ],
  "patch": {
    "set": {"CLAUDE_CODE_AUTO_MODE_SERVER": "0", "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"},
    "unset": [],
    "covers": ["CC001"],
    "manual": ["CC003"]
  }
}
```

- `notices` 就是 dashboard 通知區塊顯示的內容：最近 `window_days` 天內 gateway 在你的 client 請求裡看到、而你還沒回報已修正的問題。沒有待辦時是空陣列。
- 通知的 `patch` 是它的修正方式（前提是能靠設定修正）：要在 `~/.claude/settings.json` 的 `env` 區塊裡設定的變數（`set`），和要從裡面移除的變數（`unset`）。沒有 `patch` 的通知要用別的方式處理（更新 Claude Code、申請權限）。
- 最上層的 `patch` 是實際要套用的那一份：把所有未處理的項目合併，再加上每個帳號都該有的設定。`covers` 是它能修正的通知，`manual` 是它修不了的。沒有待辦時不會出現。patch 裡不會有 key。
- 這個端點只能讀。套用修正後，請到 dashboard 按 *I've applied it*；gateway 連續 `window_days` 天沒再看到同樣的問題時，通知也會自己消失。

狀態列、terminal 工具或腳本要讀的就是 `/v1/me`、`/v1/status`、`/v1/usage`、`/v1/apps`、`/v1/notices`，合起來就是 dashboard 上看得到的資訊。它們在額度用完時一樣可以查，都不計費，也都不會回傳 API key：key 只會在登入之後交出（見下一節，或 dashboard）。

## 從 terminal 工具登入

不是瀏覽器的程式（terminal 工具、TUI）可以直接取得你的 API key，不用你到 dashboard 複製：

1. 它用 AuthCenter 給命令列程式的登入流程讓你登入，拿到給 gateway 的 access token，也就是你在網頁登入時 Web UI 拿到的那一種 token。
2. 它拿這個 token 換你的 API key，只換一次：

```bash
curl -X POST https://your-gateway/auth/api-key -H "Authorization: Bearer $AUTHCENTER_ACCESS_TOKEN"
```

```json
{"object": "api_key", "username": "alice", "api_key": "sk-your-api-key", "legacy_key": false}
```

3. 之後它就和其他 client 一樣用 API key：呼叫模型，以及讀 `/v1/me`、`/v1/status`、`/v1/usage`、`/v1/apps`、`/v1/notices`。access token 不會再用到，其他端點也不接受它，用完就丟。

- 拿到的永遠是你現有的 key。在第二台機器登入，不會讓第一台的 key 失效。
- 第一次登入會建立帳號，和第一次開 dashboard 一樣。
- 之後如果 API 回 401，代表 key 被重新產生過：重新登入、再換一次即可。
- `401`：token 沒帶、過期，或不是 AuthCenter 發的。`403`：token 沒有 `read` scope、帳號被停用（且 token 沒有 `admin` scope），或你的位址不允許使用 API（訊息會說明是哪一種）。`429`：你的位址送出太多錯誤的 token 或 key，請等 `Retry-After` 之後再試。
- 每一次這樣交出 key，都會記在 gateway 的稽核紀錄裡。

## Tool calling：選對 client 模式

兩條路徑對 tool call 的檢查嚴格程度不同。

### 地端 vLLM 模型：寬鬆

Gateway 直接轉送 chat completions，不檢查 tool call 和 tool result 是否配對。重點在**模型**：要用結構化 tool call，模型必須支援 native function calling（Qwen 2.5 以上、Llama 3.1 以上、Hermes、Mistral Large…）。

| Client | 模型支援 function calling | 模型不支援 |
|---|---|---|
| Roo Code | **OpenAI** provider，base URL `https://your-gateway/v1` | **OpenAI Compatible** provider，同一個 base URL |
| Cline / Continue.dev / Cursor | OpenAI provider | 多數沒有 XML 備案，請用支援 tool calling 的模型 |
| Claude Code | `ANTHROPIC_BASE_URL=https://your-gateway` | 同左 |

### Azure 模型：嚴格

所有 Azure 呼叫都走 Responses API，只要有 `function_call` 沒有對應的 `function_call_output` 就回 400。把結構化 tool call 和 inline 文字結果混在一起的 client 在這裡會出錯。Gateway 會記一筆 `Dropping N orphan function_call(s)` 並降級處理，讓對話還能繼續，但真正的解法是改 client 設定。

| Client | 設定方式 | 端點 |
|---|---|---|
| Claude Code / Anthropic SDK | `ANTHROPIC_BASE_URL=https://your-gateway`（或 `/azure`） | `/v1/messages` |
| Roo Code **OpenAI** provider（推薦） | Base URL `https://your-gateway/v1`，Custom Model ID 填 Azure alias | `/v1/chat/completions` |
| Roo Code Anthropic provider | Base URL 指向 gateway | `/v1/messages` |
| Roo Code **OpenAI Compatible** | ⚠️ **避免使用**：會混用 native tool call 和 inline 結果 | — |
| Cursor / Continue.dev / OpenAI SDK | `base_url=https://your-gateway/v1` | `/v1/chat/completions` |
| OpenAI SDK Responses API | 在 `/azure/v1` 上呼叫 `client.responses.create(...)` | `/azure/v1/responses`（這條路徑不會移除 sampling 參數） |

**原則：** Anthropic 風格的 client 打 `/messages`；OpenAI 風格的 client 打 `/chat/completions`；接 Azure 時絕對不要用混合 tool calling 風格的模式。

## 可能收到的回應

| 狀態碼 / 標頭 | 意思 | 怎麼處理 |
|---|---|---|
| `X-Model-Fallback` 標頭 | 實際回答的模型和你要求的不同（alias 不存在、伺服器離線，或雲端模型被限流） | 檢查模型名稱 |
| `X-Budget-Fallback` 標頭 | 你的雲端每日額度用完，改由地端模型回答（需管理員開啟此功能） | — |
| `429` "Daily spending limit … exceeded" | 今日額度已用完，`Retry-After` 指向當地午夜 | 等明天，或請管理員調高額度 |
| `429` "Daily spending limit is $0" | 你的帳號額度是 $0，等明天也沒用 | 請管理員設定額度 |
| `429` "Too many concurrent requests" | 你的帳號同時進行中的請求已達上限，`Retry-After: 5` | 稍後重試，Claude Code 會自動重試 |
| `403` "Azure / Bedrock access not granted" | 你的帳號沒有開通這個後端 | 請找管理員 |
| `403` "Account disabled" | 帳號已被停用 | 請找管理員 |
| `429` "Too many failed authentication attempts from this address" | 你的位址送出太多把不同的錯誤 key，被暫時封鎖（見 `Retry-After`），期間連正確的 key 也會被拒絕 | 先修正 client 裡的 key，再等封鎖結束 |
| `502` "Downstream error (ConnectError)"（或其他錯誤類型） | 連不上模型伺服器或它出錯；細節記在 gateway 的 log，不會出現在回應裡 | 重試；持續發生請把時間與模型告訴管理員 |

## System One

`systemone` 模型（例如 [Mapika 的 decider](https://huggingface.co/Mapika/decider-4b)）針對一段文字回答型別化的問題（`choice`、`score` 或是非題 `noul`），一次 forward pass 就回傳校準過的機率，不生成文字。Gateway 在 `/v1/systemone` 提供 TypeSafe 的格式，所以 [TypeSafe Python SDK](https://docs.typesafe.ai/sdk/python/) 不用改就能用：

```bash
pip install typesafe-sdk
export TYPESAFE_BASE_URL=https://your-gateway     # gateway 根網址，不要加 /v1（SDK 會自己補 /v1/systemone）
export TYPESAFE_API_KEY=sk-your-api-key
export TYPESAFE_DEFAULT_MODEL=decider-4b         # [models.systemone.*] 底下的 alias
```

```python
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

with TypeSafeClient() as client:
    result = client.system_one(
        state="I was charged twice for order A-104. Please refund the duplicate.",
        questions={
            "department": Choice(instructions="Which team should handle this?",
                                 criteria={"billing": "Charges, invoices", "returns": "Exchanges, refunds", "other": None}),
            "refund_requested": Noul(instructions="Does the customer ask for a refund?"),
            "frustration": Score(instructions="How frustrated is the customer?",
                                 criteria=["calm", "frustrated", "very frustrated"]),
        },
    )
    print(result.choices["department"].choice, result.nouls["refund_requested"].noul)
```

- 沒設 `TYPESAFE_DEFAULT_MODEL` 時，SDK 會要求 `jev-latest`。Gateway 仍會用 systemone 的預設模型回答，但每次回應都會帶 `X-Model-Fallback`，並記一筆 warning。
- `/v1/models` 只列聊天模型，所以不支援 `client.models.list()`。
- 直接打 HTTP 也可以，而且 `model` 可以省略：

```bash
curl https://your-gateway/v1/systemone \
  -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"state": "I was charged twice.", "questions": {"refund": {"type": "noul", "instructions": "Does the customer ask for a refund?"}}}'
```

只依 input token 計費。
