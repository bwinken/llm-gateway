# Spec: Langfuse Observability Integration

> Status: **DRAFT — awaiting approval**. Spec-driven workflow: SPECIFY → PLAN → TASKS → IMPLEMENT. Do not start IMPLEMENT until this is approved.

## Objective

Add LLM observability/tracing to the gateway by emitting a Langfuse **generation**
per billable request, so operators can analyse cost and usage (per user, per model,
over time) and trend quality signals (empty-turn rate, fallback rate) in Langfuse's
UI / Metrics API instead of grepping logs.

**Primary goal: per-user usage observation** — understand *how each person uses the
gateway*. This splits into two halves:
- **Quantitative** ("how much / what") — which models & clients each user uses, request
  volume, tokens, cost over time, reasoning usage, empty-turn/fallback rate. Delivered
  by **Phase 1** (no PII).
- **Qualitative** ("how / what they ask") — actual prompts, task types, interaction
  patterns. Requires **Phase 2** I/O capture.

The integration is **decoupled from `monitor.py`** and is intended to **replace it**:
once Langfuse captures the same request/response payloads, `monitor.py`, its admin
endpoints, and the on-disk JSONL under `monitor/` are removed.

- **Users:** gateway operators / admins (not API end-users).
- **Why:** today usage lives only in `usage_logs` (billing) + ad-hoc loguru lines +
  per-user JSONL. There is no searchable, aggregatable observability surface.
- **Success looks like:** every billable request appears in self-hosted Langfuse with
  accurate token usage and cost (computed by the gateway, not re-priced by Langfuse);
  dashboards for per-user / per-model cost and empty-turn / fallback rate work; the
  integration is a no-op when unconfigured; `monitor.py` is removed.

### Decisions (resolved)

| Decision | Choice |
|---|---|
| Deployment | **Self-hosted** Langfuse (payloads stay on internal network) |
| Phase 2 I/O capture gating | **Global env flag** (`LANGFUSE_CAPTURE_IO`) |
| Conversation/session grouping | **Per-request independent trace** (model A). `sessionId` defaults to null; only filled opportunistically from `x-session-id`. No state introduced, no per-request field changed by it. |
| `monitor.py` | **Removed** by this spec (Phase 3), after Langfuse I/O is verified |
| Phase 2 I/O shape | Send the gateway's **internal OpenAI chat `messages` shape** as `input` (incl. for `/v1/messages` — the Anthropic request is translated via `anthropic_to_openai_request` before sending). Langfuse renders OpenAI chat format as a conversation view natively. |
| Phase 2 images | **Option B** — pass image content through and let **Langfuse v3 media handling** store/render it (base64 auto-extracted to blob storage; trace stays light, image viewable). Not the `[image]` placeholder. |
| Score encoding | Boolean signals (`empty_turn`, `fallback_used`) → categorical (`"true"`/`"false"`); `output_tokens` → numeric |
| Timing | Phase 1 captures **request latency** (monotonic clock at the proxy); **TTFT deferred** (not now) |
| User identity | `userId` = `user.username` (stable login handle, human-readable in the Users view). `user.id` also stored in `metadata.user_id` as the immutable anchor; `display_name` (IdP-synced, mutable) stays in metadata, never the identity key. If usernames ever become reassignable, switch `userId` to `user.id`. |
| Per-user analysis mechanism | Langfuse **Users view** + **filter-by-userId**, NOT custom `group by user` (userId is high-cardinality → not a Metrics-API group-by dimension). Once filtered to a user, break down by `name` (endpoint), `model`, or the `client` Score; filter further by `backend`/`type` tags. |

## Tech Stack

- **Langfuse Python SDK** (v3.x) — async, batches + flushes in background. Added via `uv add langfuse`.
- Self-hosted Langfuse server (operator-provided, not part of this repo).
- Existing: FastAPI, httpx (shared async clients), loguru, SQLModel, uv.
- **No** OpenTelemetry, **no** schema change (`usage_logs` unchanged).

