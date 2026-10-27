"""
Microsoft Teams alerts — opt-in: set ``TEAMS_WEBHOOK_URL`` in ``.env``.

Unset (the default) every function here is a no-op: nothing is posted and
the ``alert_state`` table is never touched.

Set, three kinds of event are posted to that channel as Adaptive Cards:

  health   an on-prem server went DOWN, or came back UP (health loop)
  budget   an account reached 80% / 100% of its daily limit (auth check)
  anomaly  the hourly anomaly scan created new findings (scan script)

``TEAMS_ALERTS`` (comma-separated, default all three) picks a subset.

The URL is a Teams Workflows "When a Teams webhook request is received"
trigger (or a legacy incoming webhook); both accept the
``{"type": "message", "attachments": [adaptive card]}`` body built here.
The request goes through the standard ``HTTPS_PROXY`` env when one is set.

Every worker runs its own health loop and auth checks, so one event is seen
several times — by each worker, on each host. ``alert_state`` makes it one
post: the worker whose DB write wins sends, the others see the event as
already announced. Posting runs on a small thread pool: an alert never
delays a request, and a failed post is logged, never raised.
"""

from __future__ import annotations

import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.config import APP_TITLE
from app.core.database import engine
from app.core.logger import logger
from app.core.timeutil import LOCAL_TZ
from app.models.schema import AlertState

KINDS: tuple[str, ...] = ("health", "budget", "anomaly")
BUDGET_THRESHOLDS: tuple[int, ...] = (80, 100)  # percent of daily_limit_usd
BUDGET_KEY_RETENTION = timedelta(days=7)
_POST_TIMEOUT_S = 10.0

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="teams-alert")

# Budget keys this worker already handled today: past the first request over
# a threshold, the rest of the user's requests that day skip the DB claim.
_budget_lock = threading.Lock()
_budget_day: str = ""
_budget_seen: set[str] = set()


def webhook_url() -> str:
    return os.getenv("TEAMS_WEBHOOK_URL", "").strip()


def enabled(kind: str) -> bool:
    """Whether alerts of ``kind`` are on: a webhook is configured and
    ``TEAMS_ALERTS`` (default: every kind) includes it."""
    if not webhook_url():
        return False
    raw = os.getenv("TEAMS_ALERTS", "").strip()
    if not raw:
        return True
    return kind in {k.strip().lower() for k in raw.split(",")}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Delivery ──

def build_card(
    title: str,
    facts: list[tuple[str, str]],
    *,
    color: str = "attention",
    text: str | None = None,
) -> dict[str, Any]:
    """A Teams message carrying one Adaptive Card. ``color`` is an Adaptive
    Card text color: ``attention`` (red), ``warning``, ``good``."""
    body: list[dict[str, Any]] = [{
        "type": "TextBlock", "text": title, "weight": "Bolder",
        "size": "Medium", "color": color, "wrap": True,
    }]
    if text:
        body.append({"type": "TextBlock", "text": text, "wrap": True})
    body.append({
        "type": "FactSet",
        "facts": [{"title": k, "value": v} for k, v in
                  [*facts, ("Gateway", f"{APP_TITLE} @ {socket.gethostname()}")]],
    })
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": body,
            },
        }],
    }


def post(card: dict[str, Any]) -> bool:
    """POST ``card`` to the webhook. Blocking; returns whether it was
    accepted. Never raises, and never logs the URL (it carries a secret)."""
    url = webhook_url()
    if not url:
        return False
    try:
        resp = httpx.post(url, json=card, timeout=_POST_TIMEOUT_S)
    except Exception as exc:
        logger.warning("Teams alert failed | error={}: {}", type(exc).__name__, exc)
        return False
    if resp.status_code >= 300:
        logger.warning("Teams alert rejected | status={} body={}", resp.status_code, resp.text[:200])
        return False
    return True


def _submit(fn, *args) -> None:
    def run() -> None:
        try:
            fn(*args)
        except Exception as exc:
            logger.warning("Teams alert job failed | error={}: {}", type(exc).__name__, exc)

    try:
        _executor.submit(run)
    except Exception as exc:  # e.g. the executor is shutting down
        logger.warning("Teams alert not queued | error={}: {}", type(exc).__name__, exc)


# ── Health ──

def server_state_changed(base_url: str, alive: bool, models: list[str]) -> None:
    """The health loop saw ``base_url`` change state in this worker (or saw
    it for the first time). Posts only if no worker announced it yet."""
    if not enabled("health"):
        return
    _submit(_health_job, base_url, alive, models)


