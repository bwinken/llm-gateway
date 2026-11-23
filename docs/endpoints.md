# Endpoint 盤點

Gateway 目前註冊的每一條路由：誰在呼叫、用什麼驗證、要不要留。日期：2026-11-23，共 **142** 條（另有 `/static` 靜態檔掛載，不算在內）。

這份清單和程式是對齊的：`tests/test_endpoint_inventory.py` 會比對這裡每一列和實際註冊的路由，新增或刪除路由而沒改這份文件，測試會失敗。

各群組的行為細節在 [architecture.md](architecture.md)；給 client 作者看的格式與範例在 [client-guide.zh-TW.md](client-guide.zh-TW.md)。

## 結論

| 類別 | 條數 | 處理 |
|---|---|---|
| 已刪除 | 10 | `GET /redoc`、`GET /docs/oauth2-redirect`、`GET /v1/model-changes`、`GET /dashboard/client-notices`、`GET /dashboard/model-changes`，以及併入帳號端點的 `GET /dashboard/api/me`、`status`、`usage`、`service-accounts`、`notices` |
| 新增 | 3 | `GET /v1/version`，以及 dashboard 上看完整 key 的兩條（`/dashboard/api/api-key`、`/dashboard/api/service-accounts/{id}/api-key`） |
| 保留，但值得之後再看（見「刪除候選」） | 25 | 頁面沒在用、可能有外部呼叫者：先保留，看 access log 再決定 |
| 保留 | 其餘 | 有頁面、文件或 client 在用 |

**判斷「不必要」的標準**：頁面沒有呼叫，**而且**外部也不可能有人呼叫。只符合前一半的不刪：使用者或腳本可能直接打 API。照這個標準，確定可以刪的是兩條重複的文件頁，和兩條只回傳 HTML 片段、只有現有頁面在用的路由。`GET /v1/model-changes` 是另一種情況：內容已經在 `GET /v1/notices` 裡，由 owner 決定拿掉。

### 已刪除

| 路由 | 原因 |
|---|---|
| `GET /redoc` | 和 `/docs` 是同一份 OpenAPI 的第二種呈現，沒有文件或頁面連到它 |
| `GET /docs/oauth2-redirect` | Swagger UI 的 OAuth2 登入流程用的轉址頁；gateway 的 `/docs` 沒有這個流程 |
| `GET /v1/model-changes` | 內容已經在 `GET /v1/notices` 的 `model_changes` 區塊裡，問一個地方就好。**還在讀這一條的工具會收到 404，要改讀 `/v1/notices`** |
| `GET /dashboard/client-notices` | 只回傳通知面板的 HTML 片段，只有 dashboard 頁面在按下 *I've applied it* 後用它重畫。頁面改成整頁重新載入 |
| `GET /dashboard/model-changes` | 同上，模型汰換面板的 HTML 片段（按下 *I've switched* 之後）。`/dashboard/model-changes/patch.{bat,sh}` 與 `.../applied` 不受影響 |
| `GET /dashboard/api/me`、`status`、`usage`、`service-accounts`、`notices` | 和同名的帳號端點（`GET /v1/me` ...）回一樣的內容，只是登入方式不同。帳號端點現在兩種登入都收（API key 或 SSO），所以只留 `/v1` 那一條；登入者原本多看到的部分也搬過去：遮罩的 key、`site`、service account 的 `id` 等兩種登入都給，只有 admin 的 `is_admin` 和 `sent_for` 只給 SSO 登入。刪的時候還沒有任何頁面或前端在用這五條 |

### 刪除候選（這次沒有刪）

這三組在 repo 裡找不到呼叫者，但 repo 看不到正式環境的流量。要刪之前先確認一段時間（建議 30 天）內沒有請求，有兩個地方可以查。

