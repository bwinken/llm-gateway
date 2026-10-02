"""
Per-user concurrency limit — the lease store.

Every limited request (chat / responses / messages on all three surfaces)
holds one row in ``concurrency_leases`` from the moment it is admitted until
its response — stream included — has been fully sent. "How many requests
does this user have in flight" is then a COUNT over that table, which every
gateway worker sees, so ``--workers 2`` enforces one limit instead of two
per-process ones.

Lifecycle of a lease:
  - ``acquire``  — short transaction: lock the user's row (``SELECT ... FOR
    UPDATE``, so two workers admitting the same user serialize), count the
    live leases, insert one unless that would exceed an enforced limit.
  - ``release``  — ``DELETE`` by id, from the request dependency's exit
    (``deps._concurrency_slot``), which runs after the response is sent.
  - heartbeat    — every ``HEARTBEAT_INTERVAL`` each worker pushes its own
    leases' ``expires_at`` forward (a long stream outlives the TTL) and
    sweeps leases whose owner is gone.

Recovering from a worker that died without releasing (SIGKILL after
systemd's stop timeout, OOM, crash):
  - same host, process gone   → deleted by the sweep. ``worker_id`` is
    ``<hostname>:<pid>:<boot token>``; ``os.kill(pid, 0)`` tells whether that
    pid still exists, and the random boot token catches the case where the
    kernel handed the dead worker's pid to the worker doing the sweep. The
    sweep runs at worker startup before any request is served, so after a
    ``systemctl restart`` the previous workers' leases are gone before the
    first retry arrives.
  - anything else (another host, a pid we may not signal) → ``expires_at``
    passes within ``LEASE_TTL`` because nobody renews it.

Nothing here holds a DB connection beyond one short transaction: an
advisory lock or a request-scoped session would pin a pooled connection for
the whole life of a stream, which is how the QueuePool was exhausted once
already (see ``deps.get_current_user``).
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, distinct, update
from sqlmodel import Session, func, select

from app.core.database import engine
from app.core.logger import logger
from app.models.schema import ConcurrencyLease, User

LEASE_TTL = timedelta(seconds=180)
HEARTBEAT_INTERVAL = 60.0

_identity: tuple[int, str] | None = None


def worker_id() -> str:
    """This process's lease owner id: ``<hostname>:<pid>:<boot token>``.

    Recomputed when the pid changes, so a forked worker never inherits its
    parent's identity."""
    global _identity
    pid = os.getpid()
    if _identity is None or _identity[0] != pid:
        _identity = (pid, f"{socket.gethostname()}:{pid}:{uuid.uuid4().hex[:12]}")
    return _identity[1]


def _utcnow() -> datetime:
    # Naive UTC — see the ConcurrencyLease docstring.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def acquire(user_id: int, limit: int, endpoint: str, *, enforce: bool) -> tuple[str | None, int]:
    """Try to take a slot for ``user_id``.

    Returns ``(lease_id, active)`` where ``active`` is how many leases the
    user held *before* this request. With ``enforce=True`` and
    ``active >= limit`` nothing is inserted and ``lease_id`` is None. In
    monitor mode (``enforce=False``) the lease is always taken, so the count
    — and the WARNING the caller logs — reflects the user's real peak rather
    than stopping at the limit.

    Blocking; async callers go through ``run_in_threadpool``.
    """
    with Session(engine) as session:
        # Serializes concurrent admissions of the same user across workers.
        # PostgreSQL only — SQLite ignores FOR UPDATE (and serializes writers
        # on its own).
        session.exec(select(User.id).where(User.id == user_id).with_for_update()).first()
        now = _utcnow()
        active = int(session.exec(
            select(func.count())
            .select_from(ConcurrencyLease)
            .where(ConcurrencyLease.user_id == user_id)
            .where(ConcurrencyLease.expires_at > now)
        ).one())
        if enforce and active >= limit:
            return None, active
        lease_id = uuid.uuid4().hex
        session.add(ConcurrencyLease(
            id=lease_id,
            user_id=user_id,
            worker_id=worker_id(),
            endpoint=endpoint[:200],
            created_at=now,
            expires_at=now + LEASE_TTL,
        ))
        session.commit()
        return lease_id, active


