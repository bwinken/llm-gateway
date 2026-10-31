"""Daily usage rollup: ``usage_daily`` and the reads that combine it with
``usage_logs``.

Reports used to aggregate ``usage_logs`` directly, so every admin page load
scanned a whole month of requests, and the cost grew with traffic. Closed
local (Asia/Taipei) days are now summed once into ``usage_daily``, a few
rows per user per day, and reports read:

    closed days up to the watermark  ->  usage_daily
    every later day (today included) ->  usage_logs

The watermark (``usage_rollup_state.rolled_through``) is the last day the
rollup has written, with no gaps before it. A rollup that never ran or fell
behind only moves more days onto the ``usage_logs`` side: slower, still
exact. ``aggregate`` is the one read entry point; report code never picks a
side itself.

Writes come from ``rollup_loop``, a background task every gateway worker
starts in the lifespan (``scripts/rollup_usage_daily.py`` runs the same code
by hand, for repairs). One transaction per day: delete the day's rows,
re-insert them from a single ``INSERT ... SELECT ... GROUP BY``, move the
watermark. Request handling never writes here, so there is no row
contention with traffic: the per-request work is still one ``usage_logs``
INSERT. Every worker tries, and a transaction-scoped advisory lock lets one
of them (across hosts too: the lock lives in PostgreSQL) do each day while
the rest skip. ``delete_user`` takes the same lock so a concurrent rollup
can't re-insert rows for a user being deleted. The lock is held for one
day's transaction only, never across a request or stream.

A day is rolled only once it has been over for ``SETTLE_DELAY``:
``_log_usage`` stamps ``created_at`` when the row is built and commits right
after, so nothing lands in a day that long closed.
"""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import sqlalchemy as sa
from sqlmodel import Session, delete, func, select

from app.core.database import engine
from app.core.logger import logger
from app.core.timeutil import LOCAL_TZ, local_day_start_utc_of
from app.models.schema import UsageDaily, UsageLog, UsageRollupState

STATE_NAME = "usage_daily"
SETTLE_DELAY = timedelta(minutes=15)
DEFAULT_INTERVAL_S = 600.0
# Per day transaction (PostgreSQL ``SET LOCAL``): one day's GROUP BY.
_DAY_STATEMENT_TIMEOUT_MS = 600_000
# pg_advisory_xact_lock key shared by the rollup writer and delete_user.
_LOCK_KEY = 0x75736167_64617931  # "usagday1"

DIMENSIONS = ("day", "user_id", "backend", "model", "model_type", "endpoint")
_GROUP_COLUMNS = ("user_id", "backend", "model", "model_type", "endpoint")


class RollupBusy(RuntimeError):
    """Another rollup run holds the lock."""


# ── Shared SQL pieces ──

def local_date_expr(session: Session, col):
    """SQL expression: Asia/Taipei calendar date of a UTC-wallclock timestamp column."""
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        return func.date(col, "+8 hours")
    return func.date(col + timedelta(hours=8))


def _empty_turn_case():
    return sa.case((UsageLog.output_tokens <= 1, 1), else_=0)


def _is_postgres(session: Session) -> bool:
    return session.bind is not None and session.bind.dialect.name == "postgresql"


def lock(session: Session) -> None:
    """Take the rollup lock for the rest of the current transaction (blocks).
    No-op off PostgreSQL."""
    if _is_postgres(session):
        session.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})


def _try_lock(session: Session) -> bool:
    """Take the rollup lock for the current transaction if it is free, and
    bound the transaction's statements. Always True off PostgreSQL."""
    if not _is_postgres(session):
        return True
    if not session.execute(
        sa.text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _LOCK_KEY}
    ).scalar():
        return False
    session.execute(sa.text(f"SET LOCAL statement_timeout = {_DAY_STATEMENT_TIMEOUT_MS}"))
    return True


# ── Reads ──

def rolled_through(session: Session) -> date | None:
    """Last local day present in ``usage_daily`` (no gaps before it), or None."""
    value = session.exec(
        select(UsageRollupState.rolled_through).where(UsageRollupState.name == STATE_NAME)
    ).first()
    return _as_date(value) if value is not None else None


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _as_decimal(value) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _merge(out: dict[tuple, dict], rows, n_dims: int, day_index: int | None) -> None:
    for row in rows:
        requests = int(row[n_dims] or 0)
        if requests == 0:
            continue
        key = tuple(
            _as_date(v) if i == day_index else v
            for i, v in enumerate(row[:n_dims])
        )
        entry = out.get(key)
        if entry is None:
            entry = out[key] = {
                "requests": 0, "input_tokens": 0, "output_tokens": 0,
                "cost_usd": Decimal("0"), "empty_turns": 0,
            }
        entry["requests"] += requests
        entry["input_tokens"] += int(row[n_dims + 1] or 0)
        entry["output_tokens"] += int(row[n_dims + 2] or 0)
        entry["cost_usd"] += _as_decimal(row[n_dims + 3])
        entry["empty_turns"] += int(row[n_dims + 4] or 0)


