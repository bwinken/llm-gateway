#!/bin/bash
# 啟動開發/測試用的 PostgreSQL Docker 容器
# 用法: bash scripts/start-pg-dev.sh [start|stop|status]
#
# 預設帳號密碼與 .env.example 一致：
#   DATABASE_URL=postgresql://llm_gateway:your_password@localhost:5432/llm_gateway
set -e

CONTAINER_NAME="llm-gateway-pg-dev"
PG_USER="llm_gateway"
PG_PASSWORD="your_password"
PG_DB="llm_gateway"
PG_PORT="5432"
PG_VERSION="16"

ACTION="${1:-start}"

case "$ACTION" in
  start)
    # 檢查是否已在運行
    if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
      echo "PostgreSQL 已在運行 (${CONTAINER_NAME})"
      echo "  連線: postgresql://${PG_USER}:${PG_PASSWORD}@localhost:${PG_PORT}/${PG_DB}"
      exit 0
    fi

    # 檢查是否有已停止的容器
    if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
      echo "啟動已存在的容器..."
      docker start "$CONTAINER_NAME"
    else
      echo "建立新的 PostgreSQL ${PG_VERSION} 容器..."
      docker run -d \
        --name "$CONTAINER_NAME" \
        -e POSTGRES_USER="$PG_USER" \
        -e POSTGRES_PASSWORD="$PG_PASSWORD" \
        -e POSTGRES_DB="$PG_DB" \
        -p "${PG_PORT}:5432" \
        "postgres:${PG_VERSION}"
    fi

    # 等待 PostgreSQL 就緒
    echo -n "等待 PostgreSQL 就緒"
    for i in $(seq 1 30); do
      if docker exec "$CONTAINER_NAME" pg_isready -U "$PG_USER" -d "$PG_DB" &>/dev/null; then
        echo " OK"
        echo ""
        echo "PostgreSQL 已就緒！"
        echo "  連線: postgresql://${PG_USER}:${PG_PASSWORD}@localhost:${PG_PORT}/${PG_DB}"
        echo ""
        echo "確認 .env 中的 DATABASE_URL 設定正確："
        echo "  DATABASE_URL=postgresql://${PG_USER}:${PG_PASSWORD}@localhost:${PG_PORT}/${PG_DB}"
        echo ""
        echo "接下來可以："
        echo "  # 預覽遷移"
        echo "  python scripts/migrate_sqlite_to_pg.py /path/to/llm_gateway.db --dry-run"
        echo ""
        echo "  # 執行遷移"
        echo "  python scripts/migrate_sqlite_to_pg.py /path/to/llm_gateway.db"
        echo ""
        echo "  # 啟動 Gateway"
        echo "  fastapi dev app/main.py"
        exit 0
      fi
      echo -n "."
      sleep 1
    done
    echo " TIMEOUT"
    echo "ERROR: PostgreSQL 未能在 30 秒內就緒" >&2
    exit 1
    ;;

  stop)
    if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
      echo "停止 ${CONTAINER_NAME}..."
      docker stop "$CONTAINER_NAME"
      echo "已停止（資料保留在容器中，下次 start 可恢復）"
    else
      echo "容器未在運行"
    fi
    ;;

  rm|remove)
    echo "停止並刪除 ${CONTAINER_NAME}（資料將遺失）..."
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
    echo "已刪除"
    ;;

  status)
    if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
      echo "運行中"
      docker exec "$CONTAINER_NAME" pg_isready -U "$PG_USER" -d "$PG_DB"
    elif docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
      echo "已停止（執行 bash scripts/start-pg-dev.sh start 可恢復）"
    else
      echo "不存在"
    fi
    ;;

  *)
    echo "用法: bash scripts/start-pg-dev.sh [start|stop|rm|status]"
    exit 1
    ;;
esac
