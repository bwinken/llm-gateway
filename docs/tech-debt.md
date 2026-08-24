# Tech Debt

Known suboptimal-but-shipped items. Each entry should have:
- **What** — the issue in one line
- **Context** — why it exists, when it was incurred
- **Impact** — what's affected today
- **Suggested fix** — direction for whoever picks it up
- **Added** — date noted

Pick items off as time allows. New entries go to the top.

---

## Azure `api_version` field is loaded but unused (2026-05-22)

**What.** `api_version` is still threaded through the config loader and exposed in the admin model-config UI, but no longer used by any HTTP request. The v1 Responses surface (`/openai/v1/responses`) does not accept an `api-version` query string.

**Context.** The Azure path used to send requests to `/openai/deployments/<name>/<path>?api-version=...`, so the field was a real parameter. The migration to Responses API (PR #70) made the field a no-op but kept it in place to avoid surprising existing config files mid-rollout.

**Impact.**
- Operators see an editable `api_version` field in `/admin/models` and may waste time tuning it.
- `config.toml.example` now carries a "currently unused" note, which itself is a smell.
- The loader still emits a default value, so the in-memory route dict carries dead data.

**Suggested fix.** One PR that touches three places together:
1. `app/core/config.py` — drop `_AZURE_OPTIONAL_KEYS`, `_AZURE_DEFAULT_API_VERSION`, and the two `api_version` assignments in `_build_config` and `apply_runtime_config`.
2. `app/templates/admin_models.html` — remove the `API Version` column, the `AZURE_DEFAULT_API_VERSION` JS constant, and the `api_version` field write-back.
3. `app/core/README.md` / `app/core/README.zh-TW.md` — drop the `api_version` mention from the `AZURE_MODELS` schema description.

Backward-compat consideration: existing `[azure_models.<alias>]` entries with `api_version = "..."` should still load (silently ignored is fine; loud rejection would break running configs).

---