def aggregate(
    session: Session,
    start: date,
    end: date,
    dims: Sequence[str] = (),
    *,
    user_ids: Iterable[int] | None = None,
) -> dict[tuple, dict]:
    """Usage totals over local days ``[start, end)``, grouped by ``dims``.

    Returns ``{key: {"requests", "input_tokens", "output_tokens",
    "cost_usd" (Decimal), "empty_turns"}}`` where ``key`` is the tuple of
    ``dims`` values in the order given (``day`` as a ``date``); with no dims
    the single key is ``()``. Groups without requests are absent.

    Days up to the rollup watermark come from ``usage_daily``, the rest from
    ``usage_logs``; the result is the same either way.
    """
    for d in dims:
        if d not in DIMENSIONS:
            raise ValueError(f"unknown dimension {d!r}")
    ids = None if user_ids is None else sorted({int(u) for u in user_ids})
    if start >= end or ids == []:
        return {}

    day_index = dims.index("day") if "day" in dims else None
    watermark = rolled_through(session)
    split = start if watermark is None else min(max(start, watermark + timedelta(days=1)), end)
    out: dict[tuple, dict] = {}

    if split > start:
        cols = [getattr(UsageDaily, d) for d in dims]
        stmt = (
            select(
                *cols,
                func.sum(UsageDaily.requests),
                func.sum(UsageDaily.input_tokens),
                func.sum(UsageDaily.output_tokens),
                func.sum(UsageDaily.cost_usd),
                func.sum(UsageDaily.empty_turns),
            )
            .where(UsageDaily.day >= start)
            .where(UsageDaily.day < split)
        )
        if ids is not None:
            stmt = stmt.where(UsageDaily.user_id.in_(ids))  # type: ignore[attr-defined]
        if cols:
            stmt = stmt.group_by(*cols)
        _merge(out, session.exec(stmt).all(), len(dims), day_index)

    if end > split:
        cols = [
            local_date_expr(session, UsageLog.created_at) if d == "day" else getattr(UsageLog, d)
            for d in dims
        ]
        stmt = (
            select(
                *cols,
                func.count(),
                func.sum(UsageLog.input_tokens),
                func.sum(UsageLog.output_tokens),
                func.sum(UsageLog.cost_usd),
                func.sum(_empty_turn_case()),
            )
            .where(UsageLog.created_at >= local_day_start_utc_of(split))
            .where(UsageLog.created_at < local_day_start_utc_of(end))
        )
        if ids is not None:
            stmt = stmt.where(UsageLog.user_id.in_(ids))  # type: ignore[attr-defined]
        if cols:
            stmt = stmt.group_by(*cols)
        _merge(out, session.exec(stmt).all(), len(dims), day_index)

    return out


# ── Writes (scripts/rollup_usage_daily.py) ──

def settled_through(now: datetime | None = None) -> date:
    """Last local day that ended at least ``SETTLE_DELAY`` ago."""
    now = now or datetime.now(timezone.utc)
    return (now - SETTLE_DELAY).astimezone(LOCAL_TZ).date() - timedelta(days=1)


def earliest_log_day(session: Session) -> date | None:
    """Local day of the oldest ``usage_logs`` row (one index probe)."""
    oldest = session.exec(select(func.min(UsageLog.created_at))).first()
    if oldest is None:
        return None
    if isinstance(oldest, str):
        oldest = datetime.fromisoformat(oldest)
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    return oldest.astimezone(LOCAL_TZ).date()


def rollup_day(session: Session, day: date) -> int:
    """Replace ``day``'s rows in ``usage_daily`` with a fresh sum of
    ``usage_logs``. Does not commit. Returns the number of rows written."""
    session.execute(delete(UsageDaily).where(UsageDaily.day == day))
    src = (
        select(
            sa.literal(day, sa.Date),
            *(getattr(UsageLog, c) for c in _GROUP_COLUMNS),
            func.count(),
            func.coalesce(func.sum(UsageLog.input_tokens), 0),
            func.coalesce(func.sum(UsageLog.output_tokens), 0),
            func.coalesce(func.sum(UsageLog.cost_usd), 0),
            func.coalesce(func.sum(_empty_turn_case()), 0),
        )
        .where(UsageLog.created_at >= local_day_start_utc_of(day))
        .where(UsageLog.created_at < local_day_start_utc_of(day + timedelta(days=1)))
        .group_by(*(getattr(UsageLog, c) for c in _GROUP_COLUMNS))
    )
    result = session.execute(
        sa.insert(UsageDaily).from_select(
            ["day", *_GROUP_COLUMNS,
             "requests", "input_tokens", "output_tokens", "cost_usd", "empty_turns"],
            src,
        )
    )
    return int(result.rowcount or 0)