**Langfuse（前兩組）**：generation 的 `name` 是 client 實際打的路徑，沒有改寫過（`/chat/completions` 不會變成 `/v1/chat/completions`）。在 Langfuse 用 `name` 分組，就看得到每一條路徑的請求數、token 和費用，也看得到是誰（`userId`）、用什麼工具（`version`）在打。限制：只有送到後端的請求才會記錄（`/v1/models`、帳號端點、`/admin` 不會）；`LANGFUSE_SAMPLE_RATE` 小於 1 時是抽樣，沒出現不代表沒人用；這項改動上線前的紀錄，`name` 還是改寫過的路由。

**nginx access log（三組都能查，而且是完整的）**：

```bash
# 根目錄、沒有 /v1 前綴的別名
grep -cE '"(GET|POST) /(models|chat/completions|responses|messages|embeddings|tokenize|score|rerank|systemone)[ /?]' /var/log/nginx/access.log
# /azure、/aws 底下沒有 /v1 的別名
grep -cE '"(GET|POST) /(azure|aws)/(models|chat/completions|responses|messages)[ /?]' /var/log/nginx/access.log
# admin 的 JSON API
grep -cE '"(GET|POST|PATCH) /admin/(users(/[0-9]+)?|api/anomalies|api/audit-log)[ ?]' /var/log/nginx/access.log
```

| 組別 | 條數 | 現況 | 刪掉的影響 |
|---|---|---|---|
| 根目錄無 `/v1` 前綴的別名 | 11 | 範例 nginx（`deploy/llm-gateway-example.nginx.conf`）只把 `/v1/`、`/azure/`、`/aws/`、`/auth/`、`= /me` 直接轉給 gateway；這 11 條會落到走 SSO 的 `location /`，帶 API key 的 client 會被導去登入頁。也就是說，照範例部署的話它們本來就打不到。`/me` 有自己的 location，不在此列 | 正式環境的 nginx 若和範例一樣：沒有影響。若另外放行了這些路徑：base URL 沒寫 `/v1` 的 client 會變 404 |
| `/azure`、`/aws` 無 `/v1` 的別名 | 9 | nginx 打得到。只有 base URL 設成 `https://host/azure`（沒有 `/v1`）的 OpenAI 相容 client 會用到；Anthropic SDK 和 Claude Code 一律自己加 `/v1` | 那些 client 變 404；`docs/client-guide` 與 README 裡「不加 `/v1` 也可以」的說明要一起改 |
| admin 的 JSON API | 5 | admin 頁面不呼叫它們（頁面用表單路由和 `bulk-update`）。要用得帶 admin scope 的 SSO token。測試大量使用（約 45 處） | 用 SSO token 打這幾條的管理腳本會壞。另外：之後 admin 也要前後端分離的話，JSON API 才是要留的那一邊，被取代的反而是表單路由 |

## 給前端 / 工具的 API（之後前後端分離的對象）

### 版本與健康檢查（不需驗證）

`GET /v1/version` 是新的：回傳這台 gateway 支援的 API 版本與各自的狀態（`stable` / `deprecated` / `nightly`）。清單在 `app/core/api_versions.py`。其餘三條給 nginx、load balancer、Prometheus 用。

| 端點 | Handler |
|---|---|
| `GET /healthz` | `health_api.healthz` |
| `GET /readyz` | `health_api.readyz` |
| `GET /metrics` | `health_api.prometheus_metrics` |
| `GET /v1/version` | `health_api.api_version` |

### Dashboard 上只給登入者的 API（SSO）

Dashboard 的資料（`me`、`status`、`usage`、`service-accounts`、`notices`）不在這裡：它們就是下面的帳號端點，瀏覽器用 SSO 登入去讀同一條 `/v1` 路由。

留在 `/dashboard/api` 的是 API key 不能做的事：兩條 `api-key` 各回傳一把完整的 key，會記在稽核紀錄。它們留在 `/dashboard` 前綴下，沒登入的請求在 proxy 那一層就被擋掉，不只靠 gateway 的程式。`model-breakdown` 是原本就有的，給現有頁面的 Cost by Model 卡片用。