## Commands

```bash
uv sync                                   # install deps (incl. langfuse after `uv add langfuse`)
uv add langfuse                           # add the SDK
uv run fastapi dev app/main.py            # dev with auto-reload
uv run pytest tests/test_observability.py -v   # the new test module
uv run pytest tests/ -v                   # full suite (must stay green)
```

## Project Structure

```
app/services/observability.py   → NEW. Langfuse client lifecycle (init in lifespan,
                                   flush on shutdown) + record_generation() helper +
                                   pure mapping/redaction functions.
app/services/vllm_proxy.py       → call observability.record_generation() at the
                                   existing _log_usage seam; gate stream I/O
                                   accumulation on LANGFUSE_CAPTURE_IO.
app/services/azure_proxy.py      → same, at its _log_usage seams.
app/core/config.py + .env        → LANGFUSE_* env parsing.
app/core/server_state.py         → hold the Langfuse client handle (like get_client()).
tests/test_observability.py      → NEW. Unit tests for the pure functions + record()
                                   no-op-when-disabled + proxy call-site wiring.

# Removed in Phase 3:
app/services/monitor.py          → DELETE
app/routers/admin.py             → remove the /admin/monitor + /admin/users/{id}/monitor endpoints
monitor/                         → no longer written
```

## Code Style

Match the patterns already in the repo (env via `os.getenv` with defaults, loguru,
fire-and-forget background work, **pure functions tested in isolation** — exactly like
the `empty_turn_warning` / `summarize_request_shape` helpers in `anthropic_adapter.py`).

```python
# app/services/observability.py  (illustrative)

@dataclass
class GenerationRecord:
    user: User
    endpoint: str            # "/v1/messages"  -> Langfuse generation `name` (groupable)
    backend: str             # "vllm" | "azure"
    user_agent: str | None   # raw User-Agent header -> metadata (calibration only)
    client: str              # derived: claude-code|roo-code|openai-compatible|other -> categorical Score
    model_alias: str         # user-facing alias  -> Langfuse `model`
    real_model: str          # downstream model/deployment -> metadata
    model_type: str          # llm | vlm | embedding | reranker
    usage: dict              # {"input": int, "output": int, "cache_read_input_tokens": int}
    cost: dict               # {"input": Decimal, "output": Decimal, "cache_read_input_tokens": Decimal, "total": Decimal}
    model_parameters: dict   # {temperature, top_p, max_tokens, reasoning_effort, stream}
    fallback_reason: str | None
    req_shape: dict          # {msgs, asst, asst_thinking, asst_empty}
    empty_turn: bool         # output_tokens <= 1 on a clean finish
    latency_ms: float | None  # Phase 1 — monotonic clock at the proxy
    ttft_ms: float | None = None  # DEFERRED — not captured in Phase 1
    error: str | None        # statusMessage when downstream/translation failed
    session_id: str | None   # from x-session-id header, else None
    input_payload: Any = None    # Phase 2 only (LANGFUSE_CAPTURE_IO)
    output_payload: Any = None   # Phase 2 only


def record_generation(rec: GenerationRecord) -> None:
    """Fire-and-forget. No-op when Langfuse is unconfigured. Never raises into
    the request path — all errors are swallowed + logged."""
    client = get_langfuse()
    if client is None:
        return
    try:
        client.generation(**build_generation_kwargs(rec))   # build_* is pure + tested
    except Exception as exc:                                  # never break the proxy
        logger.warning("Langfuse record failed: {}", exc)


def build_generation_kwargs(rec: GenerationRecord) -> dict:
    """PURE. Maps a GenerationRecord to Langfuse SDK kwargs. Unit-tested."""
    ...

def redact_payload(messages: Any) -> Any:
    """PURE. Replace base64 image data URLs with '[image]' so traces stay light."""
    ...
```

## Data Model Mapping (Langfuse)