def _set_rolled_through(session: Session, day: date) -> None:
    state = session.get(UsageRollupState, STATE_NAME)
    now = datetime.now(timezone.utc)
    if state is None:
        session.add(UsageRollupState(name=STATE_NAME, rolled_through=day, updated_at=now))
    else:
        state.rolled_through = day
        state.updated_at = now
        session.add(state)


def _next_day_to_roll(session: Session) -> date | None:
    watermark = rolled_through(session)
    if watermark is not None:
        return watermark + timedelta(days=1)
    return earliest_log_day(session)


def pending_days(session: Session, now: datetime | None = None) -> list[date]:
    """Settled days not rolled yet, oldest first."""
    first = _next_day_to_roll(session)
    last = settled_through(now)
    if first is None or first > last:
        return []
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def advance(
    session: Session,
    now: datetime | None = None,
    *,
    max_days: int | None = None,
    on_day: Callable[[date, int], None] | None = None,
) -> list[date]:
    """Roll settled days after the watermark, oldest first, one transaction
    per day (an interrupted backfill resumes where it stopped), at most
    ``max_days`` of them. Raises ``RollupBusy`` if another run holds the lock."""
    last = settled_through(now)
    done: list[date] = []
    while max_days is None or len(done) < max_days:
        if not _try_lock(session):
            session.rollback()
            raise RollupBusy("another usage_daily rollup is running")
        day = _next_day_to_roll(session)
        if day is None or day > last:
            session.rollback()
            return done
        rows = rollup_day(session, day)
        _set_rolled_through(session, day)
        session.commit()
        done.append(day)
        if on_day is not None:
            on_day(day, rows)
    return done


def recompute(
    session: Session,
    first: date,
    last: date,
    *,
    on_day: Callable[[date, int], None] | None = None,
) -> tuple[list[date], list[date]]:
    """Re-roll already-rolled days in ``[first, last]`` from ``usage_logs``
    (repair after a backfill or correction). Leaves the watermark alone.

    Days older than the oldest ``usage_logs`` row are skipped, not wiped:
    their raw rows are gone (retention cleanup or archive), and the rollup
    is all that is left of them. Returns ``(recomputed, skipped)``.
    """
    watermark = rolled_through(session)
    session.rollback()
    if watermark is None:
        return [], []
    earliest = earliest_log_day(session)
    session.rollback()
    done: list[date] = []
    skipped: list[date] = []
    day = first
    while day <= min(last, watermark):
        if earliest is None or day < earliest:
            skipped.append(day)
        else:
            if not _try_lock(session):
                session.rollback()
                raise RollupBusy("another usage_daily rollup is running")
            rows = rollup_day(session, day)
            session.commit()
            done.append(day)
            if on_day is not None:
                on_day(day, rows)
        day += timedelta(days=1)
    return done, skipped


# ── In-app scheduling (every worker, from the lifespan) ──

def interval_seconds() -> float:
    """``USAGE_ROLLUP_INTERVAL_S`` (default 600). ``0`` turns the background
    rollup off: reports then read ``usage_logs`` for every day after the
    watermark (still correct, slower as days pile up)."""
    raw = os.getenv("USAGE_ROLLUP_INTERVAL_S", "").strip()
    if not raw:
        return DEFAULT_INTERVAL_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid USAGE_ROLLUP_INTERVAL_S={!r}; using {}", raw, DEFAULT_INTERVAL_S)
        return DEFAULT_INTERVAL_S
    return max(value, 0.0)


def roll_next_day(bind=None) -> date | None:
    """Roll the oldest pending day, if any and if no other worker is on it.
    Blocking; never raises (a missing table before ``alembic upgrade`` or a
    DB hiccup is logged and retried next tick)."""
    try:
        with Session(bind or engine) as session:
            done = advance(session, max_days=1)
    except RollupBusy:
        return None
    except Exception as exc:
        logger.warning("Usage rollup failed | error={}: {}", type(exc).__name__, exc)
        return None
    if done:
        logger.info("Usage rollup: rolled {} into usage_daily", done[0].isoformat())
        return done[0]
    return None


async def rollup_loop(interval: float | None = None) -> None:
    """Background task (one per worker). Each tick drains the pending days
    one transaction at a time, so a first-deploy backfill runs in the
    background without holding a pool connection for long, then sleeps."""
    interval = interval_seconds() if interval is None else interval
    if interval <= 0:
        logger.info("Usage rollup disabled (USAGE_ROLLUP_INTERVAL_S=0)")
        return
    # Spread the workers' first attempts; the lock sorts out the rest.
    await asyncio.sleep(random.uniform(5.0, 30.0))
    while True:
        while await asyncio.to_thread(roll_next_day) is not None:
            await asyncio.sleep(0.5)
        await asyncio.sleep(interval)