| 端點 | Handler |
|---|---|
| `GET /dashboard/api/api-key` | `dashboard.api.own_api_key` |
| `GET /dashboard/api/service-accounts/{app_id}/api-key` | `dashboard.api.service_account_api_key` |
| `GET /dashboard/api/model-breakdown` | `dashboard.api.model_breakdown_api` |

### Dashboard 的動作與下載（SSO）

原本就有，從 `web_ui.py` 搬到 `app/routers/dashboard/actions.py`。帶 `Accept: application/json` 就回 JSON，所以現有頁面和之後的獨立前端共用。

兩條 `resolved` 是現有頁面的表單在用的。獨立前端改打 `POST /v1/notices/resolved`（同一個動作，SSO 登入也收），所以這兩條會跟著頁面一起退場。現在不能先刪：瀏覽器的登入 cookie 只有經過 `location /` 才會換成 token，頁面的表單打不到 `/v1/`。

| 端點 | Handler |
|---|---|
| `POST /dashboard/refresh-key` | `dashboard.actions.refresh_own_key` |
| `POST /dashboard/app/{app_id}/refresh-key` | `dashboard.actions.refresh_owned_app_key` |
| `GET /dashboard/model-changes/patch.{fmt}` | `dashboard.actions.model_changes_patch` |
| `POST /dashboard/model-changes/{change_id}/applied` | `dashboard.actions.model_change_applied` |
| `GET /dashboard/client-rules/all/patch.{fmt}` | `dashboard.actions.client_rules_patch_all` |
| `POST /dashboard/client-rules/all/resolved` | `dashboard.actions.client_rules_resolved_all` |
| `POST /dashboard/client-rules/{rule_id}/resolved` | `dashboard.actions.client_rule_resolved` |
| `GET /dashboard/api-key/patch.{fmt}` | `dashboard.actions.api_key_patch` |
| `GET /dashboard/install-claude-code.bat` | `dashboard.actions.personalized_claude_installer` |

### 帳號端點（API key 或 SSO）

帳號查詢自己的資訊。兩種 client 打同一條路由：repo 以外的程式（terminal 工具、狀態列、設定 client 的桌面程式）帶帳號的 API key；瀏覽器（dashboard 的獨立前端）帶 SSO 登入，由前面的 proxy 把登入 cookie 換成 token 再轉進來（範例見 `deploy/llm-gateway-example.nginx.conf` 的 `/dashboard/v1/`）。只增不改，不能刪。

兩種登入拿到的回應相同（`/v1/me` 都有 `org_code`、遮罩的 `api_key`、`site`；`/v1/service-accounts` 都有每個帳號的 `id`、`api_key_masked`，以及 `withheld`），只有兩件事只告訴 SSO 登入的人：admin 的 `is_admin` 為 true（key 不帶管理員權限），以及 `/v1/service-accounts` 的 `sent_for` 和 `top_users`（這個帳號替誰送過請求：外洩的個人 key 不應該說出誰在用某個應用程式）。兩種登入都拿不到完整的 key。

路徑雖然也在 `/v1` 底下，但和下面的「模型 API」是兩回事：這裡不呼叫模型、不計費、額度用完照樣回應。程式上也分開，在 `app/routers/account_api.py`；模型 API 在 `app/routers/v1_api.py`。

| 端點 | Handler |
|---|---|
| `GET /me` | `account_api.account_info` |
| `GET /v1/me` | `account_api.account_info` |
| `GET /v1/status` | `account_api.model_status` |
| `GET /v1/usage` | `account_api.usage_history` |
| `GET /v1/service-accounts` | `account_api.owned_service_accounts` |
| `GET /v1/notices` | `account_api.client_notices` |
| `POST /v1/notices/resolved` | `account_api.client_notices_resolved` |
| `POST /v1/events` | `account_api.client_event` |
| `GET /v1/settings` | `account_api.client_settings_fields` |

