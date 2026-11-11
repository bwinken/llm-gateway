#!/bin/bash
# 步驟 6（演練機器）：驗證容器化的環境，和正式環境的快照對照
#
# 用法:
#   bash deploy/migration-drill/6-verify.sh
#   DRILL_TEST_MODEL=<模型別名> bash deploy/migration-drill/6-verify.sh   # 多打一個真的請求
#
# 每一項印出 ✔ / ✘，全部通過才回傳 0。不會改動 config.toml，寫進資料庫的只有
# 一個臨時帳號 migration_drill（以及它那一筆用量）。可以重複執行。
#
# DRILL_TEST_MODEL 沒設時不會對任何模型發請求（演練機器不一定連得到 vLLM；
# 雲端模型會產生費用）。設了就用臨時帳號送一個 max_tokens=8 的 chat 請求，
# 並確認它記進 usage_logs。只支援本機（vLLM）模型的別名。
. "$(dirname "$0")/lib.sh"
step_begin 6-verify
need docker curl sha256sum
guard_not_production
[ -f "$MARKER" ] || die "這裡沒有演練環境：先跑 4-up.sh"
snap="$STATE_DIR/snapshot.env"; restored="$STATE_DIR/restored.env"
[ -f "$snap" ] || die "找不到 $STATE_DIR/snapshot.env：4-up.sh 沒有跑完？"
[ -f "$restored" ] || die "資料還沒搬進來：先跑 5-load-data.sh"

PORT="$(kv "$DEPLOY_DIR/.env" GATEWAY_PORT)"; BASE="http://127.0.0.1:$PORT"
PASS=0; FAIL=0; FAILED=()
pass() { PASS=$((PASS + 1)); echo -e "  ${GREEN}✔${NC} $*"; }
fail() { FAIL=$((FAIL + 1)); FAILED+=("$1"); echo -e "  ${RED}✘${NC} $*"; }
check() { local name="$1"; shift; if "$@"; then pass "$name"; else fail "$name"; fi; }
section() { echo -e "${BOLD}$*${NC}"; }
http_code() { curl -s -m 15 -o /dev/null -w '%{http_code}' "$@" || true; }
in_gateway() { docker exec "$GW_CONTAINER" "$@"; }

# ── 容器 ──
section "容器"
[ "$(docker inspect -f '{{.State.Status}}' "$GW_CONTAINER" 2>/dev/null)" = running ] \
    && pass "gateway 容器執行中" || fail "gateway 容器執行中"
[ "$(http_code "$BASE/readyz")" = 200 ] && pass "/readyz 回 200（資料庫連得上）" || fail "/readyz 回 200"
[ "$(http_code "$BASE/healthz")" = 200 ] && pass "/healthz 回 200" || fail "/healthz 回 200"
[ "$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$GW_CONTAINER" 2>/dev/null)" = host ] \
    && pass "gateway 使用主機網路（network_mode: host）" || fail "gateway 使用主機網路"
