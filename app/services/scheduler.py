"""Periodic jobs the gateway runs itself: no systemd timer, no cron.

Every worker starts one ``job_loop`` per job from the lifespan. Each tick
(about a minute) the loop asks the database whether the job is due and, if
so, claims it with a conditional UPDATE:

    UPDATE scheduled_jobs SET last_started_at = :now
     WHERE name = :job AND (last_started_at IS NULL OR last_started_at <= :now - interval)

PostgreSQL lets exactly one concurrent UPDATE match (the others re-check the
WHERE against the committed row and find it no longer due), so a job runs
once per interval across every worker and every host, and survives restarts
on schedule. The claim commits before the job starts: nothing is held while
it runs. A worker killed mid-job leaves the claim in place, so that interval
is skipped rather than re-run.

A job failing (or its claim failing, e.g. before ``alembic upgrade``) is
logged and recorded in ``scheduled_jobs.last_status``; it never raises into
the gateway. The job body runs in a thread with its own short-lived session.
"""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.core.database import engine
from app.core.logger import logger
from app.models.schema import ScheduledJob

TICK_S = 60.0


@dataclass(frozen=True)
class Job:
    name: str
    # Seconds between runs, read when the loop starts; <= 0 means disabled.
    interval: Callable[[], float]
    # The job body: gets a fresh Session, returns a one-line summary.
    run: Callable[[Session], str]


def env_seconds(name: str, default: float) -> float:
    """A non-negative number of seconds from the environment (blank = default)."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid {}={!r}; using {}", name, raw, default)
        return default
    return max(value, 0.0)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def claim(session: Session, name: str, interval_s: float, now: datetime | None = None) -> bool:
    """Claim ``name`` for a run if its last start is ``interval_s`` old (or it
    never ran). Commits. True for exactly one caller per interval."""
    now = now or _utcnow()
    if session.get(ScheduledJob, name) is None:
        session.add(ScheduledJob(name=name))
        try:
            session.commit()
        except IntegrityError:  # another worker created it first
            session.rollback()
    result = session.execute(
        update(ScheduledJob)
        .where(ScheduledJob.name == name)
        .where(or_(
            ScheduledJob.last_started_at.is_(None),  # type: ignore[union-attr]
            ScheduledJob.last_started_at <= now - timedelta(seconds=interval_s),
        ))
        .values(last_started_at=now, last_status="running", last_detail="")
    )
    session.commit()
    return result.rowcount == 1


def finish(session: Session, name: str, status: str, detail: str) -> None:
    session.execute(
        update(ScheduledJob)
        .where(ScheduledJob.name == name)
        .values(last_finished_at=_utcnow(), last_status=status, last_detail=detail[:500])
    )
    session.commit()


def run_if_due(job: Job, bind=None) -> str | None:
    """Claim ``job`` and run it if it is due. Blocking; never raises.
    Returns ``"ok"`` / ``"error"`` when it ran, None when it didn't."""
    interval = job.interval()
    if interval <= 0:
        return None
    bind = bind or engine
    try:
        with Session(bind) as session:
            if not claim(session, job.name, interval):
                return None
    except Exception as exc:
        logger.warning("Scheduled job {} claim failed | error={}: {}", job.name, type(exc).__name__, exc)
        return None

    try:
        with Session(bind) as session:
            detail = job.run(session) or ""
        status = "ok"
        logger.info("Scheduled job {} done | {}", job.name, detail)
    except Exception as exc:
        status, detail = "error", f"{type(exc).__name__}: {exc}"
        logger.warning("Scheduled job {} failed | error={}", job.name, detail)
    try:
        with Session(bind) as session:
            finish(session, job.name, status, detail)
    except Exception as exc:
        logger.warning("Scheduled job {} status write failed | error={}: {}", job.name, type(exc).__name__, exc)
    return status


async def job_loop(job: Job, tick: float = TICK_S) -> None:
    """Background task (one per job per worker)."""
    if job.interval() <= 0:
        logger.info("Scheduled job {} disabled", job.name)
        return
    await asyncio.sleep(random.uniform(5.0, 30.0))  # spread the workers' first claims
    while True:
        await asyncio.to_thread(run_if_due, job)
        await asyncio.sleep(tick + random.uniform(0.0, 5.0))
