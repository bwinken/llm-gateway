# Spec: Langfuse Observability Integration

> Status: **DRAFT — awaiting approval**. Spec-driven workflow: SPECIFY → PLAN → TASKS → IMPLEMENT. Do not start IMPLEMENT until this is approved.

## Objective

Add LLM observability/tracing to the gateway by emitting a Langfuse **generation**
per billable request, so operators can analyse cost and usage (per user, per model,
over time) and trend quality signals (empty-turn rate, fallback rate) in Langfuse's
UI / Metrics API instead of grepping logs.

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
| Score encoding | Boolean signals (`empty_turn`, `fallback_used`) → categorical (`"true"`/`"false"`); `output_tokens` → numeric |
| Timing | Phase 1 captures **request latency** (monotonic clock at the proxy); **TTFT deferred** (not now) |

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
    endpoint: str            # "/v1/messages"
    backend: str             # "vllm" | "azure"
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
| `user.username` | `userId` | built-in per-user views; filterable (not a group-by dim) |
| `x-session-id` header (if present) | `sessionId` | conversation drill-down |
| model **alias** | `model` (`providedModelName`) | primary group-by dimension for cost/usage |
| `real_model` / deployment | `metadata.real_model` | reference only |
| input/output/cached tokens | **`usageDetails`** map | native aggregatable measure |
| gateway `_calc_cost` breakdown | **`costDetails`** map (manual → overrides Langfuse pricing) | accurate, matches billing |
| temperature/top_p/max_tokens/reasoning_effort/stream | `modelParameters` | params view |
| `empty_turn` (out≤1), `fallback_used` | **Scores** (`scores-categorical`) | designed for trend/aggregation |
| `output_tokens` (for trending) | Score (`scores-numeric`) or usageDetails | — |
| backend, model_type | `tags` (low cardinality, ≤4) | quick filtering only |
| endpoint, fallback_reason, `req_shape` (asst_thinking/asst_empty), latency_ms, org_code | `metadata` | filterable/displayable; not relied on for group-by (ttft_ms deferred) |
| downstream error / truncation / empty-turn | `level` + `statusMessage` | error surfacing |

**Hard rules:**
- `costDetails` is passed **manually** from the gateway's `_calc_cost` (per-model
  override + prompt-cache discount). Langfuse must NOT re-price.
- Tags stay low-cardinality. No per-request / numeric values in tags.
- Base64 image data is stripped from `input_payload` before sending.
- Never send downstream API keys / `Authorization` / `api-key` headers.

## Testing Strategy

- **Framework:** pytest, in-memory SQLite (existing `tests/conftest.py` fixtures).
  **No network to Langfuse** — the Langfuse client is mocked/patched (mirrors how
  `get_client` / `get_azure_client` are patched in `_patch_all`).
- **TDD** (red → green) for every new function, per repo convention.
- **What to test:**
  - `build_generation_kwargs()` — correct field routing (model=alias, costDetails from
    gateway, scores for empty_turn/fallback, tags low-cardinality, image redaction).
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
  - Strip base64 image data from captured I/O.
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
  - Send raw base64 image bytes.
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

## Plan (phases)

- **Phase 1 — metrics only (no PII).** `observability.py` + `record_generation()` wired
  at every `_log_usage` seam (vLLM ×8, Azure ×6). Sends usage, costDetails, model alias,
  userId, tags, scores (empty_turn/fallback), metadata, modelParameters, level/status.
  env-gated; `LANGFUSE_CAPTURE_IO` not yet used. Zero impact when unconfigured.
- **Phase 2 — full I/O.** Behind `LANGFUSE_CAPTURE_IO`. Capture request payload
  (`monitor_body` / `anthropic_body`) + response: non-stream from the response dict,
  stream by accumulating chunks in the proxy loop (the few lines `monitor.py` does today,
  but gated on `LANGFUSE_CAPTURE_IO` instead of `_monitoring`). Redact base64 images;
  keep reasoning/thinking as a separate field.
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

## Operational Notes (not gateway code)

- **Data retention.** How long traces/generations live is configured on the **Langfuse
  server** (project-level retention + its Postgres/ClickHouse storage), not in the
  gateway — the gateway only emits data. Flag to ops to set a retention period,
  **especially once Phase 2 I/O capture is on** (full prompts = larger storage + PII
  living longer).

## Open Questions

None blocking — all design decisions above are resolved.