def release(lease_id: str) -> None:
    """Delete one lease. A lease already swept or expired is a no-op."""
    with Session(engine) as session:
        session.execute(delete(ConcurrencyLease).where(ConcurrencyLease.id == lease_id))
        session.commit()


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` exists on this host. Unknown → True (keep the lease;
    the TTL is the backstop), so a lease is only ever swept on proof."""
    if os.name != "posix":
        # On Windows os.kill(pid, 0) would *terminate* the process
        # (signal 0 is CTRL_C_EVENT there) — never probe.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError: exists but belongs to someone else
        return True
    return True


def _owner_is_dead(owner: str) -> bool:
    """True when ``owner`` (a lease's ``worker_id``) provably no longer runs."""
    me = worker_id()
    parts = owner.rsplit(":", 2)
    my_host, _, my_token = me.rsplit(":", 2)
    if len(parts) != 3:
        return False
    host, pid_s, token = parts
    if host != my_host:
        return False  # another host: cannot see its processes — TTL decides
    try:
        pid = int(pid_s)
    except ValueError:
        return False
    if pid == os.getpid():
        # Our own pid: either this process, or a dead worker whose pid the
        # kernel recycled for us — the boot token tells them apart.
        return token != my_token
    return not _pid_alive(pid)


def renew_and_sweep() -> tuple[int, int]:
    """Heartbeat: renew this worker's leases, delete dead workers' and
    expired leases. Returns ``(renewed, removed)``. Blocking."""
    now = _utcnow()
    with Session(engine) as session:
        renewed = session.execute(
            update(ConcurrencyLease)
            .where(ConcurrencyLease.worker_id == worker_id())
            .values(expires_at=now + LEASE_TTL)
        ).rowcount or 0
        owners = session.exec(select(distinct(ConcurrencyLease.worker_id))).all()
        dead = [o for o in owners if _owner_is_dead(o)]
        removed = 0
        if dead:
            removed += session.execute(
                delete(ConcurrencyLease).where(ConcurrencyLease.worker_id.in_(dead))  # type: ignore[attr-defined]
            ).rowcount or 0
        removed += session.execute(
            delete(ConcurrencyLease).where(ConcurrencyLease.expires_at <= now)
        ).rowcount or 0
        session.commit()
    if removed:
        logger.info(
            "Concurrency leases swept | removed={} dead_workers={} renewed={}",
            removed, len(dead), renewed,
        )
    return renewed, removed


def sweep_safely() -> None:
    """``renew_and_sweep`` that never raises — used at startup, where a
    missing table (migration not applied yet) must not stop the gateway."""
    try:
        renew_and_sweep()
    except Exception as exc:
        logger.warning("Concurrency lease sweep failed | error={}: {}", type(exc).__name__, exc)


async def lease_heartbeat_loop(interval: float = HEARTBEAT_INTERVAL) -> None:
    """Background task (one per worker): renew + sweep every ``interval``.

    Runs whatever the configured mode, so leases left over from before the
    limit was switched off still get cleaned up."""
    while True:
        await asyncio.sleep(interval)
        await asyncio.to_thread(sweep_safely)


def in_flight_summary() -> dict[str, int]:
    """Live leases right now, for the admin card: total, distinct users,
    and the busiest user's count. Blocking."""
    now = _utcnow()
    with Session(engine) as session:
        rows = session.exec(
            select(ConcurrencyLease.user_id, func.count())
            .where(ConcurrencyLease.expires_at > now)
            .group_by(ConcurrencyLease.user_id)
        ).all()
    counts = [int(c) for _, c in rows]
    return {
        "total": sum(counts),
        "users": len(counts),
        "peak_user": max(counts, default=0),
    }


def delete_user_leases(session: Session, user_id: int) -> None:
    """Remove a user's leases inside the caller's transaction (user delete)."""
    session.execute(delete(ConcurrencyLease).where(ConcurrencyLease.user_id == user_id))
