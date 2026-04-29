# Web UI Frontend Pages

[中文版](README.zh-TW.md)

All pages are protected by oauth2-proxy + nginx `auth_request` — users must complete SSO login first. Pages are rendered with Jinja2 templates + Tailwind CSS (CDN) + Chart.js.

## Page Overview

```
/                    → Welcome page (requires read or admin scope)
/dashboard           → User Dashboard (requires read or admin scope)
/admin               → Admin Panel (requires admin scope)
/admin/models        → Model Config (requires admin scope)
/setup               → CA cert install page (requires read or admin scope; SSO-protected)
/oauth2/sign_out     → Logout (handled by oauth2-proxy)
```

---

## `/` — Welcome Page

**File**: `welcome.html` ← `web_ui.py`
**Permission**: JWT scope includes `read` or `admin`

### Page Sections

| Section | Description |
|---|---|
| **Hero** | Title + subtitle + Go to Dashboard button |
| **Quick Start** | Three-step guide: Get API Key → Configure SDK → Send Request |
| **Code Example** | Python Chat Completions example with user's API key and actual host |
| **Available Endpoints** | 6 API endpoint table (Chat, Responses, Embeddings, Rerank, Score, Models) |
| **Available Models** | Model list grouped by type, color-coded (from `MODEL_ROUTING`) |
| **Feature Cards** | Three cards: Health-Aware Routing, Usage Tracking, Drop-In Compatible |

---

## `/dashboard` — User Dashboard

**File**: `dashboard.html` ← `web_ui.py`
**Permission**: JWT scope includes `read` or `admin`

### Page Sections

| Section | Description |
|---|---|
| **System Status** | All downstream server health, grouped by type (LLM / VLM / Embedding / Reranker), real-time ONLINE / DOWN |
| **Stats Cards** | Monthly stat cards: Requests count, Estimated Cost, Budget Usage (progress bar + percentage + today's `Remaining: $X.XXXX / $Y` display, or "Unlimited" when `daily_limit_usd = 0`), Total Tokens (Input / Output) |
| **Usage Trend** | Chart.js line chart, 30-day usage trend, toggle between Requests / Cost (USD) views |
| **My App Accounts** | Owned app account list (visible to owners only), shows API key, monthly usage, Copy / Refresh Key |
| **Claude Code Installer** | Card to download a personalized PowerShell installer; the script is generated server-side via `GET /dashboard/install-claude-code.ps1` (auth-required), with the user's API key inlined into the template |
| **API Integration Guide** | Inline code examples: Chat Completions, Embeddings, Rerank, Score, with user's API key and actual host |

### Available Actions

| Action | Path | Description |
|---|---|---|
| View API Key | Button → Modal | Shows full API key, copyable to clipboard |
| Regenerate Key | `POST /dashboard/refresh-key` | Regenerate own API key (old key immediately invalidated) |
| Refresh App Key | `POST /dashboard/app/{id}/refresh-key` | Regenerate owned app account's API key (owner only) |
| Copy App Key | Button | Copy app account's full API key |

---

## `/admin` — Admin Panel

**File**: `admin.html` ← `admin.py`
**Permission**: JWT scope includes `admin`

### Page Sections

| Section | Description |
|---|---|
| **Monthly Summary** | Platform-wide monthly totals: Total Requests, Total Cost, Input / Output Tokens |
| **DAU Trend** | Chart.js bar chart, 30-day Daily Active Users, today's DAU in top-right |
| **App Leaderboard** | App account ranking (`app_*`), top-10 by monthly cost descending |
| **User Leaderboard** | User ranking, top-10 by monthly cost descending, shows display_name and org_code badge |
| **Department Usage** | Usage table grouped by org_code: Users, Cost, Input / Output Tokens, Reqs |
| **Create App Account** | Form to create new app account: username (auto-prefixed with `app_`), Daily Limit, Owner |
| **User Management** | Server-side paginated (limit/offset) user table with server-side search (`q` parameter, ILIKE on username/display_name/org_code), Users / Apps tab switching. Header shows a **Default Limit** input + Save button (`POST /admin/default-limit`) — sets `[app].default_daily_limit_usd` and bulk-bumps any user with `0 < daily_limit_usd < new_floor` up to the floor (unlimited users with `daily_limit_usd = 0` are never modified) |

### Available Actions

| Action | Path | Description |
|---|---|---|
| Create App Account | `POST /admin/users/create` | Create `app_*` account, optionally assign owner and daily limit |
| Update Daily Limit | `POST /admin/users/{id}/limit` | Change user's daily spend limit |
| View API Key | Button → Modal | View any user's full API key |
| Regenerate Key | `POST /admin/users/{id}/refresh-key` | Regenerate any user's API key |
| Toggle Monitor | `POST /admin/users/{id}/monitor` | Enable/disable request/response monitoring for a user |
| Delete User | `POST /admin/users/{id}/delete` | Delete user and all their usage logs (cannot delete self) |

---

## `/admin/models` — Model Config

**File**: `admin_models.html` ← `admin.py`
**Permission**: JWT scope includes `admin`

### Page Sections

| Section | Description |
|---|---|
| **Tab Navigation** | Tabs by model type: llm, vlm, embedding, vision_embedding, reranker, vision_reranker |
| **Model Table** | Model list per type, editable Real Model, Base URL, API Key |
| **Fallback Dropdown** | Per-type fallback model selection (preferred when server is down) |
| **Pricing Table** | Per-type pricing: Default + each type's Input / Output Price (USD per 1M tokens) |

### Available Actions

| Action | Path | Description |
|---|---|---|
| Add Model | Button → prompt | Enter alias to add model, default base_url is localhost:8000 |
| Edit Model | Inline table edit | Directly modify Real Model, Base URL, API Key |
| Delete Model | Button | Delete model (also cleans up related fallback) |
| Set Fallback | Dropdown | Select fallback model for that type |
| Edit Pricing | Inline table edit | Modify Input / Output pricing |
| Save All | `PUT /admin/api/config` | Write all changes to `config.toml` and apply immediately |

---

## `/setup` — CA Certificate Install Page

**File**: `setup.html` ← `web_ui.py`
**Permission**: JWT scope includes `read` or `admin` (SSO-protected; nginx no longer bypasses oauth2-proxy for `/setup`)

This page is for **Claude desktop / Office** users who need the gateway's internal CA certificate trusted on Windows. The page is intentionally separate from the dashboard's Claude Code installer, and the on-page copy makes the distinction explicit:

- **This page** — CA cert (so corporate browsers and Claude in Office can reach the HTTPS gateway)
- **Not this page** — Claude Code CLI installer (download from the Dashboard instead)

### Downloads

The user-facing UI offers only the `.bat` installer. The `.ps1` installer (`install-cert-user.ps1`) still lives in `setup/` for ops to use manually but is **not** in the download whitelist (`_SETUP_ALLOWED` in `app/routers/web_ui.py`):

| File | Whitelisted? | Purpose |
|---|---|---|
| `llm-gateway-ca.crt` | yes | Internal CA certificate |
| `install-cert.bat` | yes | Windows batch installer (CurrentUser\Root, no admin required) |
| `install-cert-user.ps1` | no | PowerShell equivalent — kept in repo for ops, not downloadable |

---

## Shared Components

### `base.html` — Base Layout

Shared framework for all pages:

- **Navbar** — Logo (App title initial) + App title, Admin link (shown for admin scope), Hello {display_name} + org_code badge, Logout button
- **Footer** — Copyright + year
- **CDN** — Tailwind CSS, Chart.js, Plus Jakarta Sans + JetBrains Mono fonts

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
