# Migration Guide: SQLite → PostgreSQL

本文件說明如何從 SQLite 遷移到 PostgreSQL，適用於已經在用 SQLite 運行的既有部署。

---

## 總覽

遷移分為三個階段：

```
階段 1: 準備環境（PostgreSQL + .env）
階段 2: 資料遷移（SQLite → PostgreSQL）
階段 3: Schema 升級（Alembic 接管）
```

> **預估停機時間**：階段 2 的最終 `--sync` 到階段 3 切換完成之間，約 1-5 分鐘（取決於資料量）。

---

## 階段 1: 準備 PostgreSQL 環境

### 1.1 啟動 PostgreSQL

開發環境可用內建腳本：

```bash
bash scripts/start-pg-dev.sh start
```

正式環境建議用 Docker Compose 或獨立安裝：

```bash
docker compose up -d postgres
```

### 1.2 設定 .env

```bash
cp .env.example .env
```

編輯 `.env`，確認 `DATABASE_URL` 指向 PostgreSQL：

```env
DATABASE_URL=postgresql://llm_gateway:your_password@localhost:5432/llm_gateway
```

### 1.3 驗證連線

```bash
python -c "
from dotenv import load_dotenv; load_dotenv()
from app.core.config import DATABASE_URL
from sqlmodel import create_engine
engine = create_engine(DATABASE_URL)
with engine.connect() as conn:
    print('OK: Connected to', DATABASE_URL.split('@')[-1])
"
```

---

## 階段 2: 資料遷移

### 2.1 預覽（Dry Run）

不會寫入任何資料，只顯示會執行的操作：

```bash
python scripts/migrate_sqlite_to_pg.py /path/to/llm_gateway.db --dry-run
```

**檢查重點：**
- users 數量是否正確
- usage_logs 數量是否合理
- 有沒有 `SKIP` 或 `ERROR` 訊息

### 2.2 首次完整遷移

```bash
python scripts/migrate_sqlite_to_pg.py /path/to/llm_gateway.db
```

此步驟會：
- 在 PostgreSQL 建立 `users` 和 `usage_logs` 表
- 搬移所有 users（保留 username, api_key, daily_limit_usd 等）
- 搬移所有 usage_logs（自動對應新的 user_id）

> **注意**：SQLite 的原始檔案不會被修改或刪除。

### 2.3 增量同步（正式切換前）

如果首次遷移後 SQLite 仍繼續服務了一段時間，在正式切換前執行：

```bash
python scripts/migrate_sqlite_to_pg.py /path/to/llm_gateway.db --sync
```

此步驟會：
- 只遷移 PostgreSQL 中 `usage_logs.created_at` 最新時間之後的記錄
- 更新有變動的 user 欄位（api_key, daily_limit_usd, is_admin）
- 不會重複插入已存在的資料

> **建議**：在流量低峰時執行，並在 sync 完成後立即切換到 PostgreSQL。

---

## 階段 3: Schema 升級（Alembic）

### 3.1 標記現有 Schema

**重要**：如果資料庫是透過 `migrate_sqlite_to_pg.py` 或 `create_all()` 建立的（不是透過 Alembic），必須先標記當前版本，否則 `alembic upgrade head` 會嘗試重新建表並失敗。

```bash
alembic stamp head
```

這會在資料庫建立 `alembic_version` 表，並標記為最新版本。

### 3.2 套用後續的 Schema 變更

之後的 schema 變更（例如 `cost_usd` 從 `float` 改為 `Numeric(12,6)`）：

```bash
# 查看待執行的 migration
alembic history --verbose

# 套用所有待執行的 migration
alembic upgrade head

# 查看當前版本
alembic current
```

### 3.3 回滾（如有問題）

```bash
# 回到上一個版本
alembic downgrade -1

# 回到特定版本
alembic downgrade <revision_id>
```

---

## 注意事項

### ID 不保留

遷移時 user ID 由 PostgreSQL 自動產生（auto-increment），不會與 SQLite 中的 ID 相同。腳本會自動建立 old_id → new_id 的對應關係來正確遷移 `usage_logs.user_id`。

**影響**：如果有外部系統硬編碼了 user ID，遷移後需要更新。

### API Key 不變

User 的 `api_key` 會原樣搬移，客戶端不需要更換 key。

### Timestamps 保留

`created_at` 會原樣搬移，歷史資料的時間戳不會改變。

### owner_id 欄位

`migrate_sqlite_to_pg.py` 是在 `owner_id` 功能之前寫的，不會遷移 `owner_id`。如果 SQLite 中有 `owner_id` 資料，需要手動補：

```sql
-- 遷移後手動設定 app 帳號的 owner
UPDATE users SET owner_id = (SELECT id FROM users WHERE username = 'owner_name')
WHERE username LIKE 'app_%';
```

### 不要同時運行兩個資料庫

遷移完成後，確保 `.env` 中的 `DATABASE_URL` 指向 PostgreSQL，並停止使用 SQLite。同時運行兩個資料庫會導致資料不一致。

### 大量資料的遷移

如果 `usage_logs` 超過百萬筆，遷移可能需要幾分鐘。腳本沒有進度條，可以觀察 PostgreSQL 的連線狀態：

```bash
# 查看 PostgreSQL 活躍連線
psql -U llm_gateway -c "SELECT count(*) FROM usage_logs;"
```

---

## 完整流程（Checklist）

```
[ ] 1. PostgreSQL 已啟動且可連線
[ ] 2. .env 中 DATABASE_URL 已設定為 PostgreSQL
[ ] 3. --dry-run 預覽結果正確
[ ] 4. 首次完整遷移完成，檢查 users 和 usage_logs 數量
[ ] 5. （如有需要）--sync 增量同步完成
[ ] 6. 停止舊的 SQLite 服務
[ ] 7. alembic stamp head 標記 schema 版本
[ ] 8. alembic upgrade head 套用最新 schema 變更
[ ] 9. 啟動新服務，確認 dashboard 和 API 正常
[ ] 10. 保留 SQLite 檔案作為備份（至少一週）
```
