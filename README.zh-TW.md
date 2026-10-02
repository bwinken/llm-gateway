# LLM Gateway

[![CI](https://github.com/bwinken/llm-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/bwinken/llm-gateway/actions/workflows/ci.yml)

[English](README.md)

一個同時相容 OpenAI 與 Anthropic API 的入口，統一接上地端 [vLLM](https://github.com/vllm-project/vllm) 伺服器、Azure OpenAI 與 AWS Bedrock，並提供每位使用者的 API key、預算與用量計費。

```
                                  ┌──▶ vLLM 伺服器    (LLM、VLM、embedding、reranker、System One)
Clients ──▶ LLM Gateway ──────────┼──▶ Azure OpenAI   (逐使用者授權)
 (OpenAI SDK、Anthropic SDK、      └──▶ AWS Bedrock    (逐使用者授權)
  Claude Code、Roo Code…)
              │
              ├── 認證：client 用 API key，Web UI 用 SSO
              ├── 依模型名稱路由，健康狀態感知的自動備援
              ├── 每人每日預算與並行上限
              └── 用量與費用紀錄、Web 儀表板、管理面板
```

| 儀表板 | 管理面板 |
|---|---|
| ![Dashboard](docs/screenshots/dashboard.png) | ![Admin](docs/screenshots/admin.png) |

## 功能

- **一個 base URL 接所有後端。** `/v1/*` 同時提供 vLLM、Azure、Bedrock 模型，用模型名稱決定後端。另有 `/azure/v1/*`、`/aws/v1/*`，給只能使用單一後端的 client。
- **OpenAI 與 Anthropic API。** Chat completions、Responses、Messages（可直接搭配 Claude Code）、embeddings、rerank、token 計算，含串流；各後端支援的部分都能用。
- **高負載下也穩定。** 每 30 秒健康檢查、自動切換到同類型的其他伺服器，並送出 keepalive ping，長時間推理也不會逾時斷線。
- **成本控管。** 每人每日預算（可另設 Azure、Bedrock 子額度）、每人並行上限、逐模型計價（含 prompt cache 折扣價）。
- **存取控制。** 可停用帳號，也可逐人授權 Azure 或 Bedrock。
- **Web UI。** 使用者可查看自己的用量、剩餘預算與伺服器即時負載。管理員不用改檔案就能管理使用者、模型、價格與限制，並匯出每月費用報表。
- **可觀測性。** 可選用 [Langfuse](https://langfuse.com) 記錄每個請求，並提供 `/healthz`、`/readyz` 給負載平衡器檢查。

## 快速開始

需要 Python 3.11、[uv](https://docs.astral.sh/uv/)，以及 Docker（開發用資料庫）。

```bash
git clone https://github.com/bwinken/llm-gateway.git && cd llm-gateway
uv sync
cp config.toml.example config.toml    # 在這裡設定模型伺服器
cp .env.example .env                  # 在這裡設定 DATABASE_URL 與 AUTH_*
bash scripts/start-pg-dev.sh start    # 以 Docker 啟動開發用 PostgreSQL
uv run alembic upgrade head           # 建立資料表
uv run fastapi dev app/main.py
```

Web UI 登入需要把 [AuthCenter](https://github.com/bwinken/authcenter) 的 RS256 公鑰放在 `keys/public.pem`（或設定 `AUTH_CENTER_PUBLIC_KEY_PATH`）。Windows 若出現 `UnicodeEncodeError`，請先設定 `PYTHONUTF8=1`。

## 設定

### `config.toml`：模型與價格

最簡單的模型設定：

```toml
[models.llm."my-model"]                # 類型：llm | vlm | embedding | vision_embedding | reranker | vision_reranker | systemone
base_url   = "http://vllm-host:8000/v1"
real_model = "Qwen/Qwen3-32B"          # vLLM 伺服器上的模型名稱
api_key    = ""                        # vLLM 的 --api-key（有設才填）
```

| 區段 | 用途 |
|---|---|
| `[app]` | 新使用者的預設每日預算等執行期設定，多數可在管理面板調整 |
| `[models.<type>.<alias>]` | 地端 vLLM 模型。可選：逐模型價格、顯示用 metadata、`hidden`、reasoning effort 規則 |
| `[azure_models.<alias>]` | Azure OpenAI 部署（`endpoint`、`deployment`、`api_key`） |
| `[bedrock_models.<alias>]` | AWS Bedrock 模型（`region`、`model_id`、長期 Bedrock API key） |
| `[pricing]`、`[pricing.<type>]` | 預設與各類型價格（USD / 每百萬 token），模型自己的價格欄位優先 |
| `[fallback]`、`[azure_fallback]`、`[bedrock_fallback]` | 要求的模型不存在或離線時改用哪個模型 |

三個後端的模型名稱不能重複。每個欄位的說明都在 [`config.toml.example`](config.toml.example)。模型、價格與備援也可以在 **管理面板 → Model Config** 編輯，會自動寫回 `config.toml`。

### `.env`：環境變數

| 變數 | 用途 | 預設值 |
|---|---|---|
| `DATABASE_URL` | PostgreSQL 連線字串 | `postgresql://llm_gateway:password@localhost:5432/llm_gateway` |
| `AUTH_BASE_URL`、`AUTH_CENTER_APP_ID`、`AUTH_CENTER_PUBLIC_KEY_PATH` | Web UI 登入用的 JWT issuer、audience 與公鑰 | `auth-center`、`llm_gateway`、`./keys/public.pem` |
| `APP_TITLE` | UI 與 log 中顯示的服務名稱 | `LLM Gateway` |
| `AZURE_HTTP_PROXY`、`BEDROCK_HTTP_PROXY` | 只給雲端流量用的 HTTP proxy（vLLM 流量永遠不走 proxy） | 未設定 |
| `AZURE_INSECURE`、`BEDROCK_INSECURE` | 不驗證該雲端的 TLS 憑證（企業 TLS 檢查 proxy 用） | `false` |
| `LANGFUSE_HOST`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` | 啟用 Langfuse（三個都要設） | 未設定 |
| `LANGFUSE_CAPTURE_IO` | 也把 prompt 與回應**內容**送到 Langfuse。含個人資料，開啟前請先確認告知與同意 | `false` |
| `LANGFUSE_SAMPLE_RATE` | 記錄到 Langfuse 的比例，`0.0`–`1.0`（計費永遠記錄每一筆） | `1.0` |
| `ENFORCE_DAILY_LIMIT` | 設為 `false` 時，超過預算只記 log、不擋請求 | `true` |
| `SLOW_REQUEST_WARN_S` | 請求超過這個秒數就記一筆 warning（`0` 為關閉） | `120` |
| `LOG_DIR`、`LOG_LEVEL`、`LOG_ROTATION`、`LOG_RETENTION` | Log 檔，每個 worker 一個 | `./logs`、`WARNING`、`100 MB`、`14 days` |
| `LOG_REQUEST_BODIES` | 下游出錯時記錄原始請求內容。內含使用者 prompt，僅限除錯時暫時開啟 | `false` |
| `DOWNSTREAM_MAX_CONNECTIONS` | HTTP 連線池大小，每條進行中的串流佔一條連線 | `1000` |

SSO 登入本身（OIDC issuer、client secret）是在 `deploy/.env` 為 oauth2-proxy 設定，詳見 [deploy/README.md](deploy/README.md)。

## 使用方式

Client 用自己的 gateway API key 認證（在儀表板上可以看到）。

```python
from openai import OpenAI

client = OpenAI(base_url="http://your-gateway/v1", api_key="sk-your-api-key")
client.chat.completions.create(model="my-model", messages=[{"role": "user", "content": "Hello!"}])
```

```bash
# Claude Code
ANTHROPIC_BASE_URL=http://your-gateway ANTHROPIC_AUTH_TOKEN=sk-your-api-key claude
```

其他內容請見 **[Client 使用指南](docs/client-guide.zh-TW.md)**：所有端點、Azure 與 Bedrock 的使用方式、Roo Code、Cursor 等 client 的 tool calling 設定、client 需要處理的錯誤回應，以及 System One。

## 管理

開啟 `http://your-gateway` 並透過 SSO 登入。在 AuthCenter 擁有 `admin` scope 的帳號會看到管理面板，可以：

- 設定每位使用者的每日預算，以及選填的 Azure、Bedrock 子額度；
- 授權 Azure 或 Bedrock，或停用帳號；
- 設定每人**並行上限**（Off、Monitor、Enforce），並可個別豁免帳號；
- 決定雲端子額度用完時，是改由地端模型回答，還是回 429；
- 在 **Model Config** 編輯模型、價格與備援；
- 匯出每月費用報表（xlsx，或依後端拆分的 CSV），並查看用量異常。

## 部署

正式環境以 user-level systemd 服務執行，前面接 nginx 與 oauth2-proxy，PostgreSQL 跑在 Docker。詳見 [deploy/README.md](deploy/README.md)。

升級時，請先套用資料庫變更再重啟服務：

```bash
uv run alembic upgrade head
```

要從舊的 SQLite 資料庫搬過來，請看[搬遷指南](docs/migration-guide.md)。

## 開發

```bash
uv run pytest tests/ -q     # 測試：in-memory SQLite、下游全部 mock，不需要 config.toml
uv run ruff check .         # lint
```

每次 push 到 `main` 與每個 pull request，CI 都會跑這兩項。[CLAUDE.md](CLAUDE.md) 是給開發者的導覽：架構、慣例，以及容易踩到的規則。

| 文件 | |
|---|---|
| [Client 使用指南](docs/client-guide.zh-TW.md) | 接上 client、端點、錯誤處理 |
| [Architecture](docs/architecture.md) | 各子系統的詳細行為（英文） |
| [deploy/](deploy/README.zh-TW.md) | 正式環境部署 |
| [Langfuse](docs/langfuse-observability.md) | 可觀測性設計（英文） |
| [alembic/](alembic/README.zh-TW.md) | 資料庫 migration |
| [app/core](app/core/README.zh-TW.md)、[routers](app/routers/README.zh-TW.md)、[models](app/models/README.zh-TW.md)、[templates](app/templates/README.zh-TW.md)、[tests](tests/README.md) | 各套件說明 |
