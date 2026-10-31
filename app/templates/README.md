# Web UI Frontend Pages

[中文版](README.zh-TW.md)

All pages are protected by oauth2-proxy + nginx `auth_request` — users must complete SSO login first. Pages are rendered with Jinja2 templates + Tailwind CSS (CDN) + Chart.js.

## Page Overview

```
/                    → Welcome page (requires read or admin scope)
/dashboard           → User Dashboard (requires read or admin scope)
/admin               → Admin Panel (requires admin scope)
/admin/models        → Model Config (requires admin scope)
/setup               → Claude Code installer page (requires read or admin scope; SSO-protected)
disabled.html        → Rendered by AccountDisabledError handler when a disabled user hits any HTML route
/oauth2/sign_out     → Logout (handled by oauth2-proxy)
```

---

## `/` — Welcome Page

**File**: `welcome.html` ← `web_ui.py`
**Permission**: JWT scope includes `read` or `admin`

### Page Sections

| Section | Description |
|---|---|
| **Hero** | Title + subtitle + Go to Dashboard and First-Time Setup (`/setup`) buttons, plus an "Install Claude Code" link |
| **Quick Start** | Three-step guide: Get Your API Key → Point Your SDK → Make Requests |
| **Code Example** | Python Chat Completions example with user's API key and actual host |
| **Available Endpoints** | 7-row API endpoint table: Chat Completions, Responses, Embeddings, Rerank, Score, System One (`POST /v1/systemone`), Models (`GET /v1/models`), each with the model types it serves |
| **Available Models** | On-prem model list grouped by type, color-coded (from `MODEL_ROUTING`, `hidden` entries skipped); each card shows admin-set metadata badges (`display_name`, `context_window`, `reasoning`, `tools`, `vision`, `cache`, `max_output_tokens`) when present |
| **Feature Cards** | Three cards: Health-Aware Routing, Usage Tracking, Drop-In Compatible |

---

## `/dashboard` — User Dashboard

**File**: `dashboard.html` ← `web_ui.py`
**Permission**: JWT scope includes `read` or `admin`

