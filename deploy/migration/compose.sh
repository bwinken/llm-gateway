#!/bin/bash
# docker compose，帶上原本的 deploy/docker-compose.yml 和這裡的 gateway 容器定義
#
# 用法（和 docker compose 一樣，只是不用自己寫兩個 -f）:
#   bash deploy/migration/compose.sh up -d
#   bash deploy/migration/compose.sh ps
#   bash deploy/migration/compose.sh logs -f gateway
#   bash deploy/migration/compose.sh down
#
# 以容器執行 gateway 的主機（新主機、演練機器）一律用這支：只用
#   docker compose -f deploy/docker-compose.yml down
# 的話，PostgreSQL 會停，gateway 容器卻還在跑（沒有資料庫可用）。
# 還在用 systemd 跑 gateway 的主機不要用這支；那裡照舊用原本的指令。
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
exec docker compose -f "$DIR/../docker-compose.yml" -f "$DIR/docker-compose.gateway.yml" "$@"
