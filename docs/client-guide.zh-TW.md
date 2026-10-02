# Client 使用指南

[English](client-guide.md) · [回到 README](../README.zh-TW.md)

說明各種 client 怎麼接上 gateway：base URL、SDK 範例、各 client 的 tool calling 設定，以及會收到哪些回應需要處理。

所有請求都用你的 gateway API key 認證，可以放在 `Authorization: Bearer <key>` 或 `x-api-key: <key>` 標頭。API key 在 Web 儀表板上取得。

## Base URL

| Base URL | 服務範圍 | 列出的模型 |
|---|---|---|
| `http://your-gateway/v1` | 全部：地端 vLLM，加上你有權限的 Azure / AWS Bedrock 模型 | vLLM 模型；帳號有授權時另含 Azure、Bedrock 模型 |
| `http://your-gateway/azure/v1` | 只有 Azure OpenAI | 只有 Azure 模型 |
| `http://your-gateway/aws/v1` | 只有 AWS Bedrock | 只有 Bedrock 模型 |

**沒有特殊理由就用 `/v1`。** 選模型名稱就等於選後端，一個 base URL 全部涵蓋。只有在某個 client 絕對不能碰到其他後端時，才用 `/azure/v1` 或 `/aws/v1`。

所有路由也都接受不帶 `/v1` 的路徑（`/chat/completions`、`/messages`…），base URL 少寫 `/v1` 的 client 一樣能用。

模型名稱打錯在任何介面上都不會報錯：請求會由該介面的預設模型回答，回應帶 `X-Model-Fallback` 標頭說明。

## OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://your-gateway/v1", api_key="sk-your-api-key")

resp = client.chat.completions.create(
    model="my-model",              # GET /v1/models 列出的任一 alias
    messages=[{"role": "user", "content": "Hello!"}],
)

emb = client.embeddings.create(model="bge-m3", input=["The quick brown fox"])
```

## Anthropic SDK 與 Claude Code

```python
from anthropic import Anthropic

client = Anthropic(base_url="http://your-gateway", api_key="sk-your-api-key")  # gateway 的 key，不是 Anthropic 的
resp = client.messages.create(
    model="my-model",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}],
)
```

```bash
ANTHROPIC_BASE_URL=http://your-gateway \
ANTHROPIC_AUTH_TOKEN=sk-your-api-key \
claude
```

`/v1/messages` 適用所有後端：vLLM 模型會轉成 OpenAI chat completions，Azure 模型走 Responses API，Bedrock 模型走 Converse。串流、tool use、圖片、推理內容（`thinking` block）在每個後端都支援。下游長時間沒有輸出時，gateway 每 10 秒送一次 `ping`，Claude Code 不會因此斷線。串流若在生成途中被切斷，會以可重試的 `overloaded_error` 結束，不會假裝這一輪已經完成。

Windows 使用者可以在 Web UI 的 **Setup** 頁面下載已經設定好的 Claude Code 安裝程式。

## 其他端點

| 端點 | 用途 |
|---|---|
| `GET /v1/models` | 你的帳號可以用的模型 |
| `POST /v1/embeddings` | Embeddings（僅 vLLM） |
| `POST /v1/rerank`、`/v1/score` | Rerank |
| `POST /v1/messages/count_tokens` | 計算 Anthropic 請求的 token 數（不計費） |
| `POST /v1/tokenize` | vLLM tokenizer pass-through（不計費） |
| `POST /v1/chat/completions/render` | 查看模型實際收到的內容：套用 chat template 後的 prompt，不做生成，不計費。用法寫在 `/docs` |
| `POST /azure/v1/responses` | Azure Responses API 原生 pass-through，需要 `previous_response_id`、`store` 等功能時使用 |
| `POST /v1/systemone` | 型別化決策，見 [System One](#system-one) |

```bash
curl http://your-gateway/v1/rerank \
  -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"model": "bge-reranker-v2-m3", "query": "What is AI?", "documents": ["AI is...", "Machine learning is..."]}'
