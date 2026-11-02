"""Prompt guardrail: screen the newest user turn of local (vLLM) requests
with a guard model (before forwarding under "block", alongside the model
call under "monitor").

**Built for Qwen3Guard-Gen** (Qwen/Qwen3Guard-Gen-4B; the 0.6B / 8B
variants answer the same way). The guard is called through its OpenAI chat
endpoint with the text to judge as a single user message; vLLM applies the
model's own chat template, which carries Qwen's fixed safety policy. The
reply is two lines that ``parse_qwen3guard`` reads::

    Safety: Unsafe
    Categories: Violent

Switching to another guard model (Llama Guard, ShieldGemma, ...) means
rewriting ``build_guard_request`` and ``parse_qwen3guard``: each model has
its own prompt and verdict format. The admin Guardrail card says so too.

Contract (docs/architecture.md#guardrail-appservicesguardrailpy):

- Runs only when ``[guardrail]`` is enabled AND has a base URL, for the
  endpoints, accounts and users the policy selects.
- Never billed: the guard call goes through none of ``_log_usage``, and
  its token counts are only reported as Langfuse metadata.
- **Fails open**: a timeout, an HTTP / connection error or a reply that
  does not parse lets the request through, even with action = "block".
  The failure is recorded (Langfuse ``guardrail`` score = timeout / error /
  parse_error) and logged.
- Every verdict is stashed in the request-meta contextvar and lands on the
  request's own Langfuse generation (``guardrail_*`` metadata, a
  ``guardrail:<status>`` tag, ``guardrail`` / ``guardrail_action`` scores).
  A blocked request gets a generation of its own (level WARNING), since
  it never reaches ``_log_usage``.
- Action "monitor" (the default) never refuses anything: a verdict the
  policy would block is recorded as ``guardrail_action = flag``. Since it
  can't change the outcome, the guard runs **in the background**, alongside
  the vLLM call, and adds no latency: the request meta holds a
  ``PendingVerdict`` that the Langfuse emission (off the event loop) waits
  for. Action "block" waits for the verdict before forwarding.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.config import get_guardrail_settings
from app.core.logger import logger
from app.core.server_state import get_guardrail_client
from app.models.schema import User, is_app_account
from app.services.observability import (
    GenerationRecord,
    get_langfuse,
    get_request_meta,
    record_generation,
    request_latency_ms,
    set_guardrail_result,
)

GUARD_MODEL_FAMILY = "Qwen3Guard-Gen"

# Qwen3Guard's safety categories, spelled as the model prints them. Jailbreak
# is only reported for prompts.
QWEN3GUARD_CATEGORIES: tuple[str, ...] = (
    "Violent",
    "Non-violent Illegal Acts",
    "Sexual Content or Sexual Acts",
    "PII",
    "Suicide & Self-Harm",
    "Unethical Acts",
    "Politically Sensitive Topics",
    "Copyright Violation",
    "Jailbreak",
)

DEFAULT_CATEGORY_RULE = "block"

# Verdict statuses. The first three come from the model, the rest are the
# gateway's own: skipped (nothing to judge), and the fail-open outcomes.
STATUS_SAFE = "safe"
STATUS_CONTROVERSIAL = "controversial"
STATUS_UNSAFE = "unsafe"
STATUS_SKIPPED = "skipped"
STATUS_TIMEOUT = "timeout"
STATUS_ERROR = "error"
STATUS_PARSE_ERROR = "parse_error"

_SAFETY_RE = re.compile(r"^\s*Safety\s*:\s*(Safe|Unsafe|Controversial)\b", re.IGNORECASE | re.MULTILINE)
_CATEGORIES_RE = re.compile(r"^\s*Categories\s*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
# The model's reply is a few words; anything beyond this is not a verdict.
_MAX_CATEGORIES = 10
_MAX_CATEGORY_LEN = 64
_DETAIL_SNIPPET = 120


class GuardParseError(ValueError):
    pass


@dataclass
class GuardVerdict:
    status: str
    categories: list[str] = field(default_factory=list)
    action: str = "pass"            # pass | flag | block
    latency_ms: float | None = None
    detail: str | None = None       # error type, skip reason, unparsed snippet
    tokens: int | None = None       # guard model tokens (informational, never billed)

    def as_meta(self, settings: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": self.status,
            "action": self.action,
            "categories": list(self.categories),
            "latency_ms": self.latency_ms,
            "detail": self.detail,
            "tokens": self.tokens,
            "model": settings.get("model"),
            "policy": settings.get("action"),
        }


# ---------------------------------------------------------------------------
# Qwen3Guard-Gen protocol
# ---------------------------------------------------------------------------

def build_guard_request(model: str, text: str) -> dict[str, Any]:
    """Body for prompt moderation: the text as the only user message."""
    return {
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "max_tokens": 64,
        "temperature": 0,
        "stream": False,
    }


def parse_qwen3guard(content: Any) -> tuple[str, list[str]]:
    """``(status, categories)`` from a Qwen3Guard-Gen reply.

    ``status`` is safe / controversial / unsafe. ``Categories: None`` (or a
    missing line) is an empty list. Raises ``GuardParseError`` when there
    is no ``Safety:`` line.
    """
    if not isinstance(content, str):
        raise GuardParseError("reply content is not text")
    m = _SAFETY_RE.search(content)
    if not m:
        raise GuardParseError("no 'Safety:' line")
    status = m.group(1).lower()
    categories: list[str] = []
    c = _CATEGORIES_RE.search(content)
    if c:
        for part in c.group(1).split(","):
            name = part.strip().strip(".").strip()
            if not name or name.lower() == "none":
                continue
            name = name[:_MAX_CATEGORY_LEN]
            if name not in categories:
                categories.append(name)
            if len(categories) >= _MAX_CATEGORIES:
                break
    return status, categories


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

def category_rule(settings: dict[str, Any], category: str) -> str:
    return settings.get("category_rules", {}).get(category, DEFAULT_CATEGORY_RULE)


def decide_action(settings: dict[str, Any], status: str, categories: list[str]) -> str:
    """``pass`` / ``flag`` / ``block`` for a model verdict under the policy.

    - Below the threshold (safe; controversial with threshold "unsafe"): pass.
    - Otherwise the strictest rule among the reported categories wins
      (block > flag > ignore). A verdict without categories uses the
      default rule, "block".
    - "block" only refuses when the action is "block"; under "monitor"
      it is recorded as ``flag``.
    """
    over_threshold = status == STATUS_UNSAFE or (
        status == STATUS_CONTROVERSIAL and settings.get("block_threshold") == "controversial"
    )
    if not over_threshold:
        return "pass"
    rules = [category_rule(settings, c) for c in categories] or [DEFAULT_CATEGORY_RULE]
    if "block" in rules:
        return "block" if settings.get("action") == "block" else "flag"
    if "flag" in rules:
        return "flag"
    return "pass"


def applies(settings: dict[str, Any], user: User, kind: str) -> bool:
    """Whether the policy screens this user on this endpoint kind."""
    if not settings.get("active"):
        return False
    if kind not in settings.get("endpoints", ()):
        return False
    if user.username in settings.get("exempt_users", ()):
        return False
    scope = settings.get("apply_to", "all")
    if scope == "apps" and not is_app_account(user.username):
        return False
    if scope == "users" and is_app_account(user.username):
        return False
    return True


# ---------------------------------------------------------------------------
# What gets judged: the newest turn, if the user wrote it
# ---------------------------------------------------------------------------

_TEXT_PART_TYPES = ("text", "input_text")


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, dict) and part.get("type") in _TEXT_PART_TYPES:
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif isinstance(part, str):
            parts.append(part)
    return "\n".join(parts)


def extract_user_text(body: Any, kind: str) -> str:
    """Text of the request's newest turn when that turn is the user's.

    Only text the user wrote is judged: a turn that ends in an assistant
    prefill, a tool result (Claude Code's agent loop) or images only gives
    "", and the check is skipped. Earlier turns were judged when they were
    new. Tool results, images and system prompts are never sent.
    """
    if not isinstance(body, dict):
        return ""
    if kind == "responses":
        items = body.get("input")
        if isinstance(items, str):
            return items
        if not isinstance(items, list) or not items:
            return ""
        last = items[-1]
        if not isinstance(last, dict) or last.get("role") != "user":
            return ""
        if last.get("type") not in (None, "message"):
            return ""
        return _content_text(last.get("content"))
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return ""
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        return ""
    return _content_text(last.get("content"))


def clip(text: str, max_chars: int) -> str:
    """Keep the head and the tail of an over-long text (an instruction can
    sit at either end)."""
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return text[:head] + "\n...\n" + text[-tail:]


# ---------------------------------------------------------------------------
# The guard call
# ---------------------------------------------------------------------------

async def evaluate(settings: dict[str, Any], text: str) -> GuardVerdict:
    """Ask the guard model about ``text``. Never raises: timeouts, HTTP and
    parse failures come back as a verdict with action ``pass``."""
    headers = {"Content-Type": "application/json"}
    if settings.get("api_key"):
        headers["Authorization"] = f"Bearer {settings['api_key']}"
    timeout_s = float(settings["timeout_s"])
    t0 = time.monotonic()

    def elapsed() -> float:
        return round((time.monotonic() - t0) * 1000.0, 1)

    try:
        resp = await asyncio.wait_for(
            get_guardrail_client().post(
                f"{settings['base_url']}/chat/completions",
                json=build_guard_request(settings["model"], text),
                headers=headers,
                timeout=timeout_s,
            ),
            timeout=timeout_s,
        )
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return GuardVerdict(STATUS_TIMEOUT, latency_ms=elapsed(), detail=f"no verdict within {timeout_s:g}s")
    except Exception as exc:
        return GuardVerdict(STATUS_ERROR, latency_ms=elapsed(), detail=type(exc).__name__)
    latency = elapsed()
    if resp.status_code != 200:
        return GuardVerdict(STATUS_ERROR, latency_ms=latency, detail=f"HTTP {resp.status_code}")
    content: Any = None
    tokens = None
    try:
        data = resp.json()
        usage = data.get("usage") or {}
        if isinstance(usage.get("total_tokens"), int):
            tokens = usage["total_tokens"]
        content = data["choices"][0]["message"]["content"]
        status, categories = parse_qwen3guard(content)
    except Exception as exc:
        snippet = content if isinstance(content, str) else type(exc).__name__
        return GuardVerdict(
            STATUS_PARSE_ERROR, latency_ms=latency, tokens=tokens,
            detail=str(snippet)[:_DETAIL_SNIPPET],
        )
    return GuardVerdict(
        status, categories=categories, latency_ms=latency, tokens=tokens,
        action=decide_action(settings, status, categories),
    )


# ---------------------------------------------------------------------------
# Request-path entry point
# ---------------------------------------------------------------------------

STATUS_PENDING = "pending"


class PendingVerdict:
    """A monitor-mode verdict still being computed by a background task.

    Stashed in the request meta in place of the verdict dict. Nothing ever
    waits on it: ``_emit_observation`` defers the request's Langfuse
    generation with ``add_done_callback`` until the verdict is in (the
    guard task is bounded by ``timeout_s`` and always settles), and a
    synchronous reader (``_log_error``) that finds it unsettled records
    ``pending``.
    """

    def __init__(self, settings: dict[str, Any]):
        self._future: concurrent.futures.Future = concurrent.futures.Future()
        self._fallback = {"model": settings.get("model"), "policy": settings.get("action"),
                          "action": "pass", "categories": []}

    def set(self, meta: dict[str, Any]) -> None:
        if not self._future.done():
            self._future.set_result(meta)

    def done(self) -> bool:
        return self._future.done()

    def add_done_callback(self, fn) -> None:
        """``fn(verdict_dict)`` once the verdict is in (now, if it is).
        Runs on the thread that settles it: the event loop's."""
        self._future.add_done_callback(lambda f: fn(f.result()))

    def resolve(self) -> dict[str, Any]:
        if self._future.done():
            return self._future.result()
        return dict(self._fallback, status=STATUS_PENDING, detail="request ended before the verdict")


# Strong references to the running monitor tasks: the event loop only keeps
# weak ones, and a collected task would never deliver its verdict.
_monitor_tasks: set[asyncio.Task] = set()


def _log_verdict(verdict: GuardVerdict, username: str, model: str, endpoint: str) -> None:
    if verdict.status in (STATUS_TIMEOUT, STATUS_ERROR, STATUS_PARSE_ERROR):
        logger.warning(
            "Guardrail unavailable, allowing request | user={} model={} endpoint={} "
            "status={} detail={} latency_ms={}",
            username, model, endpoint, verdict.status,
            verdict.detail if verdict.status != STATUS_PARSE_ERROR else "unparsable reply",
            verdict.latency_ms,
        )
    elif verdict.action == "flag":
        logger.info(
            "Guardrail flagged | user={} model={} endpoint={} status={} categories={}",
            username, model, endpoint, verdict.status, ",".join(verdict.categories) or "-",
        )


async def _monitor(
    settings: dict[str, Any], text: str, pending: PendingVerdict,
    username: str, model: str, endpoint: str,
) -> None:
    """Background guard call for action "monitor". Always settles ``pending``."""
    meta: dict[str, Any] | None = None
    try:
        verdict = await evaluate(settings, text)
        meta = verdict.as_meta(settings)
        _log_verdict(verdict, username, model, endpoint)
    except BaseException as exc:  # incl. cancellation at shutdown
        meta = GuardVerdict(STATUS_ERROR, detail=type(exc).__name__).as_meta(settings)
        if not isinstance(exc, Exception):
            raise
    finally:
        pending.set(meta or GuardVerdict(STATUS_ERROR).as_meta(settings))


def _start_monitor(settings: dict[str, Any], text: str, username: str, model: str, endpoint: str) -> None:
    pending = PendingVerdict(settings)
    set_guardrail_result(pending)  # type: ignore[arg-type]
    task = asyncio.create_task(_monitor(settings, text, pending, username, model, endpoint))
    _monitor_tasks.add(task)
    task.add_done_callback(_monitor_tasks.discard)

def _blocked_response(kind: str, message: str) -> JSONResponse:
    if kind == "messages":
        body: dict[str, Any] = {
            "type": "error",
            "error": {"type": "invalid_request_error", "message": message},
        }
    else:
        body = {
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": None,
                "code": "content_policy_violation",
            }
        }
    return JSONResponse(status_code=400, content=body, headers={"X-Guardrail": "blocked"})


