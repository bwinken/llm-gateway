"""Which accounts' Claude Code is waiting for a server-side auto mode review.

Behind an ``ANTHROPIC_BASE_URL`` gateway, Claude Code's auto mode asks the
upstream to review each action: the request carries a ``safeguards`` body
field (paired with an ``anthropic-beta`` header) and the client waits for a
verdict in the response. No backend of this gateway produces one, and the
field is dropped on the way down, so the client refuses every action ("Auto
mode is unavailable: its safety check does not run for this session").

The client-side fix is two settings (``CLAUDE_CODE_AUTO_MODE_SERVER=0`` and
``CLAUDE_CODE_AUTO_MODE_MODEL``), which the gateway cannot write. What it can
do is notice who is affected: a Messages request carrying ``safeguards``
stamps ``users.auto_mode_server_seen_at``. The dashboard shows that user how
to fix it and the admin page lists the accounts.

A side effect: it never fails or changes a request, and nothing here reads
or logs the field's value.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import Session, select, update
from starlette.concurrency import run_in_threadpool

from app.core.database import engine
from app.core.logger import logger
from app.models.schema import User

# The request field that marks a session waiting for the server's verdict.
FIELD = "safeguards"

# One write per user per worker per hour, like the API-key last-use stamp.
RESOLUTION_S = 3600

# How long after the last such request the notice stays up. A fixed client
# stops sending the field, so the notice clears by itself after this long.
WINDOW_DAYS = 7

# The settings that make Claude Code run its own check instead.
SERVER_ENV = "CLAUDE_CODE_AUTO_MODE_SERVER"
MODEL_ENV = "CLAUDE_CODE_AUTO_MODE_MODEL"

_lock = threading.Lock()
_last: dict[int, float] = {}


def _due(user_id: int | None, now: float | None = None) -> bool:
    if user_id is None:
        return False
    now = time.monotonic() if now is None else now
    with _lock:
        last = _last.get(user_id)
        if last is not None and now - last < RESOLUTION_S:
            return False
        _last[user_id] = now
        return True


def reset_cache() -> None:
    """Forget which users were stamped recently (tests)."""
    with _lock:
        _last.clear()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _record(user_id: int) -> None:
    now = _utcnow()
    stale = now - timedelta(seconds=RESOLUTION_S)
    col = User.auto_mode_server_seen_at
    with Session(engine) as session:
        session.exec(
            update(User)
            .where(User.id == user_id, (col.is_(None)) | (col < stale))  # type: ignore[union-attr,operator]
            .values(auto_mode_server_seen_at=now)
        )
        session.commit()


async def observe(body: Any, user: User) -> None:
    """Note that ``user`` sent a Messages request asking for a server-side
    review. ``body`` is the parsed request body (anything else is ignored)."""
    try:
        if not isinstance(body, dict) or FIELD not in body:
            return
        if not _due(user.id):
            return
        logger.info(
            "Claude Code auto mode: request asks for a server-side review this gateway "
            "cannot give | user={} (fix: {}=0 and {})",
            user.username, SERVER_ENV, MODEL_ENV,
        )
        await run_in_threadpool(_record, user.id)
    except Exception as exc:
        logger.warning(
            "Recording auto mode notice failed | user={} error={}: {}",
            getattr(user, "username", "-"), type(exc).__name__, exc,
        )


async def observe_request(request: Any, user: User) -> None:
    """``observe`` for a route handler: reads the (cached) request body."""
    try:
        body = await request.json()
    except Exception:
        return
    await observe(body, user)


def is_active(seen_at: datetime | None, now: datetime | None = None) -> bool:
    """Whether a stamp is recent enough to still show the notice."""
    if seen_at is None:
        return False
    now = _utcnow() if now is None else now
    return now - seen_at <= timedelta(days=WINDOW_DAYS)


def affected(session: Session, now: datetime | None = None) -> list[dict[str, Any]]:
    """Accounts seen inside the window, newest first (the admin panel)."""
    now = _utcnow() if now is None else now
    since = now - timedelta(days=WINDOW_DAYS)
    col = User.auto_mode_server_seen_at
    rows = session.exec(
        select(User.username, User.display_name, col)
        .where(col.is_not(None), col >= since)  # type: ignore[union-attr,operator]
        .order_by(col.desc())  # type: ignore[union-attr]
    ).all()
    return [
        {"username": username, "display_name": display_name, "seen_at": seen_at}
        for username, display_name, seen_at in rows
    ]
