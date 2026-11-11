# LLM Gateway

[![CI](https://github.com/bwinken/llm-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/bwinken/llm-gateway/actions/workflows/ci.yml)

[English](README.md)

一個同時相容 OpenAI 與 Anthropic API 的入口，統一接上地端 [vLLM](https://github.com/vllm-project/vllm) 伺服器、Azure OpenAI 與 AWS Bedrock。每位使用者有自己的 API key、每日預算和用量儀表板。

```
                                  ┌──▶ vLLM 伺服器    (LLM、VLM、embedding、reranker、System One)
Clients ──▶ LLM Gateway ──────────┼──▶ Azure OpenAI   (逐使用者授權)
 (OpenAI SDK、Anthropic SDK、      └──▶ AWS Bedrock    (逐使用者授權)
  Claude Code、Roo Code…)
```

| 儀表板：預算、用量、模型即時狀態 | 管理面板：花費、帳號、限制 |
|---|---|
| ![Dashboard](docs/screenshots/dashboard.png) | ![Admin panel](docs/screenshots/admin.png) |

## 它能做什麼

- **一個 base URL 接所有後端。** Client 呼叫 `/v1/*`，由模型名稱決定走 vLLM、Azure 或 Bedrock。
- **OpenAI 與 Anthropic API。** Chat completions、Responses、Messages（可直接搭配 Claude Code）、embeddings、rerank，都支援串流。
- **穩定不斷線。** 健康檢查、自動切換到同類型的其他伺服器，長時間推理時送 keepalive ping。
- **成本控管。** 每人每日預算、雲端花費另設上限、並行上限、逐模型計價。
- **存取與安全。** 逐人授權雲端模型、可選的 IP allowlist、prompt 篩檢（guardrail），以及記錄每個管理操作的稽核紀錄。
- **幫使用者修好設定。** 偵測設定有問題的 Claude Code，並在儀表板和信件裡給每位使用者一個修正設定的腳本。
- **人人可用的 Web UI。** 使用者看自己的預算、用量與模型狀態；管理員不用改檔案就能管理帳號、模型與價格。

## 快速開始

需要 Python 3.11、[uv](https://docs.astral.sh/uv/) 和 Docker（開發用資料庫）。

```bash
git clone https://github.com/bwinken/llm-gateway.git && cd llm-gateway
uv sync
cp config.toml.example config.toml    # 你的模型伺服器
cp .env.example .env                  # DATABASE_URL 與 AUTH_*
bash scripts/start-pg-dev.sh start    # 以 Docker 啟動開發用 PostgreSQL
uv run alembic upgrade head           # 建立資料表
uv run fastapi dev app/main.py
```

Web UI 登入需要把 [AuthCenter](https://github.com/bwinken/authcenter) 的 RS256 公鑰放在 `keys/public.pem`。Windows 若出現 `UnicodeEncodeError`，請先設定 `PYTHONUTF8=1`。

## 使用方式

每位使用者的 API key 在自己的儀表板上。

```python
from openai import OpenAI

client = OpenAI(base_url="https://your-gateway/v1", api_key="sk-your-api-key")
client.chat.completions.create(model="my-model", messages=[{"role": "user", "content": "Hello!"}])
```

```bash
# Claude Code
ANTHROPIC_BASE_URL=https://your-gateway ANTHROPIC_AUTH_TOKEN=sk-your-api-key claude
```

**[Client 指南](docs/client-guide.zh-TW.md)** 有完整內容：所有端點、Azure 與 Bedrock 的使用方式、Roo Code 與 Cursor 等工具的設定，以及 client 該處理的錯誤回應。

## 設定

設定檔有兩個。日常會調整的項目大多也能在管理面板裡改。

**`config.toml`：模型與價格。** 最簡單的模型設定：

```toml
[models.llm."my-model"]                # llm | vlm | embedding | reranker | ...
base_url   = "http://vllm-host:8000/v1"
real_model = "Qwen/Qwen3-32B"          # vLLM 伺服器上的模型名稱
```

Azure 與 Bedrock 模型放在 `[azure_models.*]`、`[bedrock_models.*]`；價格在 `[pricing]`；模型不存在或離線時改用哪個模型在 `[fallback]`。模型名稱在所有後端之間不能重複。[`config.toml.example`](config.toml.example) 說明了每個欄位，**Admin → Models** 也能直接編輯這個檔案。

**`.env`：連線與金鑰。** 最常需要設定的幾項：

| 變數 | 用途 |
|---|---|
| `DATABASE_URL` | PostgreSQL 連線字串 |
| `AUTH_BASE_URL`、`AUTH_CENTER_APP_ID`、`AUTH_CENTER_PUBLIC_KEY_PATH` | Web UI 登入：JWT 的 issuer、audience 與公鑰 |
| `AZURE_HTTP_PROXY`、`BEDROCK_HTTP_PROXY` | 雲端流量用的 HTTP proxy（地端流量不會走 proxy） |
| `BEDROCK_REGION`、`BEDROCK_AWS_ACCESS_KEY_ID`、`BEDROCK_AWS_SECRET_ACCESS_KEY` | 所有 Bedrock 模型共用的 AWS 設定 |
| `LANGFUSE_HOST`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` | 啟用 [Langfuse](https://langfuse.com) 追蹤 |
| `METRICS_ENABLED` | 在 `/metrics` 提供 Prometheus 指標 |
| `EXCHANGE_*`、`COMPANY_EMAIL_DOMAIN`、`PUBLIC_BASE_URL` | 寄信通知使用者修正 client 設定 |
| `USAGE_LOG_RETENTION_DAYS` | 刪除超過這個天數的原始用量紀錄（預設關閉；報表的總計會保留） |

[`.env.example`](.env.example) 列出所有變數與預設值：log、登入失敗封鎖、連線池、背景工作的間隔、寄信節奏。SSO 本身（OIDC issuer、client secret）是在 `deploy/.env` 設定給 oauth2-proxy，見 [deploy/README.zh-TW.md](deploy/README.zh-TW.md)。

## 管理

開啟 `https://your-gateway` 並透過 SSO 登入。在 AuthCenter 擁有 `admin` scope 的帳號會看到管理面板：

| 分頁 | 在這裡做什麼 |
|---|---|
| **Overview** | 查看花費、請求數與活躍使用者；檢視用量異常；匯出每月費用報表 |
| **Users** | 設定預算與雲端上限、授權 Azure 或 AWS、停用帳號、建立 App 帳號、查看或重新產生 key |
| **Clients** | 選擇要偵測哪些 client 問題、怎麼通知使用者、給他們什麼設定 patch；查看受影響的人和寄出的信 |
| **Security** | IP allowlist、prompt guardrail、稽核紀錄 |
| **Settings** | 預設預算、並行上限、雲端上限用完時的處理方式、網站連結 |
| **Models** | 新增與編輯模型、價格、備援；測試連線 |

## 部署

正式環境以 user-level systemd 服務執行，前面接 nginx 與 oauth2-proxy，PostgreSQL 跑在 Docker，詳見 [deploy/README.zh-TW.md](deploy/README.zh-TW.md)。每次升級都要先套用資料庫變更再重啟：

```bash
uv run alembic upgrade head
```

從舊的 SQLite 資料庫搬過來？請看[遷移指南](docs/migration-guide.md)。

## 開發

```bash
uv run pytest tests/ -q     # in-memory SQLite、模擬下游，不需要 config.toml
uv run ruff check .
```

CI 會在每個 pull request 跑這兩項，另外還有相依套件稽核與金鑰掃描。[CLAUDE.md](CLAUDE.md) 是貢獻者的地圖：東西放在哪裡，以及容易踩到的規則。

## 更多文件

| | |
|---|---|
| [Client 指南](docs/client-guide.zh-TW.md) | 連接 client、端點、錯誤處理 |
| [架構](docs/architecture.md) | 每個子系統的詳細行為 |
| [部署](deploy/README.zh-TW.md) | 正式環境設定、備份、升級 |
| [Langfuse](docs/langfuse-observability.md) | 可觀測性設計 |
| [Migrations](alembic/README.zh-TW.md) | 資料庫 schema 變更 |
| 各套件說明 | [core](app/core/README.zh-TW.md) · [routers](app/routers/README.zh-TW.md) · [models](app/models/README.zh-TW.md) · [templates](app/templates/README.zh-TW.md) · [tests](tests/README.md) |
