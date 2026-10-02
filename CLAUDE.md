# CLAUDE.md

Guidance for Claude Code (claude.ai/code) in this repository.

OpenAI- and Anthropic-compatible reverse proxy in front of on-prem vLLM servers, Azure OpenAI and AWS Bedrock, with per-user API keys, budgets, concurrency limits and usage billing. FastAPI + httpx + SQLModel/PostgreSQL + Jinja2.

This file is the map and the rules. **[docs/architecture.md](docs/architecture.md) is the reference**: read the section for a subsystem before changing it. When you change behavior, update that section; when you add a rule someone could easily break, add a line under *Rules* below.

## Commands

```bash
uv sync                                    # install
uv run fastapi dev app/main.py             # run with auto-reload
uv run pytest tests/ -q                    # all tests (CI runs the same)
uv run pytest tests/test_messages.py::TestX::test_y -v   # one test
uv run ruff check . [--fix]                # lint (CI runs the same)
uv run alembic upgrade head                # apply migrations
uv run alembic revision --autogenerate -m "description"
uv add <package>                           # add a dependency (commit uv.lock too)
```

## Repo map

| Path | What lives there |
|---|---|
| `app/main.py` | App, lifespan (DB init, httpx clients, health loop, concurrency-lease heartbeat), middleware, `AccountDisabledError` handler |
| `app/core/config.py` | `config.toml` → `MODEL_ROUTING`, `AZURE_MODELS`, `BEDROCK_MODELS`, pricing and fallback maps; `[app]` getters/setters; `save_config` |
| `app/core/deps.py` | API-key auth, daily limits and cloud sub-limits, concurrency-limited dependencies |
| `app/core/auth.py` | Web-UI JWT auth (`get_web_user`, scopes) |
| `app/core/server_state.py` | Shared / Azure / Bedrock / health httpx clients, health and metrics caches |
| `app/routers/` | `v1_api` (`/v1/*`, unified dispatch), `azure_api` (`/azure/v1/*`), `aws_api` (`/aws/v1/*`), `web_ui` (dashboard, setup), `admin`, `health_api` |
| `app/services/vllm_proxy.py` | vLLM forwarding, failover, SSE pump, `_log_usage` (billing + Langfuse seam) |
| `app/services/azure_proxy.py`, `bedrock_proxy.py` | Cloud forwarding; translation via `responses_adapter.py` / `converse_adapter.py` |
| `app/services/anthropic_adapter.py` | Anthropic Messages ↔ OpenAI chat (the internal pivot format) |
| `app/services/` (rest) | `concurrency`, `rate_limit_fallback`, `reasoning_effort`, `health`, `observability`, `redact`, `stats`, `analytics` / `usage_export`, `aws_eventstream` |
| `app/models/schema.py` | `User`, `UsageLog`, `AppOwner`, `AnomalyEvent`, `ConcurrencyLease` |
| `app/templates/` | `dashboard.html`, `admin.html`, `admin_models.html` (model-config SPA), `base.html` |
| `alembic/versions/` | Migrations (PostgreSQL-only: they do not run on SQLite) |
| `scripts/` | `scan_anomalies.py` (hourly timer), `cleanup_usage_logs.py`, SQLite → PG migration, dev PG helper |
| `deploy/` | systemd units, nginx example, docker-compose (PG + oauth2-proxy), `setup.sh` |
| `setup/install-claude-code-example.bat` | Windows Claude Code installer, personalized with the user's key by `/dashboard/install-claude-code.bat` |

## Request flow

```
Client (API key) → deps.py: auth, daily limit, [concurrency slot]
   /v1/*        → v1_api.py: alias in AZURE_MODELS + can_use_azure     → azure_proxy   (→ Responses API)
                             alias in BEDROCK_MODELS + can_use_bedrock → bedrock_proxy (→ Converse API)
                             otherwise                                 → vllm_proxy
   /azure/v1/*  → azure_api.py  → azure_proxy   (require_azure_access)
   /aws/v1/*    → aws_api.py    → bedrock_proxy (require_bedrock_access)
   → downstream → _log_usage() → usage_logs + Langfuse (no-op unless configured)
```

Admins bypass the Azure/Bedrock flags. Every route also exists without the `/v1` prefix. Naming split: the URL prefix is **`/aws`**, but config, flags and the backend tag all say **`bedrock`** (`[bedrock_models.*]`, `can_use_bedrock`, `backend="bedrock"`).

## Rules

Each of these has broken production or a test before. The parenthesized test pins it.

