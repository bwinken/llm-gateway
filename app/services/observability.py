"""Langfuse observability integration.

Emits one Langfuse generation per billable request at the `_log_usage` seam.
See docs/langfuse-observability.md for the design.

This module imports cleanly WITHOUT the `langfuse` package installed — the SDK
is imported lazily inside the client accessor, and the pure helpers below never
touch it. That keeps the integration a true no-op when unconfigured and keeps
the request path free of Langfuse import cost.
"""

from __future__ import annotations

import contextvars
import os
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

# Per-request header context — set by the auth dependency (which has the
# Request), read at the `_log_usage` seam. A contextvar (not threading through
# every proxy function) keeps the wiring to a single touch-point, and it is
# visible at the seam because record_generation runs in the request's own
# async task, not a worker thread.
_request_meta: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "langfuse_request_meta", default={}
)


def set_request_meta(
    user_agent: str | None = None,
    x_app: str | None = None,
    session_id: str | None = None,
) -> None:
    _request_meta.set(
        {
            "user_agent": user_agent,
            "x_app": x_app,
            "session_id": session_id,
            "input_payload": None,
            # Monotonic clock at request entry (set by RequestMetaMiddleware, which
            # runs at the very start of every HTTP request in the request's own
            # task). Read at the _log_usage seam to compute request latency — see
            # request_latency_ms().
            "start_monotonic": time.monotonic(),
        }
    )


def request_latency_ms() -> float | None:
    """Milliseconds elapsed since the request entered the proxy, or None if the
    request-start marker is missing (e.g. a code path that bypassed the
    middleware). Read at the `_log_usage` seam to stamp the Langfuse
    generation's duration."""
    start = _request_meta.get().get("start_monotonic")
    if start is None:
        return None
    return (time.monotonic() - start) * 1000.0


def set_io_input(payload: Any) -> None:
    """Stash the request input (OpenAI chat `messages` shape) for Phase 2 I/O
    capture. Set by the forward_* functions (which have the translated body)
    only when capture is enabled; read at the `_log_usage` seam."""
    meta = _request_meta.get()
    # Mutate the current context's dict so the seam sees it without a re-set.
    if meta:
        meta["input_payload"] = payload
    else:
        _request_meta.set(
            {"user_agent": None, "x_app": None, "session_id": None, "input_payload": payload}
        )


def get_request_meta() -> dict:
    return _request_meta.get()