### 登入（SSO token）

| 端點 | Handler |
|---|---|
| `POST /auth/api-key` | `auth_api.api_key_for_token` |

## 模型 API（API key）

SDK、Claude Code 與其他 client 呼叫的推論 API：會送到後端模型、會計費、受額度與併發限制。全部保留。在 `app/routers/v1_api.py`。

### `/v1`

| 端點 | Handler |
|---|---|
| `GET /v1/models` | `v1_api.list_models` |
| `POST /v1/chat/completions` | `v1_api.chat_completions` |
| `POST /v1/chat/completions/render` | `v1_api.chat_completions_render` |
| `POST /v1/responses` | `v1_api.responses` |
| `POST /v1/messages` | `v1_api.messages` |
| `POST /v1/messages/count_tokens` | `v1_api.messages_count_tokens` |
| `POST /v1/tokenize` | `v1_api.tokenize` |
| `POST /v1/embeddings` | `v1_api.embeddings` |
| `POST /v1/score` | `v1_api.rerank` |
| `POST /v1/rerank` | `v1_api.rerank` |
| `POST /v1/systemone` | `v1_api.systemone` |

### `/azure/v1`

| 端點 | Handler |
|---|---|
| `GET /azure/v1/models` | `azure_api.list_azure_models` |
| `POST /azure/v1/chat/completions` | `azure_api.azure_chat_completions` |
| `POST /azure/v1/responses` | `azure_api.azure_responses` |
| `POST /azure/v1/messages` | `azure_api.azure_messages` |
| `POST /azure/v1/messages/count_tokens` | `azure_api.azure_count_tokens` |

### `/aws/v1`

| 端點 | Handler |
|---|---|
| `GET /aws/v1/models` | `aws_api.list_bedrock_models` |
| `POST /aws/v1/chat/completions` | `aws_api.bedrock_chat_completions` |
| `POST /aws/v1/messages` | `aws_api.bedrock_messages` |
| `POST /aws/v1/messages/count_tokens` | `aws_api.bedrock_count_tokens` |

### 無 `/v1` 前綴的別名：根目錄

和上面 `/v1` 的同名路由是同一個 handler。見「刪除候選」。

| 端點 | Handler |
|---|---|
| `GET /models` | `v1_api.list_models` |
| `POST /chat/completions` | `v1_api.chat_completions` |
| `POST /chat/completions/render` | `v1_api.chat_completions_render` |
| `POST /responses` | `v1_api.responses` |
| `POST /messages` | `v1_api.messages` |
| `POST /messages/count_tokens` | `v1_api.messages_count_tokens` |
| `POST /tokenize` | `v1_api.tokenize` |
| `POST /embeddings` | `v1_api.embeddings` |
| `POST /score` | `v1_api.rerank` |
| `POST /rerank` | `v1_api.rerank` |
| `POST /systemone` | `v1_api.systemone` |

### 無 `/v1` 前綴的別名：`/azure`、`/aws`

| 端點 | Handler |
|---|---|
| `GET /azure/models` | `azure_api.list_azure_models` |
| `POST /azure/chat/completions` | `azure_api.azure_chat_completions` |
| `POST /azure/responses` | `azure_api.azure_responses` |
| `POST /azure/messages` | `azure_api.azure_messages` |
| `POST /azure/messages/count_tokens` | `azure_api.azure_count_tokens` |
| `GET /aws/models` | `aws_api.list_bedrock_models` |
| `POST /aws/chat/completions` | `aws_api.bedrock_chat_completions` |
| `POST /aws/messages` | `aws_api.bedrock_messages` |
| `POST /aws/messages/count_tokens` | `aws_api.bedrock_count_tokens` |

## gateway 自己繪製的頁面（SSO）

前後端分離之後會被獨立前端取代的部分。

### Dashboard 頁面