```

## Tool calling：選對 client 模式

兩條路徑對 tool call 的檢查嚴格程度不同。

### 地端 vLLM 模型：寬鬆

Gateway 直接轉送 chat completions，不檢查 tool call 和 tool result 是否配對。重點在**模型**：要用結構化 tool call，模型必須支援 native function calling（Qwen 2.5 以上、Llama 3.1 以上、Hermes、Mistral Large…）。

| Client | 模型支援 function calling | 模型不支援 |
|---|---|---|
| Roo Code | **OpenAI** provider，base URL `http://your-gateway/v1` | **OpenAI Compatible** provider，同一個 base URL |
| Cline / Continue.dev / Cursor | OpenAI provider | 多數沒有 XML 備案，請用支援 tool calling 的模型 |
| Claude Code | `ANTHROPIC_BASE_URL=http://your-gateway` | 同左 |

### Azure 模型：嚴格

所有 Azure 呼叫都走 Responses API，只要有 `function_call` 沒有對應的 `function_call_output` 就回 400。把結構化 tool call 和 inline 文字結果混在一起的 client 在這裡會出錯。Gateway 會記一筆 `Dropping N orphan function_call(s)` 並降級處理，讓對話還能繼續，但真正的解法是改 client 設定。

| Client | 設定方式 | 端點 |
|---|---|---|
| Claude Code / Anthropic SDK | `ANTHROPIC_BASE_URL=http://your-gateway`（或 `/azure`） | `/v1/messages` |
| Roo Code **OpenAI** provider（推薦） | Base URL `http://your-gateway/v1`，Custom Model ID 填 Azure alias | `/v1/chat/completions` |
| Roo Code Anthropic provider | Base URL 指向 gateway | `/v1/messages` |
| Roo Code **OpenAI Compatible** | ⚠️ **避免使用**：會混用 native tool call 和 inline 結果 | — |
| Cursor / Continue.dev / OpenAI SDK | `base_url=http://your-gateway/v1` | `/v1/chat/completions` |
| OpenAI SDK Responses API | 在 `/azure/v1` 上呼叫 `client.responses.create(...)` | `/azure/v1/responses`（這條路徑不會移除 sampling 參數） |

**原則：** Anthropic 風格的 client 打 `/messages`；OpenAI 風格的 client 打 `/chat/completions`；接 Azure 時絕對不要用混合 tool calling 風格的模式。

## 可能收到的回應

| 狀態碼 / 標頭 | 意思 | 怎麼處理 |
|---|---|---|
| `X-Model-Fallback` 標頭 | 實際回答的模型和你要求的不同（alias 不存在、伺服器離線，或雲端模型被限流） | 檢查模型名稱 |
| `X-Budget-Fallback` 標頭 | 你的 Azure/Bedrock 每日子額度用完，改由地端模型回答（需管理員開啟此功能） | — |
| `429` "Daily spending limit … exceeded" | 今日額度已用完，`Retry-After` 指向當地午夜 | 等明天，或請管理員調高額度 |
| `429` "Too many concurrent requests" | 你的帳號同時進行中的請求已達上限，`Retry-After: 5` | 稍後重試，Claude Code 會自動重試 |
| `403` "Azure / Bedrock access not granted" | 你的帳號沒有開通這個後端 | 請找管理員 |
| `403` "Account disabled" | 帳號已被停用 | 請找管理員 |

## System One

`systemone` 模型（例如 [Mapika 的 decider](https://huggingface.co/Mapika/decider-4b)）針對一段文字回答型別化的問題（`choice`、`score` 或是非題 `noul`），一次 forward pass 就回傳校準過的機率，不生成文字。Gateway 在 `/v1/systemone` 提供 TypeSafe 的格式，所以 [TypeSafe Python SDK](https://docs.typesafe.ai/sdk/python/) 不用改就能用：

```bash
pip install typesafe-sdk
export TYPESAFE_BASE_URL=http://your-gateway     # gateway 根網址，不要加 /v1（SDK 會自己補 /v1/systemone）
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
curl http://your-gateway/v1/systemone \
  -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"state": "I was charged twice.", "questions": {"refund": {"type": "noul", "instructions": "Does the customer ask for a refund?"}}}'
```

只依 input token 計費。