def _claim_health(base_url: str, state: str) -> bool:
    """Record ``state`` for ``base_url``; True when this call is the one that
    should announce it. A server seen for the first time is news only when
    it is DOWN."""
    key = f"health:{base_url}"
    with Session(engine) as session:
        row = session.exec(
            select(AlertState).where(AlertState.key == key).with_for_update()
        ).first()
        if row is None:
            session.add(AlertState(key=key, state=state, updated_at=_utcnow()))
            try:
                session.commit()
            except IntegrityError:
                return False  # another worker recorded it first
            return state == "down"
        if row.state == state:
            return False
        row.state = state
        row.updated_at = _utcnow()
        session.add(row)
        session.commit()
        return True


def _health_job(base_url: str, alive: bool, models: list[str]) -> None:
    state = "up" if alive else "down"
    if not _claim_health(base_url, state):
        return
    facts = [("Server", base_url), ("Models", ", ".join(models) or "-")]
    if alive:
        post(build_card("On-prem server back UP", facts, color="good"))
    else:
        post(build_card(
            "On-prem server DOWN", facts,
            text="Health probes are failing. Requests for these models go to another server of the same type while one is up.",
        ))


# ── Budget ──

def budget_reached(user: Any, spent: float, limit: float) -> None:
    """Called from the daily-limit check with today's spend. Posts once per
    account, local day and threshold when spend reaches 80% / 100%. A $0
    budget is a deliberate block, not news."""
    if limit <= 0 or spent < limit * BUDGET_THRESHOLDS[0] / 100 or not enabled("budget"):
        return
    global _budget_day
    try:
        pct = max(t for t in BUDGET_THRESHOLDS if spent >= limit * t / 100)
        day = datetime.now(LOCAL_TZ).date().isoformat()
        key = f"budget:{user.id}:{day}:{pct}"
        with _budget_lock:
            if day != _budget_day:
                _budget_day = day
                _budget_seen.clear()
            if key in _budget_seen:
                return
            _budget_seen.add(key)
        _submit(_budget_job, key, user.username, getattr(user, "display_name", "") or "", spent, limit, pct)
    except Exception as exc:  # an alert must never fail the auth check
        logger.warning("Teams budget alert failed | error={}: {}", type(exc).__name__, exc)


def _claim_once(key: str) -> bool:
    with Session(engine) as session:
        session.add(AlertState(key=key, state="sent", updated_at=_utcnow()))
        try:
            session.commit()
        except IntegrityError:
            return False
    return True


def _usd(value: float) -> str:
    """Dollars for a card: cents, or more digits for sub-cent amounts."""
    return f"${value:,.2f}" if value == 0 or abs(value) >= 0.01 else f"${value:.6f}"


def _exhausted_note() -> str:
    from app.core.deps import _enforce_daily_limit  # deps imports this module

    if _enforce_daily_limit():
        return "Further requests get 429 until local midnight."
    return "ENFORCE_DAILY_LIMIT is off: requests are still served (soft mode)."


def _budget_job(key: str, username: str, display_name: str, spent: float, limit: float, pct: int) -> None:
    if not _claim_once(key):
        return
    who = f"{username} ({display_name})" if display_name else username
    reached = pct >= 100
    post(build_card(
        f"Daily budget {'exhausted' if reached else f'at {pct}%'}: {who}",
        [("Account", who), ("Spent today", _usd(spent)), ("Daily limit", _usd(limit))],
        color="attention" if reached else "warning",
        text=_exhausted_note() if reached else None,
    ))


def prune(session: Session) -> int:
    """Delete budget keys older than ``BUDGET_KEY_RETENTION`` (health keys
    are one per server and stay). Run by the hourly anomaly scan."""
    cutoff = _utcnow() - BUDGET_KEY_RETENTION
    result = session.execute(
        delete(AlertState)
        .where(AlertState.key.startswith("budget:", autoescape=True))  # type: ignore[attr-defined]
        .where(AlertState.updated_at < cutoff)
    )
    session.commit()
    return result.rowcount or 0


# ── Anomalies ──

_MAX_ANOMALY_LINES = 15


def post_anomalies(findings: list[dict[str, Any]], usernames: dict[int, str]) -> bool:
    """Post the scan's newly created findings as one card. Blocking (the
    scan is a short-lived process that must not exit before sending)."""
    if not findings or not enabled("anomaly"):
        return False
    import json

    lines = []
    for f in findings[:_MAX_ANOMALY_LINES]:
        who = usernames.get(f.get("user_id") or -1) or f["scope"]
        try:
            summary = json.loads(f.get("detail") or "{}").get("summary", "")
        except Exception:
            summary = ""
        lines.append(f"- **{f['severity']}** `{f['rule']}` {who}: {summary}")
    if len(findings) > _MAX_ANOMALY_LINES:
        lines.append(f"- … and {len(findings) - _MAX_ANOMALY_LINES} more")
    critical = any(f["severity"] == "critical" for f in findings)
    return post(build_card(
        f"{len(findings)} new usage anomal{'y' if len(findings) == 1 else 'ies'}",
        [("Review", "Admin panel → Anomalies")],
        color="attention" if critical else "warning",
        text="\n".join(lines),
    ))