**DB connections and request lifetime**
- API-path auth dependencies open their **own short-lived session** (`with Session(engine)`), never `Depends(get_session)`. A `yield` dependency's exit runs after the response is sent, streams included, so a request-scoped session pinned one pool connection per open stream and exhausted the QueuePool (`test_auth_session_scope.py`). The web/admin routers may keep `Depends(get_session)`.
- That same exit timing is what keeps a concurrency lease held for a whole stream. If a FastAPI upgrade ever changes it, the limit silently stops limiting (`TestLeaseSpansTheResponse`).
- Teardown that runs on client disconnect must survive anyio's repeated cancellation: detach it (`_release_stream`) or shield it (`anyio.CancelScope(shield=True)`). Otherwise connections and leases leak (`test_stream_disconnect_cleanup.py`, `TestClientDisconnect`).
- Never hold a DB connection across a stream: no advisory locks, no long transactions.

**Streams**
- Every streaming pre-flight uses `_STREAM_TIMEOUT` (time-to-headers is bounded) and checks `status_code` **before** handing the response to the pump. A cloud 4xx is a JSON body that the pump would silently drop (`test_stream_preflight_timeout.py`).
- Ping heartbeats are injected only at SSE event boundaries. OpenAI-shape streams consume the idle ticks silently.
- An Anthropic stream that ends without `finish_reason` emits `overloaded_error` (Claude Code retries it), never `message_stop`.
- Failover or rate-limit fallback may switch servers only before the client has received a byte.

**Side effects never break a request**
- Billing (`_log_usage`), Langfuse, slow-request logging and the concurrency store are wrapped so their failure can't fail the request. The concurrency store fails **open**. The blind `except Exception` around them is intentional.
- `_log_usage` is the single billing and observability seam. Debug and metadata calls (`render`, `tokenize`, `count_tokens`) must never reach it.

**Routing and config**
- Aliases are unique across `[models.*]`, `[azure_models.*]` and `[bedrock_models.*]`. `_build_config` raises at startup; `PUT /admin/api/config` rejects collisions before writing.
- An unknown alias falls back (with `X-Model-Fallback`) rather than returning 404. A cloud alias requested without permission silently falls back to vLLM: cloud models are hidden through the `/v1/models` filter, not through errors.
- An exhausted cloud sub-limit is a **429**, not a silent vLLM fallback, unless `[app].cloud_budget_fallback` is on, and that only applies on `/v1/*`.
- `rate_limit_fallback` stays on the **same backend**, and the request body is rebuilt per attempt from a deep copy of the original.
- Reasoning effort: no nearest-level guessing. Forward what was asked, what config maps it to, or nothing. An undeclared route is byte-faithful. `max` is a level of its own, not a spelling of `xhigh`. `reasoning_effort` is only emitted for `is_reasoning` models.
- Thinking history replayed downstream carries **both** `reasoning` and `reasoning_content` (vLLM ≥ 0.20 reads only the former).
- System messages appear only at index 0. Mid-conversation system/developer entries merge into the adjacent user turn as `<system-reminder>` text: hoisting them would break prefix caching.
- New per-model config keys go in the right tuple: `_MODEL_METADATA_KEYS` (surfaced in `/v1/models`), `_MODEL_PRICING_KEYS`, `_MODEL_REASONING_KEYS`, or the internal/cloud-routing keys (never surfaced).
- `/readyz` gates on the database only. The vLLM health cache is per-worker and failing on downstream outages would pull every instance out of the load balancer at once.

**Security and logs**
- Never log raw request bodies. Use `redact.summarize_body()` (shape only); `LOG_REQUEST_BODIES=true` is a debugging escape hatch.
- Admin listings show `mask_api_key(...)`. Full keys come one at a time from `GET /admin/users/{id}/api-key`, which is logged.
- Admin-editable site links accept only `http(s)://` or site-relative URLs.
- Downstream error lines include `user`, `model`, `endpoint` and the exception **type**. httpx timeouts stringify to an empty message.

**Admin UI**
- In-place toggles POST with `Accept: application/json` and repaint one element. Send explicit values (`enabled=on|off`): disabled controls are dropped from `FormData`.
- `/dashboard` and `/admin` split their lower half into tabs via `gwTabs(barId, storageKey)` in `base.html`: buttons `data-tab="x"`, panels `data-panel="x"` (several panels may share a name). The active tab comes from the URL hash, else `sessionStorage`. The fallback is required because every admin form POST redirects to plain `/admin`, dropping the hash. The budget hero and the "Needs attention" panel stay above the tab bar. Charts inside a hidden tab are sized on the `resize` event the switch fires. The admin bar is the shared partial `_admin_tabs.html`: its fourth tab, **Models**, is a link to the separate `/admin/models` page, which renders the same bar with links back to `/admin#<tab>`. Model Config is deliberately not embedded: its URL hash is the model selection (`#settings` would collide with the admin tab of that name) and its unsaved-changes guard must keep firing on navigation.
- `admin_models.html` writes every input straight into its `config` object through `setField`. There is no DOM re-collection at save time.