uid="$(docker inspect -f '{{.Config.User}}' "$GW_CONTAINER" 2>/dev/null || true)"
[ "${uid%%:*}" != 0 ] && [ -n "$uid" ] && pass "容器以非 root 執行（$uid）" || fail "容器以非 root 執行（$uid）"
ext_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ -n "$ext_ip" ] && [ "$ext_ip" != 127.0.0.1 ]; then
    [ "$(http_code -m 3 "http://$ext_ip:$PORT/readyz")" = 000 ] \
        && pass "只聽 127.0.0.1（從 $ext_ip 連不到）" || fail "只聽 127.0.0.1（從 $ext_ip 竟然連得到）"
fi

if [ "$(docker inspect -f '{{.State.Status}}' "$GW_CONTAINER" 2>/dev/null)" != running ]; then
    note "passed=$PASS failed=$FAIL"
    die "gateway 容器沒有在執行，後面的檢查無法進行（docker logs $GW_CONTAINER 看原因；重來：7-teardown.sh → 4-up.sh）"
fi

# ── 資料 ──
section "資料（對照正式環境的快照）"
head_rev="$(in_gateway alembic heads 2>/dev/null | awk 'NR==1{print $1}' || true)"
db_rev="$(drill_psql "SELECT string_agg(version_num, ',') FROM alembic_version" 2>/dev/null || true)"
[ -n "$head_rev" ] && [ "$db_rev" = "$head_rev" ] \
    && pass "schema 是映像的最新版本（$db_rev；正式環境備份時是 $(kv "$snap" schema_version)）" \
    || fail "schema 是映像的最新版本（資料庫 $db_rev，映像 ${head_rev:-?}）"
for t in $SNAPSHOT_TABLES; do
    want="$(kv "$snap" "count_$t")"; got="$(kv "$restored" "count_$t")"
    [ -n "$want" ] || continue
    # 快照在備份開始前一刻拍下，正式環境同時還在收請求：還原的只會多，不會少
    if [ -n "$got" ] && [ "$got" -ge "$want" ]; then
        extra=""; [ "$got" -gt "$want" ] && extra="，備份期間多了 $((got - want))"
        pass "$t：還原 $got 筆（快照 $want$extra）"
    else
        fail "$t：還原 ${got:-0} 筆，比快照的 $want 少"
    fi
done
leases="$(drill_psql "SELECT count(*) FROM concurrency_leases WHERE worker_id NOT LIKE '$(hostname):%'" 2>/dev/null || echo 0)"
[ "$leases" = 0 ] && pass "沒有舊主機留下的並發 lease" || fail "沒有舊主機留下的並發 lease（還有 $leases 筆）"
trg="$(drill_psql "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'audit_log'::regclass AND NOT tgisinternal" 2>/dev/null || echo 0)"
[ "$trg" -ge 2 ] && pass "audit_log 的防竄改觸發器還在（$trg 個）" || fail "audit_log 的防竄改觸發器還在（$trg 個）"

# ── 設定 ──
section "設定"
cfg_sum="$(sha256sum "$REPO_ROOT/config/config.toml" | cut -d' ' -f1)"
[ "$cfg_sum" = "$(kv "$snap" config_sha256)" ] \
    && pass "config.toml 和正式環境的完全相同" || fail "config.toml 和正式環境的完全相同（雜湊不符）"
models="$(in_gateway python -c "
from app.core import config as c
assert str(c._active_config_path()) == '/app/config/config.toml', c._active_config_path()
print(len(c.MODEL_ROUTING), len(c.AZURE_MODELS), len(c.BEDROCK_MODELS))" 2>/dev/null || true)"
if [ -n "$models" ]; then
    set -- $models
    pass "gateway 讀的是掛載進來的 config.toml（vLLM $1 個、Azure $2 個、Bedrock $3 個模型）"
else
    fail "gateway 讀的是掛載進來的 config.toml（它退回了內建的範例設定？）"
fi
in_gateway python -c "
import os, tempfile
d = '/app/config'
fd, tmp = tempfile.mkstemp(dir=d, suffix='.toml.tmp'); os.close(fd)
os.replace(tmp, os.path.join(d, '.drill-write-probe')); os.unlink(os.path.join(d, '.drill-write-probe'))" 2>/dev/null \
    && pass "config 目錄可以寫入並 rename（admin 頁面存檔用的方式）" || fail "config 目錄可以寫入並 rename"
ls "$REPO_ROOT"/logs/gateway_*.log >/dev/null 2>&1 \
    && pass "log 寫到主機上的 logs/" || fail "log 寫到主機上的 logs/"
if [ -f "$REPO_ROOT/keys/public.pem" ]; then
    in_gateway test -r /app/keys/public.pem && pass "AuthCenter 公鑰讀得到" || fail "AuthCenter 公鑰讀得到"
fi

# ── 不對外產生副作用 ──
section "演練不會影響到外面"
leaked="$({ in_gateway env | grep -E '^(EXCHANGE_|LANGFUSE_|USAGE_LOG_RETENTION_DAYS=)' || true; } | cut -d= -f1 | tr '\n' ' ')"
[ -z "$leaked" ] && pass "容器裡沒有寄信、Langfuse、保留期的設定" || fail "容器裡還有這些設定：$leaked"
in_gateway python -c "
from app.services import mailer
raise SystemExit(1 if mailer.is_enabled() else 0)" >/dev/null 2>&1 \
    && pass "寄信功能是關閉的（不會寄信給使用者）" || fail "寄信功能是關閉的"

# ── API ──
section "API"
key="$(drill_psql "SELECT api_key FROM users WHERE username = 'migration_drill'")"
if [ -z "$key" ]; then
    key="sk-drill-$(od -An -tx1 -N24 /dev/urandom | tr -d ' \n')"
    drill_psql "INSERT INTO users (username, password_hash, api_key, daily_limit_usd, is_admin, is_disabled, can_use_azure, can_use_bedrock, created_at)
                VALUES ('migration_drill', '', '$key', 1, false, false, false, false, now())" >/dev/null
fi
[ "$(http_code -H "Authorization: Bearer $key" "$BASE/v1/me")" = 200 ] \
    && pass "API key 驗證通過（/v1/me）" || fail "API key 驗證通過（/v1/me）"
n_models="$(curl -s -m 15 -H "Authorization: Bearer $key" "$BASE/v1/models" | grep -o '"id"' | wc -l || true)"
[ "$n_models" -ge 1 ] && pass "/v1/models 列出 $n_models 個模型" || fail "/v1/models 列出模型（0 個）"
probe="203.0.113.$(( (RANDOM % 200) + 20 ))"
# 一把不存在的 key：用文件裡的佔位字樣（.gitleaks.toml 認得它，不會被當成外洩的密鑰）
wrong_key="sk-your-api-key"
[ "$(http_code -H "Authorization: Bearer $wrong_key" -H "X-Forwarded-For: $probe" "$BASE/v1/me")" = 401 ] \
    && pass "錯誤的 key 被拒絕（401）" || fail "錯誤的 key 被拒絕（401）"
sleep 1
docker logs --since 30s "$GW_CONTAINER" 2>&1 | grep -q "client=$probe" \
    && pass "gateway 看到的是 X-Forwarded-For 的使用者位址（$probe），不是 proxy 的位址" \
    || fail "gateway 看到的是 X-Forwarded-For 的使用者位址（IP allowlist、鎖定、audit log 都靠它）"
[ "$(http_code "$BASE/dashboard")" = 401 ] \
    && pass "Web UI 在沒有登入時回 401（由 nginx + oauth2-proxy 負責登入）" || fail "Web UI 在沒有登入時回 401"

if [ -n "${DRILL_TEST_MODEL:-}" ]; then
    before="$(drill_psql "SELECT count(*) FROM usage_logs l JOIN users u ON u.id = l.user_id WHERE u.username = 'migration_drill'")"
    code="$(http_code -H "Authorization: Bearer $key" -H 'Content-Type: application/json' \
        -d "{\"model\": \"$DRILL_TEST_MODEL\", \"max_tokens\": 8, \"messages\": [{\"role\": \"user\", \"content\": \"ping\"}]}" \
        -m 120 "$BASE/v1/chat/completions")"
    [ "$code" = 200 ] && pass "模型 $DRILL_TEST_MODEL 回應 200" || fail "模型 $DRILL_TEST_MODEL 回應 200（實際 $code）"
    after="$before"
    for _ in 1 2 3 4 5; do
        after="$(drill_psql "SELECT count(*) FROM usage_logs l JOIN users u ON u.id = l.user_id WHERE u.username = 'migration_drill'")"
        [ "$after" -gt "$before" ] && break; sleep 1
    done
    [ "$after" -gt "$before" ] && pass "這個請求記進了 usage_logs（計費有在運作）" || fail "這個請求記進了 usage_logs"
else
    info "沒有設 DRILL_TEST_MODEL：略過實際的模型請求"
fi

# ── 時間 ──
section "時間（正式切換的停機時間估計）"
b="$(kv "$snap" backup_seconds)"; r="$(kv "$restored" restore_seconds)"
m="$(kv "$restored" migrate_seconds)"; s="$(kv "$restored" start_seconds)"
l="$(kv "$restored" load_seconds)"          # 步驟 5 從停掉 gateway 到重新就緒的實測秒數
move=$(( ${b:-0} + ${l:-0} )); inplace=$(( ${m:-0} + ${s:-0} ))
echo "  備份 ${b:-?}s ／ 載入資料（停止 → 就緒）${l:-?}s，其中還原 ${r:-?}s、migration ${m:-?}s、啟動 ${s:-?}s"
echo "  搬到新主機（容器先開好，資料最後搬）：備份 + 載入 ≈ ${move}s"
echo "      另加：把備份檔複製到新主機、切換流量（DNS / 負載平衡）的時間"
echo "  在原主機直接切換（不搬資料）：migration + 啟動 ≈ ${inplace}s，另加停掉 systemd 服務的時間"

note "passed=$PASS failed=$FAIL downtime_move=${move}s downtime_inplace=${inplace}s test_model=${DRILL_TEST_MODEL:--}"
echo
if [ "$FAIL" -gt 0 ]; then
    note "failed_checks=$(IFS='|'; echo "${FAILED[*]}" | tr ' \t' '__' | cut -c1-300)"
    die "驗證沒有通過：$FAIL 項失敗，$PASS 項通過"
fi
ok "驗證通過：$PASS 項全部通過。演練完記得跑：bash deploy/migration-drill/7-teardown.sh"