One **trace + one nested generation per request** (the gateway proxies a single LLM
call per request). Every request is fully self-contained — all fields live on its own
trace/generation. `sessionId` is a **non-destructive grouping label only**: it never
merges requests, moves fields up, or requires cross-request consistency, so it costs no
per-request fidelity. We deliberately do **not** model a conversation as one long-lived
trace (would centralise fields + need cross-request state, and the gateway is stateless
with no reliable conversation id anyway). Route each dimension to the Langfuse primitive
that can actually analyse it — confirmed against Langfuse Metrics API v2:

| Gateway value | Langfuse field | Why |
|---|---|---|
| `user.username` | `userId` | identity for the built-in Users view; filterable (not a group-by dim). See User-identity decision. |
| `user.id` (immutable) | `metadata.user_id` | stable anchor to survive any username change |
| `display_name` (IdP-synced) | `metadata.display_name` | readable label; mutable — never the identity key |
| `x-session-id` header (if present) | `sessionId` | conversation drill-down |
| model **alias** | `model` (`providedModelName`) | primary group-by dimension for cost/usage |
| **endpoint** | generation **`name`** | **groupable** dimension → per-endpoint (per-surface) statistics |
| **client** (derived: claude-code / roo-code / openai-compatible / other) | **categorical Score `client`** | **statisticable** — `scores-categorical` view gives request distribution per software + day/month trend |
| raw `User-Agent` header | `metadata.user_agent` | calibration / lookup only — **NOT** for statistics |
| `real_model` / deployment | `metadata.real_model` | reference only |
| input/output/cached tokens | **`usageDetails`** map | native aggregatable measure |
| gateway `_calc_cost` breakdown | **`costDetails`** map (manual → overrides Langfuse pricing) | accurate, matches billing |
| temperature/top_p/max_tokens/reasoning_effort/stream | `modelParameters` | params view |
| `empty_turn` (out≤1), `fallback_used` | **Scores** (`scores-categorical`) | designed for trend/aggregation |
| `output_tokens` (for trending) | Score (`scores-numeric`) or usageDetails | — |
| backend, model_type | **`tags`** (prefixed, low cardinality) | **filtering** only — e.g. `backend:vllm`, `type:llm` |
| fallback_reason, `req_shape` (asst_thinking/asst_empty), latency_ms, org_code | `metadata` | filterable/displayable; not relied on for group-by (ttft_ms deferred) |
| downstream error / truncation / empty-turn | `level` + `statusMessage` | error surfacing |

> **Rule of thumb — where a field goes depends on how you'll use it:**
> - Want to **group-by / make a breakdown chart**? → a groupable dimension
>   (`name`=endpoint, `model`=alias) or a **categorical Score** (`client`, `empty_turn`, …).
> - Want to **filter / segment** by it? → a **tag** (prefixed, low-cardinality).
> - Only need to **look it up / display** it? → **metadata** (NOT statisticable).
>
> **Tag convention:** tags are a flat string list, so prefix by category
> (`backend:`, `type:`) to keep filtering unambiguous. No per-request / numeric values in tags.

**Hard rules:**
- `costDetails` is passed **manually** from the gateway's `_calc_cost` (per-model
  override + prompt-cache discount). Langfuse must NOT re-price.
- Tags stay low-cardinality. No per-request / numeric values in tags.
- Images go via Langfuse v3 media handling (Option B), not stripped — base64 is
  auto-extracted to blob storage so the trace stays light.
- Never send downstream API keys / `Authorization` / `api-key` headers.

## Testing Strategy

- **Framework:** pytest, in-memory SQLite (existing `tests/conftest.py` fixtures).
  **No network to Langfuse** — the Langfuse client is mocked/patched (mirrors how
  `get_client` / `get_azure_client` are patched in `_patch_all`).