class RequestMetaMiddleware:
    """Pure-ASGI middleware that stashes request headers (User-Agent, x-app,
    x-session-id) into the request-meta contextvar for the observability hook.

    Must be pure ASGI (NOT BaseHTTPMiddleware) and NOT a sync FastAPI
    dependency: a contextvar written in a sync dependency runs in a threadpool
    copy and is lost, and BaseHTTPMiddleware breaks contextvar propagation into
    streaming bodies. A pure-ASGI middleware sets the contextvar in the
    request's own task, so it stays visible at the `_log_usage` seam for both
    non-stream and streaming responses (verified).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            headers: dict[str, str] = {}
            for k, v in scope.get("headers") or []:
                try:
                    headers[k.decode("latin-1").lower()] = v.decode("latin-1")
                except Exception:
                    pass
            set_request_meta(
                user_agent=headers.get("user-agent"),
                x_app=headers.get("x-app"),
                session_id=headers.get("x-session-id"),
            )
        await self.app(scope, receive, send)

# ---------------------------------------------------------------------------
# Pure helpers (no Langfuse dependency)
# ---------------------------------------------------------------------------

# OpenAI-style surfaces the gateway exposes that are NOT the Anthropic /v1/messages
# family. Used to label an unknown client as "openai-compatible".
_OPENAI_STYLE_PREFIXES = (
    "/v1/chat/completions",
    "/v1/responses",
    "/v1/embeddings",
    "/v1/rerank",
    "/v1/score",
    "/v1/completions",
)


def classify_client(
    user_agent: str | None,
    endpoint: str,
    x_app: str | None = None,
) -> str:
    """Best-effort label for the software making the request.

    Returns one of: ``claude-code``, ``roo-code``, ``anthropic-sdk``,
    ``openai-compatible``, ``other``.

    Priority: explicit Claude markers (``x-app: cli`` / UA) → Roo marker →
    endpoint family. Stays best-effort and is meant to be calibrated against
    real traffic once observed (raw User-Agent is also stored in metadata).
    """
    ua = (user_agent or "").lower()

    if (x_app or "").lower() == "cli" or "claude-cli" in ua or "claude-code" in ua:
        return "claude-code"
    if "roo" in ua:
        return "roo-code"

    # Unknown UA — fall back to the endpoint family.
    if endpoint.startswith("/v1/messages"):
        return "anthropic-sdk"
    if any(endpoint.startswith(p) for p in _OPENAI_STYLE_PREFIXES):
        return "openai-compatible"
    return "other"


def build_scores(
    *,
    empty_turn: bool,
    fallback_used: bool,
    client: str,
    output_tokens: int,
    is_error: bool = False,
) -> list[dict]:
    """Build the per-request Langfuse scores.

    Low-cardinality categoricals (`empty_turn`, `fallback_used`, `client`) go
    in as CATEGORICAL so the `scores-categorical` view can chart their
    distribution and trend; `output_tokens` is NUMERIC for the
    `scores-numeric` view. Booleans are emitted as the strings "true"/"false"
    (categorical values are strings).

    For a failed request (`is_error`) the per-response signals are meaningless,
    so only `client` and a `request_error=true` categorical are emitted — the
    latter lets you chart error rate per model / endpoint / user.
    """
    if is_error:
        return [
            {"name": "request_error", "value": "true", "data_type": "CATEGORICAL"},
            {"name": "client", "value": client, "data_type": "CATEGORICAL"},
        ]
    return [
        {"name": "empty_turn", "value": "true" if empty_turn else "false", "data_type": "CATEGORICAL"},
        {"name": "fallback_used", "value": "true" if fallback_used else "false", "data_type": "CATEGORICAL"},
        {"name": "client", "value": client, "data_type": "CATEGORICAL"},
        {"name": "output_tokens", "value": output_tokens, "data_type": "NUMERIC"},
    ]


# ---------------------------------------------------------------------------
# Generation record + Langfuse client (lazy, env-gated)
# ---------------------------------------------------------------------------

@dataclass
class GenerationRecord:
    """Everything needed to emit one Langfuse generation. Built at the
    `_log_usage` seam from primitives (no ORM object) so it is trivial to test.
    """
    username: str            # -> Langfuse userId (human-readable handle)
    user_id: str             # immutable anchor -> metadata.user_id
    endpoint: str            # -> generation `name` (groupable dimension)
    backend: str             # "vllm" | "azure"  -> tag
    model_alias: str         # user-facing alias -> `model`
    real_model: str          # downstream model/deployment -> metadata
    model_type: str          # llm | vlm | embedding | reranker -> tag
    usage: dict              # {"input": int, "output": int, "cache_read_input_tokens": int?}
    cost: dict               # {"input": float, "output": float, "total": float, ...}
    output_tokens: int       # numeric score
    empty_turn: bool = False
    fallback_reason: str | None = None
    model_parameters: dict | None = None
    req_shape: dict | None = None       # {msgs, asst, asst_thinking, asst_empty}
    latency_ms: float | None = None
    user_agent: str | None = None
    x_app: str | None = None
    session_id: str | None = None
    display_name: str | None = None
    error: str | None = None
    is_error: bool = False              # failed request → level=ERROR + request_error score
    input_payload: Any = None           # Phase 2 (LANGFUSE_CAPTURE_IO)
    output_payload: Any = None          # Phase 2


_client = None
_initialized = False


def _config_present() -> bool:
    return bool(
        os.getenv("LANGFUSE_HOST")
        and os.getenv("LANGFUSE_PUBLIC_KEY")
        and os.getenv("LANGFUSE_SECRET_KEY")
    )


def capture_io_enabled() -> bool:
    """Phase 2 global flag — whether to attach request/response content."""
    return os.getenv("LANGFUSE_CAPTURE_IO", "").strip().lower() in ("1", "true", "yes", "on")


def get_langfuse():
    """Lazily build the singleton Langfuse client. Returns None (no-op) when
    LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY are not all set, or if init fails.
    The langfuse package is imported only here, so an unconfigured gateway
    pays no Langfuse import cost on the request path."""
    global _client, _initialized
    if _initialized:
        return _client
    _initialized = True
    if not _config_present():
        return None
    try:
        from langfuse import Langfuse  # reads LANGFUSE_* from env
        _client = Langfuse()
        logger.info("Langfuse observability enabled (host={})", os.getenv("LANGFUSE_HOST"))
    except Exception as exc:  # never let init break the app
        logger.warning("Langfuse init failed, observability disabled: {}", exc)
        _client = None
    return _client


def flush_langfuse() -> None:
    """Flush pending events (call on app shutdown)."""
    if _client is not None:
        try:
            _client.flush()
        except Exception:
            pass


def reset_langfuse_cache() -> None:
    """Test helper — drop the cached client so env changes take effect."""
    global _client, _initialized
    _client = None
    _initialized = False


def record_generation(rec: GenerationRecord) -> None:
    """Fire-and-forget: emit one Langfuse generation for a completed request.

    No-op when Langfuse is unconfigured. NEVER raises into the request path —
    any SDK/serialisation error is logged and swallowed.
    """
    client = get_langfuse()
    if client is None:
        return
    try:
        from langfuse import propagate_attributes

        client_label = classify_client(rec.user_agent, rec.endpoint, rec.x_app)
        tags = [f"backend:{rec.backend}", f"type:{rec.model_type}"]

        metadata: dict[str, Any] = {
            "user_id": rec.user_id,
            "real_model": rec.real_model,
            "endpoint": rec.endpoint,
        }
        if rec.display_name:
            metadata["display_name"] = rec.display_name
        if rec.user_agent:
            metadata["user_agent"] = rec.user_agent
        if rec.fallback_reason:
            metadata["fallback_reason"] = rec.fallback_reason
        if rec.latency_ms is not None:
            metadata["latency_ms"] = rec.latency_ms
        if rec.req_shape:
            for k, v in rec.req_shape.items():
                metadata[f"req_{k}"] = v

        # user_id / session_id / tags / trace_name are trace-level → propagate
        # them to the generation span created within this context.
        with propagate_attributes(
            user_id=rec.username,
            session_id=rec.session_id,
            tags=tags,
            trace_name=rec.endpoint,
        ):
            gen = client.start_observation(
                name=rec.endpoint,
                as_type="generation",
                model=rec.model_alias,
                model_parameters=rec.model_parameters or None,
                usage_details=rec.usage,
                cost_details=rec.cost,
                metadata=metadata,
                level="ERROR" if rec.error else "DEFAULT",
                status_message=rec.error,
                input=rec.input_payload,
                output=rec.output_payload,
            )
            # Stamp the span's duration with the real request latency so
            # Langfuse's "Latency" column is correct. `start_observation` stamps
            # the span start at "now" — but this seam runs AFTER the request
            # finished, and v4's public API can't backdate a span's start, so we
            # extend the END by the measured latency instead. The span DURATION
            # (end - start) is therefore exact; only its absolute timestamp is
            # the request-completion instant rather than the request-start
            # instant. The raw number is also kept in metadata["latency_ms"].
            if rec.latency_ms is not None:
                gen.end(end_time=time.time_ns() + int(rec.latency_ms * 1_000_000))
            else:
                gen.end()
            for s in build_scores(
                empty_turn=rec.empty_turn,
                fallback_used=rec.fallback_reason is not None,
                client=client_label,
                output_tokens=rec.output_tokens,
                is_error=rec.is_error,
            ):
                gen.score(name=s["name"], value=s["value"], data_type=s["data_type"])
    except Exception as exc:  # never break the proxy
        logger.warning("Langfuse record_generation failed: {}", exc)