def _record_blocked(
    user: User, endpoint: str, backend: str, model: str, verdict_meta: dict, reason: str,
) -> None:
    """A Langfuse generation for a refused request (it never reaches
    ``_log_usage``). Zero usage and cost: nothing was generated or billed."""
    if get_langfuse() is None:
        return
    meta = get_request_meta()
    record_generation(GenerationRecord(
        username=user.username,
        user_id=str(user.id),
        endpoint=endpoint,
        backend=backend,
        model_alias=model or "unknown",
        real_model=model or "unknown",
        model_type="llm",
        usage={"input": 0, "output": 0, "total": 0},
        cost={"input": 0.0, "output": 0.0, "total": 0.0},
        output_tokens=0,
        error=reason,
        blocked=True,
        latency_ms=request_latency_ms(),
        user_agent=meta.get("user_agent"),
        x_app=meta.get("x_app"),
        session_id=meta.get("session_id"),
        display_name=getattr(user, "display_name", None),
        guardrail=verdict_meta,
    ))


async def screen(
    request: Request, user: User, kind: str, endpoint: str, backend: str,
) -> JSONResponse | None:
    """Run the guardrail for one request. Returns the 400 to send when the
    request is blocked, else None (forward as usual). Never raises."""
    try:
        settings = get_guardrail_settings()
        if not applies(settings, user, kind):
            return None
        try:
            body = await request.json()
        except Exception:
            return None  # the forwarder answers a bad body itself
        model = body.get("model") if isinstance(body, dict) else None
        model = model if isinstance(model, str) else ""
        text = extract_user_text(body, kind).strip()
        if not text:
            set_guardrail_result(GuardVerdict(STATUS_SKIPPED, detail="no new user text").as_meta(settings))
            return None
        text = clip(text, settings["max_input_chars"])
        if settings["action"] != "block":
            # Monitor can't change the outcome: judge alongside the vLLM call.
            _start_monitor(settings, text, user.username, model, endpoint)
            return None

        verdict = await evaluate(settings, text)
        meta = verdict.as_meta(settings)
        set_guardrail_result(meta)
        _log_verdict(verdict, user.username, model, endpoint)

        if verdict.action != "block":
            return None
        reason = f"guardrail blocked: {verdict.status}" + (
            f" ({', '.join(verdict.categories)})" if verdict.categories else ""
        )
        logger.warning(
            "Guardrail blocked | user={} model={} endpoint={} status={} categories={}",
            user.username, model, endpoint, verdict.status, ",".join(verdict.categories) or "-",
        )
        try:
            _record_blocked(user, endpoint, backend, model, meta, reason)
        except Exception as exc:
            logger.warning("Guardrail Langfuse record failed: {}", exc)
        return _blocked_response(kind, settings["block_message"])
    except Exception as exc:  # the guardrail must never break a request
        logger.warning(
            "Guardrail check failed, allowing request | user={} endpoint={} error={}: {}",
            getattr(user, "username", "?"), endpoint, type(exc).__name__, exc,
        )
        return None


async def test_guard(settings: dict[str, Any], text: str) -> dict[str, Any]:
    """Admin "Test connection": judge ``text`` with ``settings`` (enabled or
    not) and report what the request path would do."""
    verdict = await evaluate(settings, clip(text, settings["max_input_chars"]))
    ok = verdict.status not in (STATUS_TIMEOUT, STATUS_ERROR, STATUS_PARSE_ERROR)
    return {
        "ok": ok,
        "status": verdict.status,
        "categories": verdict.categories,
        "action": verdict.action,
        "latency_ms": verdict.latency_ms,
        "detail": verdict.detail,
        "tokens": verdict.tokens,
    }