- **TDD** (red → green) for every new function, per repo convention.
- **What to test:**
  - `build_generation_kwargs()` — correct field routing (model=alias, name=endpoint,
    costDetails from gateway, scores for empty_turn/fallback/client, tags low-cardinality,
    image redaction).
  - `classify_client()` — `claude-code` from UA `claude-cli`/`claude-code` or `x-app:cli`;
    `roo-code` from a Roo UA; `openai-compatible` for OpenAI-style endpoints with a generic
    SDK UA; `other` otherwise. Best-effort; calibrated against real traffic later.
  - `redact_payload()` — base64 data URL → `[image]`; text untouched.
  - `record_generation()` — **no-op when unconfigured** (no client → returns, no raise);
    swallows client errors without propagating.
  - cost-breakdown helper — matches `_calc_cost` total (input+output+cached == total).
  - Proxy wiring — `record_generation` called once per billable request with expected
    `model_alias` / `usage` / `empty_turn`, on both vLLM and Azure paths.
- Coverage expectation: every new function has a test; full suite stays green.

## Boundaries

- **Always:**
  - No-op when `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` unset.
  - Use gateway `_calc_cost` for `costDetails` (manual, overrides Langfuse pricing).
  - Send I/O as the internal OpenAI chat `messages` shape (Phase 2) so Langfuse renders a conversation view.
  - Run the full test suite before commit; keep it green.
  - Best-effort & non-blocking: Langfuse failures are logged and swallowed, never
    surfaced into the request/response path.
- **Ask first:**
  - Adding the `langfuse` dependency (`uv add langfuse`).
  - Removing `monitor.py` + admin monitor endpoints (Phase 3) — confirm Langfuse I/O
    verified in staging first.
  - Any new env var beyond the LANGFUSE_* set below.
- **Never:**
  - Send downstream credentials/auth headers to Langfuse.
  - Let a Langfuse error break or slow the proxy.
  - Re-price cost inside Langfuse.
  - Capture I/O when `LANGFUSE_CAPTURE_IO` is off.

### Environment variables

```
LANGFUSE_HOST          # e.g. http://langfuse.internal:3000  (self-hosted)
LANGFUSE_PUBLIC_KEY    # pk-...
LANGFUSE_SECRET_KEY    # sk-...
LANGFUSE_CAPTURE_IO    # bool, default false — Phase 2 global I/O capture flag
# integration is ENABLED only when HOST + PUBLIC + SECRET are all set
```

## Phase 2 — endpoint coverage

| Endpoint | Metrics (Phase 1) | I/O content (Phase 2) |
|---|---|---|
| `/v1/chat/completions`, `/azure/v1/chat/completions` | ✅ | ✅ input (OpenAI messages) + output (stream + non-stream) |
| `/v1/messages`, `/azure/v1/messages` | ✅ | ✅ input (translated OpenAI messages) + output |
| `/v1/responses`, `/azure/v1/responses` | ✅ | ✅ input + output, non-stream + stream (native Responses shape — Langfuse renders it; stream output read from `response.output_text.delta` events) |
| **`/v1/embeddings` / `/v1/rerank` / `/v1/score`** | ✅ | ❌ **metrics only — never store I/O.** Embedding output is a large vector (noise + storage); input is bulk text (PII). `rerank` query-only capture is a possible future option, never docs/scores/vectors. |

Status: Phase 2 I/O capture wired for all conversational paths on both backends (vLLM + Azure), stream + non-stream. All gated on `LANGFUSE_CAPTURE_IO` (default off).

## Plan (phases)

- **Phase 1 — metrics only (no PII).** `observability.py` + `record_generation()` wired
  at every `_log_usage` seam (vLLM ×8, Azure ×6). Sends usage, costDetails, model alias,
  userId, generation `name`=endpoint, scores (empty_turn / fallback / **client**), tags
  (backend/type), metadata (incl. raw `user_agent`, `user_id` anchor), modelParameters,
  level/status. Includes the pure `classify_client(user_agent, endpoint)` helper.
  env-gated; `LANGFUSE_CAPTURE_IO` not yet used. Zero impact when unconfigured.