**Lint and config**
- Ruff's rule set is deliberately narrow (`E4`, `E7`, `E9`, `F`). Don't widen it: the default set fights intentional patterns such as blind excepts and `Depends()` defaults.
- `config.toml` is gitignored. Reads fall back to `config.toml.example` (CI has no `config.toml`); `save_config` always writes `config.toml`.

## Subsystems

One paragraph each. Follow the link for the full behavior.

- **[Unified dispatch](docs/architecture.md#unified-v1-dispatch)**: `_peek_model_alias` reads `body["model"]` from the cached body; `_route_to_azure` / `_route_to_bedrock` decide. Each base URL has its own fallback domain. `/v1/responses` has no Bedrock dispatch.
- **[Auth](docs/architecture.md#authentication)**: API key via `Authorization: Bearer` or `x-api-key`. Web UI: oauth2-proxy → nginx `auth_request` → JWT (RS256, key reloaded on mtime). Admin = `"admin"` in JWT scopes, not `user.is_admin`. Disabled accounts raise `AccountDisabledError`, which renders HTML or JSON 403 based on `Accept`; admins bypass.
- **[vLLM proxy](docs/architecture.md#vllm-proxy-appservicesvllm_proxypy)**: eight `vllm_forward_*` methods sharing `_resolve_model` (exact → type fallback → any alive). One request-time failover on connect failure or 502/503/504 (connect failures `mark_down()` the server). `native_messages` routes pass Anthropic bodies through natively after `normalize_anthropic_messages` + `sanitize_native_messages_body`. `systemone` (decider) and `render` (template preview, decoded by default) have their own sections.
- **[Azure](docs/architecture.md#azure-openai-proxy-appservicesazure_proxypy)**: every LLM/VLM call goes to the v1 Responses API. Sampling knobs are stripped on chat/messages (not on `/azure/v1/responses`), `max_output_tokens` is raised to at least 16, and orphan `function_call`s are dropped. Mid-stream errors are surfaced, with rate limits mapped to `overloaded_error`. Optional `AZURE_HTTP_PROXY` / `AZURE_INSECURE` client.
- **[Bedrock](docs/architecture.md#aws-bedrock-proxy-appservicesbedrock_proxypy)**: every call goes to Converse. Streams are binary AWS event-stream (`aws_eventstream.py`), not SSE. Auth is a long-term API key as Bearer (no SigV4). Roles are merged to alternate. Effort → thinking budget for `anthropic.*` model IDs.
- **[Rate-limit fallback](docs/architecture.md#rate-limit-fallback-appservicesrate_limit_fallbackpy)**: cloud entries may chain `rate_limit_fallback` on 429 (up to 3 hops). Azure streams are read ahead for up to 5 s to catch an in-stream rate-limit event.
- **[Anthropic adapter](docs/architecture.md#anthropic-messages-api-appservicesanthropic_adapterpy)**: stateless request/response translation and `AnthropicStreamTranslator`. 10 s pings, 300 s max idle, 280 s non-stream timeout (below nginx's 300 s). `count_tokens` uses vLLM `/tokenize`, falling back to chars/4.
- **[Reasoning effort](docs/architecture.md#reasoning-effort-compatibility-appservicesreasoning_effortpy)**: per-model `reasoning_efforts` / `reasoning_effort_map`, applied at each backend's outgoing body; edited in Admin → Model Config.
- **[Cost and budgets](docs/architecture.md#cost-calculation-budgets-cloud-budget-fallback)**: price lookup is per-model → per-type → default, with an optional cached-input rate. `usage_logs.backend` splits spend. Azure/Bedrock daily sub-limits cap that backend's share. 429s carry `Retry-After` until local midnight.
- **[Concurrency limit](docs/architecture.md#per-user-concurrency-limit-appservicesconcurrencypy)**: `[app].concurrency_limit_mode` (`off` / `monitor` / `enforce`) + `concurrency_limit` (≥ 1; "unlimited" is `off`, never `0`). Leases live in `concurrency_leases`, taken by `deps.limited_*` on chat/responses/messages only. The heartbeat renews leases and sweeps those of dead workers, including once at startup. Admins and `users.concurrency_waived` are exempt.
- **[Health and metrics](docs/architecture.md#runtime-patterns-http-clients-health-loop-logging-export)**: 30 s loop on a dedicated client, with hysteresis (2 failures to go DOWN). Scrapes vLLM `/metrics` with an **exact** metric-name match (vLLM ≥ 0.20's `*_waiting_by_reason` would double-count). `PoolTimeout` keeps the previous state.
- **[Observability](docs/architecture.md#observability-appservicesobservabilitypy)**: one Langfuse generation per billable request, emitted from `_log_usage`. Errors are recorded via `_log_error`. Request headers are captured by `RequestMetaMiddleware` (pure ASGI). I/O capture (`LANGFUSE_CAPTURE_IO`) records the endpoint's own shape.
- **[Logging](docs/architecture.md#error-log-redaction-appservicesredactpy)**: per-worker log files. `Slow request` / `Slow response headers` warnings above `SLOW_REQUEST_WARN_S`.
- **[Admin Model Config](docs/architecture.md#admin-model-config-page-apptemplatesadmin_modelshtml)**: master–detail editor over `GET/PUT /admin/api/config`. Selection lives in the URL hash; dirty tracking compares JSON snapshots.
- **[Database](docs/architecture.md#database)**: `users`, `usage_logs`, `app_owners`, `anomaly_events`, `concurrency_leases`. The hourly anomaly scan is detection only, never blocking. The usage-log retention timer ships disabled on purpose.

## Testing

Tests use in-memory SQLite with `StaticPool` (all connections share one DB), set up in `tests/conftest.py`:

- `os.environ["DATABASE_URL"] = "sqlite://"` is set before any app import.
- The `_patch_all` autouse fixture patches the routing/pricing maps, every httpx client getter, every module-level `engine` (including `app.services.concurrency.engine`), `is_alive`, and the JWT decoder.
- The `client` fixture builds a test app with a no-op lifespan (no health loop, no lease heartbeat) and mounts every router. Use it with `auth_header()` (API key) or `web_auth_header(sub=..., scopes=["admin"])` (JWT).
- Mock downstreams on `client.__httpx_mock__.post` (non-stream) or `.send` (stream; `FakeStreamResponse`, `FakeBedrockStreamResponse`).

Pitfalls:
- **Never let a test trigger a real `save_config()`.** It writes `config.toml` and `reload_config()` mutates the patched `TEST_*` dicts in place, emptying them for every later test. Wrap happy-path `PUT /admin/api/config` in `with patch("app.routers.admin.save_config"):` (pattern: `tests/test_models_endpoint.py::TestAdminConfigMetadataValidation`). `TestAzureFallback` in `test_azure_api.py` deliberately mutates `TEST_AZURE_MODELS`; keep the autouse patch pointing at the same dict.
- The shared httpx mock's `reset_mock()` does **not** clear `side_effect`, and some tests replace its methods. Use `patch.object(client.__httpx_mock__, "post", side_effect=...)` and assert on the mock you patched, never on the shared one.
- Import `app.routers.*` inside test functions, never at a test module's top level. `_build_test_app` imports the routers under its config patches; a collection-time import binds them to the real config and the page silently renders the wrong models.
- The concurrency limit defaults to `off`. Tests that exercise it patch `app.core.deps.get_concurrency_settings`.
- SQLite ignores `FOR UPDATE`. `test_pg_concurrent_acquire_respects_limit` runs against PostgreSQL when `CONCURRENCY_TEST_PG_URL` is set; it is skipped in CI.

## Database and migrations

- Production is PostgreSQL. Migrations use PostgreSQL-only DDL, so verify them against a real PostgreSQL (a throwaway `initdb` cluster works; e.g. `/usr/lib/postgresql/16/bin` when installed), never SQLite. Check upgrade → downgrade → upgrade, then `alembic check` for model drift.
- Migrations that touch `users` start with `SET LOCAL lock_timeout = '5s'` and add columns with a constant `server_default` (metadata-only on PG ≥ 11), so they are safe under live traffic.
- Deploy order: `alembic upgrade head`, then restart.
- `usage_logs` is the billing and audit record. Retention cleanup is a deliberate governance decision, never an installer default.

## CI

`.github/workflows/ci.yml`: `uv sync --locked --dev` → `ruff check .` → `pytest tests/ -q` on Python 3.11 for pushes to `main` and every PR. `--locked` fails a PR that edits `pyproject.toml` without re-running `uv lock`.