`GET /dashboard`，整頁由 gateway 繪製。獨立前端上線後可以刪，`/dashboard/api/model-breakdown` 和兩條 `/dashboard/client-rules/.../resolved` 也可以跟著刪（只有這個頁面在用）。

| 端點 | Handler |
|---|---|
| `GET /dashboard` | `dashboard.page.dashboard` |

### 其他頁面

| 端點 | Handler |
|---|---|
| `GET /` | `web_ui.index` |
| `GET /setup` | `web_ui.setup_page` |

### API 文件

| 端點 | Handler |
|---|---|
| `GET /openapi.json` | `fastapi.applications.openapi` |
| `GET /docs` | `fastapi.applications.swagger_ui_html` |

## Admin（SSO，admin scope）

除了下面「頁面不使用的 JSON API」那 5 條，每一條都有 admin 頁面的表單或 script 在呼叫。

### 頁面不使用的 JSON API

見「刪除候選」。

| 端點 | Handler |
|---|---|
| `GET /admin/api/audit-log` | `admin.security.audit_log_api` |
| `GET /admin/users` | `admin.users.list_users_api` |
| `POST /admin/users` | `admin.users.create_user_api` |
| `PATCH /admin/users/{user_id}` | `admin.users.update_user_api` |
| `GET /admin/api/anomalies` | `admin.overview.list_anomalies` |

### Overview

| 端點 | Handler |
|---|---|
| `GET /admin` | `admin.page.admin_page` |
| `POST /admin/api/anomalies/{event_id}/ack` | `admin.overview.ack_anomaly` |
| `POST /admin/api/anomalies/{event_id}/resolve` | `admin.overview.resolve_anomaly` |
| `GET /admin/api/export/usage.xlsx` | `admin.overview.export_usage_report` |
| `GET /admin/api/export/user-backend-costs.csv` | `admin.overview.export_user_backend_costs_csv` |

### Users

| 端點 | Handler |
|---|---|
| `GET /admin/user-table` | `admin.users.user_table_panel` |
| `GET /admin/users/{user_id}/detail` | `admin.users.user_detail_panel` |
| `GET /admin/api/owner-search` | `admin.users.search_owners` |
| `GET /admin/api/account-search` | `admin.users.search_accounts` |
| `POST /admin/users/bulk-update` | `admin.users.bulk_update_users` |
| `POST /admin/users/create` | `admin.users.create_app_account_web` |
| `POST /admin/users/{user_id}/owner` | `admin.users.update_user_owner` |
| `POST /admin/users/{user_id}/limit` | `admin.users.update_user_limit` |
| `POST /admin/users/{user_id}/cloud-limit` | `admin.users.update_user_cloud_limit` |
| `POST /admin/users/{user_id}/toggle-disable` | `admin.users.toggle_disable` |
| `POST /admin/users/{user_id}/toggle-azure` | `admin.users.toggle_azure` |
| `POST /admin/users/{user_id}/toggle-bedrock` | `admin.users.toggle_bedrock` |
| `POST /admin/users/{user_id}/toggle-concurrency-waive` | `admin.users.toggle_concurrency_waive` |
| `POST /admin/users/{user_id}/toggle-overflow` | `admin.users.toggle_overflow` |
| `POST /admin/users/{user_id}/department` | `admin.users.update_user_department` |
| `POST /admin/users/{user_id}/description` | `admin.users.update_user_description` |
| `POST /admin/users/{user_id}/acts-for-users` | `admin.users.set_acts_for_users` |
| `POST /admin/users/{user_id}/delete` | `admin.users.delete_user` |
| `GET /admin/users/{user_id}/api-key` | `admin.users.reveal_user_key` |
| `POST /admin/users/{user_id}/refresh-key` | `admin.users.refresh_user_key` |
| `GET /admin/api/export/users.csv` | `admin.users.export_users_csv` |

### Clients