- **Phase 2 — full I/O.** Behind `LANGFUSE_CAPTURE_IO`. Capture input as the internal
  **OpenAI chat `messages` shape** (for `/v1/messages`, the already-translated
  `openai_body["messages"]`) so Langfuse renders a conversation view; capture the
  response (non-stream from the response dict; stream by accumulating chunks in the proxy
  loop, gated on `LANGFUSE_CAPTURE_IO` instead of `_monitoring`). Images via **Langfuse
  media handling** (Option B). Keep reasoning/thinking as a separate field.
- **Phase 3 — remove monitor.** Delete `app/services/monitor.py`, the `/admin/monitor`
  and `/admin/users/{id}/monitor` endpoints, the monitor toggle in the admin UI, and the
  `monitor/` writes. Update README/CLAUDE.md.

## Success Criteria

1. With LANGFUSE_* unset: behaviour byte-identical to today; full suite green; no Langfuse import cost on the request path.
2. With LANGFUSE_* set: each billable request (chat / embeddings / rerank / responses / messages, vLLM + Azure) produces exactly one Langfuse generation.
3. `costDetails.total` for a request equals the gateway's `_calc_cost` value (incl. prompt-cache discount on Azure).
4. `usageDetails` carries input/output and cached tokens where available.
5. Langfuse Metrics API can group `sum(totalCost)` by model alias and by day.
6. An `empty_turn` score is attached when `output_tokens <= 1` on a clean finish, queryable as an empty-turn rate per model.
7. A Langfuse error (e.g. server down) never fails or delays a proxied request.
8. Phase 2: with `LANGFUSE_CAPTURE_IO=true`, prompt+completion appear on the generation; base64 images are shown as `[image]`; no auth headers leak.
9. Phase 3: `monitor.py` and its endpoints are gone; no references remain; suite green.

### Per-user observation (the primary goal — must be answerable)

Phase 1 (quantitative), via the Users view + filter-by-userId:
10. The Users view lists every active user with their total cost, token usage, and request count over a time range.
11. Filtering to one user and grouping by `model` shows that user's model mix; grouping by `name` (endpoint) and by the `client` Score shows which software/surface they use (Claude Code vs Roo Code vs embeddings…).
12. Per user, request volume / tokens / cost can be charted over time (day/month granularity), and their empty-turn / fallback rate is queryable.
13. The `client` categorical Score yields a request-distribution-per-software chart (Claude Code / Roo Code / openai-compatible / other), trendable by day/month, across all users or filtered to one.
14. A username change does not fragment history irrecoverably — `metadata.user_id` still ties records to the same person.

Phase 2 (qualitative):
15. For a chosen user, recent prompts/completions are inspectable to see *how* they actually use the gateway (task types, tool use), subject to the governance controls below.

## Governance (individual-level observation)

The primary goal is observing **how individual users behave**, and Phase 2 captures
their actual prompts. That is individual-level monitoring and must be governed, not just
technically de-risked:

- **Access control** — restrict the Langfuse project to the operators who legitimately
  need per-user data; do not make per-user content broadly viewable.
- **Notice/consent** — confirm with the org whether users must be informed that their
  usage (and, in Phase 2, prompt content) is recorded and attributable to them. Resolve
  this **before** enabling `LANGFUSE_CAPTURE_IO` in production.
- **Scope creep** — Phase 1 (metrics) is low-sensitivity; Phase 2 (content) is high.
  Treat enabling Phase 2 as a policy decision, not just a config flag.

## Operational Notes (not gateway code)

- **Data retention.** How long traces/generations live is configured on the **Langfuse
  server** (project-level retention + its Postgres/ClickHouse storage), not in the
  gateway — the gateway only emits data. Flag to ops to set a retention period,
  **especially once Phase 2 I/O capture is on** (full prompts = larger storage + PII
  living longer).

## Open Questions

None blocking — all design decisions above are resolved.
