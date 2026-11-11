#!/bin/bash
# 步驟 3（演練機器）：準備 gateway 映像
#
# 用法:
#   bash deploy/migration/3-image.sh                 # 用這份 checkout 建映像
#   IMAGE_TAR=llm-gateway.tar.gz bash deploy/migration/3-image.sh   # 不能連外：載入別處建好的映像
#
# 可用環境變數（也可寫在 drill.env）：
#   GATEWAY_IMAGE   映像名稱（預設 llm-gateway:drill）。正式切換要用的就是這個映像，
#                   所以演練通過後，用同一個 tag（或 docker save 出來的同一個檔）去正式主機。
#   IMAGE_TAR       docker save 產生的檔案；設了就載入，不建置
#   PYTHON_IMAGE / UV_IMAGE        基底映像（公司內部 mirror）
#   HTTPS_PROXY / HTTP_PROXY       建置時經過的 proxy
#   DRILL_BUILD_NETWORK            docker build --network 的值（例如 host）
. "$(dirname "$0")/lib.sh"
step_begin 3-image
need docker

start=$(date +%s)
if [ -n "${IMAGE_TAR:-}" ]; then
    [ -f "$IMAGE_TAR" ] || die "找不到 IMAGE_TAR: $IMAGE_TAR"
    info "載入映像 $IMAGE_TAR"
    docker load -i "$IMAGE_TAR" || die "docker load 失敗"
    docker image inspect "$GATEWAY_IMAGE" >/dev/null 2>&1 \
        || die "載入的檔案裡沒有 $GATEWAY_IMAGE（把 GATEWAY_IMAGE 設成檔案裡的映像名稱）"
    note "source=load"
else
    info "建置映像 $GATEWAY_IMAGE（$REPO_ROOT）"
    args=()
    for v in PYTHON_IMAGE UV_IMAGE HTTPS_PROXY HTTP_PROXY NO_PROXY; do
        [ -n "${!v:-}" ] && args+=(--build-arg "$v=${!v}")
    done
    [ -n "${DRILL_BUILD_NETWORK:-}" ] && args+=(--network "$DRILL_BUILD_NETWORK")
    docker build "${args[@]}" -f "$DRILL_HOME/Dockerfile" -t "$GATEWAY_IMAGE" "$REPO_ROOT" || die "docker build 失敗"
    note "source=build"
fi

# 映像能不能用：以非 root 身分載入程式、列出 migration 的最新版本
head_rev="$(docker run --rm --network none --user "$(id -u):$(id -g)" "$GATEWAY_IMAGE" alembic heads 2>/dev/null | awk 'NR==1{print $1}')" \
    || die "映像無法執行 alembic"
[ -n "$head_rev" ] || die "映像裡找不到 migration"
docker run --rm --network none --user "$(id -u):$(id -g)" "$GATEWAY_IMAGE" python -c "import app.main" >/dev/null 2>&1 \
    || die "映像裡的程式無法載入（python -c 'import app.main' 失敗）"

note "image=$GATEWAY_IMAGE id=$(docker image inspect -f '{{.Id}}' "$GATEWAY_IMAGE" | cut -c8-19) head=$head_rev seconds=$(( $(date +%s) - start ))"
ok "映像就緒：$GATEWAY_IMAGE（migration 最新版本 $head_rev）"
ok "下一步：bash deploy/migration/4-up.sh"
