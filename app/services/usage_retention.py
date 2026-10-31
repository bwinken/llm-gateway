"""usage_logs retention: delete raw request rows older than N days.

**Off unless configured.** ``usage_logs`` is the billing and audit record, so
deleting it is a governance decision: set ``USAGE_LOG_RETENTION_DAYS`` (e.g.
365) to turn it on. The gateway then runs ``RETENTION_JOB`` once a day
(app/services/scheduler.py); ``scripts/cleanup_usage_logs.py`` previews or
runs it by hand.

What survives a purge: every report, because days already rolled into
``usage_daily`` are read from there. Two guards keep that true:

* Never past the rollup watermark. A day not yet in ``usage_daily`` would
  vanish from every report, so the cutoff is capped at the first un-rolled
  day, and nothing is deleted at all before the rollup's first run.
* Whole local days only. The cutoff is a local (Asia/Taipei) midnight, so
  the oldest day left in ``usage_logs`` is complete and a later
  ``rollup --recompute`` of it can't shrink its ``usage_daily`` rows.

Rows go in batches, one short transaction each, oldest first (by
``ix_usage_created_at``), so no long transaction or lock builds up on a
table the gateway writes to on every request.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlmodel import Session, func, select

from app.core.logger import logger
from app.core.timeutil import LOCAL_TZ, local_day_start_utc_of
from app.models.schema import UsageLog
from app.services.scheduler import Job
from app.services.usage_rollup import rolled_through

BATCH_SIZE = 10_000
BATCH_PAUSE_S = 0.2  # between batches of the scheduled run: let request inserts through


def retention_days() -> int:
    """``USAGE_LOG_RETENTION_DAYS``; unset, blank, 0 or invalid = keep forever."""
    raw = os.getenv("USAGE_LOG_RETENTION_DAYS", "").strip()
    if not raw:
        return 0
    try:
        days = int(raw)
    except ValueError:
        logger.warning("Invalid USAGE_LOG_RETENTION_DAYS={!r}; retention stays off", raw)
        return 0
    return max(days, 0)


def purge_cutoff(session: Session, days: int, now: datetime | None = None) -> datetime | None:
    """Rows with ``created_at`` before this may go (UTC-aware), or None when
    nothing may: retention off, or the rollup has not run yet."""
    if days <= 0:
        return None
    watermark = rolled_through(session)
    if watermark is None:
        return None
    now = now or datetime.now(timezone.utc)
    oldest_kept_day = (now - timedelta(days=days)).astimezone(LOCAL_TZ).date()
    first_unrolled_day = watermark + timedelta(days=1)
    return local_day_start_utc_of(min(oldest_kept_day, first_unrolled_day))


def count_before(session: Session, cutoff: datetime) -> int:
    return int(session.exec(
        select(func.count()).select_from(UsageLog).where(UsageLog.created_at < cutoff)
    ).one())


def purge_before(session: Session, cutoff: datetime, *, batch_size: int = BATCH_SIZE,
                 pause_s: float = 0.0, on_batch=None) -> int:
    """Delete rows older than ``cutoff`` in batches, committing each. Returns the total."""
    total = 0
    while True:
        ids = select(UsageLog.id).where(UsageLog.created_at < cutoff).order_by(UsageLog.created_at).limit(batch_size)
        result = session.execute(delete(UsageLog).where(UsageLog.id.in_(ids)))  # type: ignore[union-attr]
        session.commit()
        n = int(result.rowcount or 0)
        if n == 0:
            return total
        total += n
        if on_batch is not None:
            on_batch(n, total)
        if pause_s:
            time.sleep(pause_s)


def run_retention(session: Session) -> str:
    """One scheduled run. Returns a summary line."""
    days = retention_days()
    cutoff = purge_cutoff(session, days)
    if cutoff is None:
        return "skipped: usage_daily rollup has not run yet" if days else "retention off"
    deleted = purge_before(session, cutoff, pause_s=BATCH_PAUSE_S)
    return f"deleted {deleted} usage_logs row(s) before {cutoff.isoformat()} (retention {days} d)"


def _interval() -> float:
    return 86400.0 if retention_days() > 0 else 0.0


RETENTION_JOB = Job(name="usage_log_retention", interval=_interval, run=run_retention)