| 端點 | Handler |
|---|---|
| `POST /admin/client-rules/monthly-mail` | `admin.clients.set_client_rules_monthly_mail` |
| `POST /admin/client-rules/mail-all` | `admin.clients.send_client_rules_mail_all` |
| `GET /admin/client-rules/mail-log` | `admin.clients.client_rules_mail_log` |
| `POST /admin/client-rules/mail-test-all` | `admin.clients.test_client_rules_mail_all` |
| `POST /admin/client-rules/{rule_id}/mail-test` | `admin.clients.test_client_rule_mail` |
| `POST /admin/client-rules/{rule_id}` | `admin.clients.save_client_rule` |
| `POST /admin/client-rules/{rule_id}/reset` | `admin.clients.reset_client_rule` |
| `POST /admin/client-settings` | `admin.clients.save_client_settings` |
| `POST /admin/client-settings/check` | `admin.clients.check_client_settings` |
| `POST /admin/client-settings/reset` | `admin.clients.reset_client_settings` |
| `POST /admin/client-rules/{rule_id}/mail` | `admin.clients.send_client_rule_mail` |
| `GET /admin/client-rules/{rule_id}/patch.{fmt}` | `admin.clients.preview_client_rule_patch` |
| `POST /admin/model-changes` | `admin.clients.create_model_change` |
| `POST /admin/model-changes/{change_id}` | `admin.clients.save_model_change` |
| `POST /admin/model-changes/{change_id}/shown` | `admin.clients.set_model_change_shown` |
| `POST /admin/model-changes/{change_id}/mail` | `admin.clients.send_model_change_mail` |
| `GET /admin/model-changes/{change_id}/mail-preview` | `admin.clients.preview_model_change_mail` |
| `GET /admin/model-changes/{change_id}/patch.{fmt}` | `admin.clients.preview_model_change_patch` |

### Security

| 端點 | Handler |
|---|---|
| `POST /admin/ip-allowlist` | `admin.security.update_ip_allowlist` |
| `POST /admin/ip-allowlist/rules` | `admin.security.add_ip_allow_rule` |
| `POST /admin/ip-allowlist/rules/{rule_id}/who` | `admin.security.set_ip_allow_rule_who` |
| `POST /admin/ip-allowlist/rules/{rule_id}/end` | `admin.security.end_ip_allow_rule` |
| `POST /admin/guardrail` | `admin.security.update_guardrail` |
| `POST /admin/guardrail/test` | `admin.security.test_guardrail` |
| `GET /admin/api/export/audit-log.csv` | `admin.security.export_audit_log_csv` |

### Limits 與 Settings

| 端點 | Handler |
|---|---|
| `POST /admin/default-limit` | `admin.settings.update_default_limit` |
| `POST /admin/cloud-limit` | `admin.settings.update_cloud_limit` |
| `POST /admin/reports/{name}` | `admin.settings.save_report_schedule` |
| `GET /admin/reports/{name}/preview` | `admin.settings.preview_report` |
| `POST /admin/reports/{name}/send` | `admin.settings.send_report_now` |
| `POST /admin/site-links` | `admin.settings.update_site_links` |
| `POST /admin/cloud-budget-fallback` | `admin.settings.update_cloud_budget_fallback` |
| `POST /admin/concurrency-limit` | `admin.settings.update_concurrency_limit` |
| `POST /admin/concurrency-offpeak` | `admin.settings.update_concurrency_offpeak` |
| `POST /admin/overflow` | `admin.settings.update_overflow` |

### Models 與 Guide

| 端點 | Handler |
|---|---|
| `GET /admin/models` | `admin.model_config.admin_models_page` |
| `GET /admin/api/config` | `admin.model_config.get_config_api` |
| `POST /admin/api/test-model` | `admin.model_config.test_model_connection` |
| `PUT /admin/api/config` | `admin.model_config.save_config_api` |
| `GET /admin/guide` | `admin.guide.admin_guide_page` |