The budget hero stays at the top; the lower half is split into two tabs (**Models**, the default, and **Usage**) by `gwTabs('dashTabs', 'dashTab')` (see [`base.html`](#basehtml--base-layout)). The Models tab shows a "N down" / "N queueing" count; the Usage tab shows this month's cost.

### Page Sections

| Section | Tab | Description |
|---|---|---|
| **Header** | — | "Set up Claude Code" link (→ `/setup`) and **My API Key** button (opens the key modal) |
| **Get started** | — | Shown while the account has no requests this month; links to `/setup` |
| **Spent today (budget hero)** | — | Today's cost vs `daily_limit_usd`, Remaining, time until the reset (local midnight), and a color-banded progress bar (ok < 70 % · warn < 90 % · crit < 100 % · over). `daily_limit_usd = 0` shows "No daily budget on this account". When the user may use a cloud backend (admins always), a split row shows **On-prem / Azure / AWS Bedrock** columns — only for the backends the user can use — plus a **Cloud (Azure + AWS)** row with today's combined cloud spend against the user's cloud cap (`deps.effective_cloud_limit`; hidden when there is no cap) |
| **This month** | — | Monthly cost, requests, tokens in / out (with today's figures) |
| **On-Prem Models** | Models | Every non-hidden vLLM model, grouped by type and sorted by price: ONLINE / DOWN, live load (running / queued from `/metrics`), Context, Max Out, billed In / Out $/1M (per-model → per-type → default, same as billing), capability badges (`reasoning`, `tools`, `vision`, `cache`). Header shows online / queueing / down counts and the base URL |
| **Azure OpenAI Models** | Models | Only when `can_use_azure or is_admin`. Non-hidden `[azure_models.*]` grouped by type, price-sorted (Context, In / Out $/1M, capabilities), base URL `/azure/v1`. If access is granted but nothing is configured, shows "Contact your administrator" |
| **AWS Bedrock Models** | Models | Same as Azure, for `can_use_bedrock or is_admin` and `[bedrock_models.*]`, base URL `/aws/v1` |
| **API examples** | Models | Collapsed `<details>`: Chat Completions, Embeddings, Rerank, Score snippets with the user's key and host; System One (TypeSafe SDK) only when a `systemone` model is configured |
| **Usage Trend** | Usage | Chart.js chart, 30-day trend, toggle between Requests / Cost (USD) |
| **Cost by Model** | Usage | Per-model cost grouped by backend (On-Prem / Azure / AWS Bedrock) with a share bar and doughnut chart; This Month / Today toggle and a Refresh button, both fetching `GET /dashboard/api/model-breakdown?period=month\|day` and re-rendering in place. Backends the user has no access to are left out |
| **Daily Breakdown** | Usage | Last 30 days with requests: requests, cost, input / output tokens |
| **My App Accounts** | Usage | Owned app accounts (owners only): truncated API key with Copy, monthly cost / tokens / requests, Refresh key |

### Available Actions

| Action | Path | Description |
|---|---|---|
| View API Key | Button → Modal | Shows full API key, copyable to clipboard |
| Regenerate Key | `POST /dashboard/refresh-key` | Regenerate own API key (old key immediately invalidated); from the key modal |
| Refresh App Key | `POST /dashboard/app/{id}/refresh-key` | Regenerate owned app account's API key (owner only) |
| Copy App Key | Button | Copy app account's full API key |
| Cost by Model refresh | `GET /dashboard/api/model-breakdown?period=` | JSON for the Cost by Model card (`month` or `day`) |
| Download installer | `GET /dashboard/install-claude-code.bat` | Personalized Claude Code installer (linked from `/setup`) |

---

## `/admin` — Admin Panel

**File**: `admin.html` ← `admin.py`
**Permission**: JWT scope includes `admin`

Above the tab bar: the **Needs attention** panel and the key-number cards. Below it, the shared `_admin_tabs.html` bar with **Overview / Users / Settings** (in-page tabs via `gwTabs('adminTabs', 'adminTab')`) and **Models**, a link to the separate `/admin/models` page. Tab badges: Users shows "N blocked" / "N near limit" (else the account count); Settings shows "limit: monitor|enforce" while the concurrency limit is on.

### Page Sections

| Section | Tab | Description |
|---|---|---|
| **Needs attention** | — | Three columns: open usage anomalies from `scripts/scan_anomalies.py` (severity, user/model, rule, summary; Ack / Resolve buttons), on-prem models down or queueing, and accounts at ≥ 90 % of today's daily limit (each links to `?q=<username>#users`). Collapses to "All clear" when empty |
| **Key numbers** | — | Spend today, Requests today, Active users today, In flight now (concurrency leases; "tracking off" when the limit is `off`), this month's cost / requests / tokens |
| **DAU Trend** | Overview | Chart.js bar chart, 30-day Daily Active Users, today's DAU in top-right |
| **App Leaderboard** | Overview | App account ranking (`app_*`), top-10 by monthly cost descending |
| **User Leaderboard** | Overview | User ranking, top-10 by monthly cost descending, shows display_name and org_code badge |
| **Department Usage** | Overview | Usage grouped by org_code: Users, Cost, Input / Output Tokens, Reqs; client-side pager past 15 rows |
| **Export Usage Report** | Overview | Month range (max 24 months) → **Download xlsx** (`GET /admin/api/export/usage.xlsx`, 4 sheets) or **Backend costs (CSV)** (`GET /admin/api/export/user-backend-costs.csv`, one row per month × account with On-prem / Azure / Bedrock cost columns) |
| **User Management** | Users | Users / Apps sub-toggle, server-side search (`q`, ILIKE on username / display_name / org_code), server-side pagination (`limit/offset`, `app_limit/app_offset`), **Export Users (CSV)** (`GET /admin/api/export/users.csv`). Users table: User, API Key (View Key), Total Spend, Daily Limit (+ *Cloud $*), Actions. Apps table adds an editable Owner column |
| **Concurrency Limit** | Settings | Mode (`off` / `monitor` / `enforce`) + per-account limit (≥ 1) → `POST /admin/concurrency-limit`; shows requests in flight, accounts and busiest account. Enforce asks for confirmation |
| **Default Daily Limit** | Settings | `POST /admin/default-limit` — sets `[app].default_daily_limit_usd` and bulk-bumps any user with `0 < daily_limit_usd < new_floor` up to the floor (users at `daily_limit_usd = 0`, a deliberate $0 budget, are never modified) |
| **Cloud Daily Limit** | Settings | `POST /admin/cloud-limit` — default cap on each user's combined Azure + AWS spend per day (`[app].cloud_daily_limit_usd`) for users with cloud access and no *Cloud $* of their own. Blank = no default cap; 0 = cloud refused |
| **Cloud Budget Fallback** | Settings | Checkbox → `POST /admin/cloud-budget-fallback`. When on, a `/v1/*` cloud request over the cloud cap is served on-prem instead of a 429 (the overall daily limit still applies; `/azure/v1/*` and `/aws/v1/*` never fall back) |
| **Create App Account** | Settings | Username (auto-prefixed with `app_`), Daily Limit, Owner → `POST /admin/users/create`. App accounts are created concurrency-waived |
| **Site Links** | Settings | Support Bot URL (floating button on every page) and Install Guide URL (card on `/setup`) → `POST /admin/site-links`. Only `http(s)://` or site-relative URLs; empty hides the element |

### Actions column

Each user / app row has these buttons, left to right:

| Button | Path | Rendered for | Behavior |
|---|---|---|---|
| **Azure** | `POST /admin/users/{id}/toggle-azure` | every row | Flips `can_use_azure` (purple when granted, ghost when not). In place |
| **AWS** | `POST /admin/users/{id}/toggle-bedrock` | every row | Flips `can_use_bedrock` (orange when granted, ghost when not). In place |
| **Waive** | `POST /admin/users/{id}/toggle-concurrency-waive` | non-admin rows | Flips `concurrency_waived` (green when waived). In place |
| **Disable / Enable** | `POST /admin/users/{id}/toggle-disable` | non-admin user rows, every app row | Flips `is_disabled` (warn-yellow "Enable" when disabled). The server refuses self-disable. Full POST → redirect |
| **Delete** | `POST /admin/users/{id}/delete` | non-admin user rows, every app row | Deletes the user and the rows referencing it (ownerships, anomalies, leases); usage is kept for billing / audit; cannot delete self. Full POST → redirect |

Every Actions button asks for confirmation first. The in-place toggles (Azure, AWS, Waive) listen in the bubble phase, after the form's own `onsubmit` confirm, so Cancel sends nothing; on OK they POST with `Accept: application/json`, get the new flag back as JSON and repaint only that button; without that header the endpoints answer with the classic 303 → `/admin`.

### Other Actions

| Action | Path | Description |
|---|---|---|
| Update Daily Limit | `POST /admin/users/{id}/limit` | Change user's daily spend limit (finite, ≥ 0; 0 is a $0 budget) |
| Update Cloud $ | `POST /admin/users/{id}/cloud-limit` | User's own Azure + AWS daily cap. Shown for users with Azure or AWS access and for admins; the placeholder shows the default; empty clears it back to the default, 0 refuses cloud |
| Set Owners | `POST /admin/users/{id}/owner` | Apps table: replace the app's owners (comma-separated usernames) |
| View API Key | `GET /admin/users/{id}/api-key` | Listings carry masked keys only; the modal fetches the full key per account (logged) |
| Regenerate Key | `POST /admin/users/{id}/refresh-key` | Regenerate any user's API key (from the key modal) |
| Ack / Resolve anomaly | `POST /admin/api/anomalies/{id}/ack` · `/resolve` | From the Needs attention panel |

> Limit and Cloud $ saves are submitted with `fetch` and flash the input green / red instead of reloading. The Cloud Budget Fallback checkbox posts an explicit `enabled=on|off` (a disabled control would be dropped from `FormData`). Actions that do reload (Disable, Delete, search, pagination, form POSTs) restore the scroll position and the Users / Apps sub-toggle from `sessionStorage`, and the active tab via `gwTabs`' `sessionStorage` fallback, since every POST redirects to plain `/admin`.

The same router also serves JSON endpoints that the page does not use: `GET` / `POST /admin/users`, `PATCH /admin/users/{id}`, `GET /admin/api/anomalies`.

---

## `/admin/models` — Model Config

**File**: `admin_models.html` ← `admin.py`
**Permission**: JWT scope includes `admin`

Master–detail editor over `GET` / `PUT /admin/api/config`, rendered under the same `_admin_tabs.html` bar (Overview / Users / Settings link back to `/admin#<tab>`; Models is active). It is a separate page on purpose: its URL hash is the selection, and its unsaved-changes guard must fire on navigation.

### Page Sections

| Section | Description |
|---|---|
| **Model list** (left) | Searchable (alias, real model / deployment / model ID, display name). Groups: On-prem (vLLM) by type, Azure OpenAI, AWS Bedrock, each with a count and **+ Add**. Badges: `vlm` (cloud), `think` (`is_reasoning`), `429→<alias>` (rate-limit fallback), `hidden`. Amber dot = edited since last save; red dot = missing required fields. A **Fallbacks & pricing** entry sits on top |
| **Model editor** (right) | Header with backend, type, served path and "used as fallback by …"; **Duplicate** / **Rename** / **Delete**. Sections: **Connection** (type + vLLM `real_model` / `base_url` / `api_key`; Azure `endpoint` / `deployment` / `api_key` / `api_version`; Bedrock `model_id` / `region` / `api_key` / `endpoint` / IAM key pair, secrets masked with Show), **Routing & visibility** (listed in `/v1/models` ↔ `hidden`; vLLM and Bedrock (Claude only) `native_messages`; cloud `rate_limit_fallback` with the resolved chain, up to 3 hops), **Pricing overrides** (input / output / cached input $/1M; blank = type default), **Reasoning** (`is_reasoning`, accepted `reasoning_efforts`, `reasoning_effort_map` for unaccepted levels: map or strip), **Metadata** (`display_name`, `context_window`, `max_output_tokens`, `supports_tools` / `vision` / `prompt_caching`) |
| **Fallbacks & pricing** | Per-type fallback dropdowns for vLLM, Azure and Bedrock (`fallback`, `azure_fallback`, `bedrock_fallback`; blank = auto) and the type pricing table (Default + each type, input / output $/1M) |
| **Unsaved-changes bar** | Sticky bar with Discard / Save while the config differs from the last loaded snapshot |

### Behavior

- Selection lives in the URL hash (`#vllm/<alias>`, `#azure/<alias>`, `#bedrock/<alias>`, `#settings`), so a reload stays put.
- Every input writes straight into the page's `config` object through `setField` (`data-kind` / `data-alias` / `data-field` / `data-t` coercion; blank optional fields delete their key). There is no DOM re-collection at save time — only the selected model's form is in the DOM.
- Dirty tracking compares `JSON.stringify(config)` with the last saved snapshot; Ctrl/Cmd+S saves, `beforeunload` warns.
- Add / Duplicate / Rename share one dialog that enforces alias uniqueness across all three backends. Rename and Delete repoint or clear type fallbacks and `rate_limit_fallback` references.

### Available Actions

| Action | Path | Description |
|---|---|---|
| Add Model | Button → dialog | Pick backend, type and alias; creates an empty entry to fill in |
| Duplicate / Rename / Delete | Editor header | Copy all settings into a new alias (default `<alias>-copy`) / rename / delete (takes effect on save) |
| Set Fallback | Fallbacks & pricing | Per-type fallback model for each backend |
| Edit Pricing | Fallbacks & pricing / model editor | Type pricing table, or per-model overrides |
| Save | `PUT /admin/api/config` | Refused client-side while any model is missing a required field; the server validates (including cross-backend alias collisions) before writing `config.toml`, then reloads |
| Discard | Button | Revert to the last loaded config |

---

## `/setup` — Claude Code Installer Page

**File**: `setup.html` ← `web_ui.py`
**Permission**: JWT scope includes `read` or `admin` (SSO-protected; nginx does not bypass oauth2-proxy for `/setup`)

Single-purpose page that walks the user through installing the Claude Code CLI: (1) download the personalized installer (`GET /dashboard/install-claude-code.bat`, which inlines the requesting user's API key; when `setup/install-claude-code.bat` is missing on the server the page asks the user to contact the operator instead), (2) run it, (3) a `~/.claude/settings.json` reference with the supported `env` keys grouped by topic. A collapsed Troubleshooting section follows. When the admin has set `install_guide_url` (Admin → Settings → Site Links), a highlighted "Step-by-step Install Guide" card is rendered above the steps.

The former CA-certificate tab (and its `/setup/files/<name>` download route) was removed — the gateway no longer ships a cert installer.

---

## `disabled.html` — Account Disabled Page

**File**: `disabled.html` ← rendered by the global `AccountDisabledError` handler in `app/main.py`

Standalone styled error page (no `base.html` chrome) returned with HTTP 403 when a disabled user opens any browser-bound route. The handler discriminates by the `Accept` header:

- `Accept` contains `text/html` → this page (Account Disabled headline + explanation + Sign Out button → `/oauth2/sign_out`)
- Otherwise → JSON 403 `{"detail": "Account disabled. Contact your administrator."}` (for SDKs and curl)

Admins are exempt from the disable check at auth time, so this page never blocks an admin from logging in to fix the situation.

---

## Shared Components

### `base.html` — Base Layout

Shared framework for all pages:

- **Navbar** — Logo (App title initial) + App title (→ `/dashboard`), Setup link, Admin link (shown for admin scope), display name + org_code badge, Logout button
- **Footer** — Copyright + year + App title
- **Support Bot button** — floating bottom-right link, rendered only when `support_bot_url` is set (Admin → Site Links); read through the `get_site_links` template global
- **`gwTabs(barId, storageKey)`** — page tabs: buttons `data-tab="x"` in the bar, panels `data-panel="x"` (several panels may share a name). Active tab comes from the URL hash, else `sessionStorage[storageKey]` (form POSTs redirect to a URL without the hash), else the first tab. Fires a `resize` event after each switch so charts in a hidden tab get sized
- **Tooltips** — any element with `gw-tip` + `data-tip` (`gw-tip-wide` for long text)
- **CDN** — Tailwind CSS, Chart.js, Plus Jakarta Sans + JetBrains Mono fonts

### `_admin_tabs.html` — Admin Tab Bar

Included by `admin.html` (`on_admin_page = true`: Overview / Users / Settings are `data-tab` buttons) and `admin_models.html` (`on_admin_page = false`: they are links to `/admin#<tab>`). The fourth tab, **Models**, is always a link to `/admin/models`.

### Auth Flow

```
Browser → Nginx → auth_request /oauth2/auth → oauth2-proxy validates cookie
                                ↓ pass
                  Nginx injects Authorization: Bearer <JWT>
                                ↓
                  FastAPI → get_web_user() decodes JWT
                                ↓
                  Page access determined by scopes
```

| Scope | Accessible Pages |
|---|---|
| `read` | `/`, `/dashboard`, `/setup` |
| `admin` | `/`, `/dashboard`, `/setup`, `/admin`, `/admin/models` |

### Logout

Logout button points to `/oauth2/sign_out` — oauth2-proxy clears the session cookie and redirects to login page.
