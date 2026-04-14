# LLM Gateway

[English](README.md)

OpenAI 相容的反向代理閘道，專為 [vLLM](https://github.com/vllm-project/vllm) 叢集設計。
將客戶端應用程式的請求路由、代理並監控到下游 vLLM 實例（LLM、VLM、Embedding、Reranker）。

```
Client App ──▶ LLM Gateway ──▶ vLLM Instance A  [LLM]
                    │                 ──▶ vLLM Instance B  [VLM]
                    │                 ──▶ vLLM Instance C  [Embedding]
                    │                 ──▶ vLLM Instance D  [Reranker]
                    │
                    ├── 驗證 (API key / OAuth2 SSO)
                    ├── 路由 (模型別名 → vLLM 實例)
                    ├── 智慧容錯 (健康檢查感知)
                    ├── 用量記錄 (token 數 + 費用)
                    └── 健康監控 (每 30 秒)
```

## 畫面截圖

| 歡迎頁面 | 儀表板 |
|---|---|
| ![Welcome](docs/screenshots/welcome.png) | ![Dashboard](docs/screenshots/dashboard.png) |

| 管理面板 | 模型設定 |
|---|---|
| ![Admin](docs/screenshots/admin.png) | ![Models](docs/screenshots/models.png) |

## 功能特色

- **OpenAI 相容 API** — `/v1/chat/completions`、`/v1/embeddings`、`/v1/rerank`、`/v1/score`、`/v1/responses`、`/v1/models`（僅列出 LLM/VLM）
- **多模型路由** — LLM、VLM、Embedding、Vision Embedding、Reranker、Vision Reranker
- **SSE 串流** — 完整支援 Server-Sent Events（chat completions 和 responses）
- **智慧容錯** — 可設定各類型的備援模型，依健康檢查自動切換；回應標頭 `X-Model-Fallback`
- **分級計價** — 各類型獨立的 input/output token 價格，自動計算費用
- **用量追蹤** — 逐請求記錄每位使用者的 token 數與費用至 PostgreSQL
- **OAuth2 SSO** — 整合 [AuthCenter](https://github.com/bwinken/authcenter)，RS256 JWT 驗證，自動建立使用者
- **雙重驗證** — SDK/API 使用 Bearer API key，Web UI 使用 oauth2-proxy + JWT
- **Web 儀表板** — 用量統計、Chart.js 趨勢圖、分組的伺服器健康狀態
- **管理面板** — 使用者管理、排行榜、每日額度控制、模型設定 UI（路由/計價/容錯）
- **背景健康檢查** — 定期 ping 所有下游伺服器

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

編輯 `config.toml` — 設定下游 vLLM 伺服器 URL 和 API key：

```toml
[models.llm."my-model"]
real_model = "Qwen/Qwen2.5-72B"
base_url = "http://your-llm-server:8000/v1"
api_key = "token-abc123"
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

各類型備援模型（選填）。當模型的伺服器離線時，優先使用此模型作為備援：

```toml
[fallback]
llm = "backup-llm"
vlm = "backup-vlm"
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

> OAuth2 登入設定（OIDC issuer、client secret、redirect URL）在 `deploy/.env` 中設定，供 oauth2-proxy 使用。詳見 [deploy/README.md](deploy/README.md)。

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

### 列出模型

```bash
curl http://your-gateway/v1/models \
  -H "Authorization: Bearer sk-your-api-key"
```

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
│   │   ├── llm_api.py          # /v1/* API 端點
│   │   ├── web_ui.py           # 儀表板 (Jinja2)
│   │   └── admin.py            # 管理面板 + API
│   ├── services/
│   │   ├── proxy.py            # 核心代理 + 容錯 + 記錄
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
│   ├── llm-gateway.nginx.conf  # Nginx 設定 (auth_request)
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
