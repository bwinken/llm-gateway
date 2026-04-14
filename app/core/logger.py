"""
Loguru-based logger with stdout + rotating file sink.

Multi-worker strategy
---------------------
Each worker process writes to its own file (``gateway_{pid}.log``) so that
uvicorn / gunicorn workers never share a file handle. Sharing a single file
across workers is unsafe once loguru rotates: the worker that triggers the
rename keeps writing to the new file while other workers continue writing
to the old (renamed) one, silently losing data.

Per-PID files sidestep that entirely. Each worker independently rotates and
applies retention to its own files. On startup we also sweep the log
directory for orphan files left behind by previous processes so stale PIDs
don't accumulate forever.

Configuration (environment variables)
-------------------------------------
LOG_DIR        Directory for file logs (default: ./logs)
LOG_LEVEL      File sink level (default: WARNING)
LOG_ROTATION   Rotation trigger, passed through to loguru (default: "100 MB")
LOG_RETENTION  Retention, passed through to loguru (default: "14 days")
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

# Logger may be imported before app.core.config (e.g. by tests), so ensure
# .env is loaded here too. ``load_dotenv`` is idempotent.
load_dotenv()

LOG_DIR = Path(os.getenv("LOG_DIR", "./logs")).expanduser()
LOG_LEVEL = os.getenv("LOG_LEVEL", "WARNING").upper()
LOG_ROTATION = os.getenv("LOG_ROTATION", "100 MB")
LOG_RETENTION = os.getenv("LOG_RETENTION", "14 days")

LOG_DIR.mkdir(parents=True, exist_ok=True)


def _retention_seconds(spec: str) -> float | None:
    """Best-effort parse of a loguru-style retention spec into seconds.

    Only used for the orphan-file sweep. Supports "<N> day(s)", "<N> week(s)",
    "<N> hour(s)". Returns None if the spec can't be parsed (in which case we
    skip the sweep and rely solely on loguru's per-file retention)."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*(second|minute|hour|day|week|month|year)s?\s*$", spec, re.I)
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2).lower()
    return n * {
        "second": 1,
        "minute": 60,
        "hour": 3600,
        "day": 86400,
        "week": 7 * 86400,
        "month": 30 * 86400,
        "year": 365 * 86400,
    }[unit]


def _sweep_orphan_logs(log_dir: Path, retention: str) -> None:
    """Delete ``gateway_*.log*`` files older than the retention window.

    Loguru's own retention only touches files produced by the active
    handler's filename template (i.e. the current PID), so files from dead
    workers stay around forever. This sweep catches them at startup."""
    max_age = _retention_seconds(retention)
    if max_age is None:
        return
    cutoff = time.time() - max_age
    for p in log_dir.glob("gateway_*.log*"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            # Another worker may have just rotated or deleted this file —
            # swallow the race rather than crashing startup.
            pass


_sweep_orphan_logs(LOG_DIR, LOG_RETENTION)

# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------

logger.remove()

# Console — keep INFO for development visibility
logger.add(
    sys.stdout,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>llm_gateway</cyan> | <level>{message}</level>",
    level="INFO",
    colorize=True,
)

# File — per-worker so concurrent writes never collide. ``enqueue=True``
# makes logging calls thread-safe and non-blocking for the caller.
logger.add(
    LOG_DIR / f"gateway_{os.getpid()}.log",
    format=(
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
        "pid={process} | {name}:{function}:{line} | {message}"
    ),
    level=LOG_LEVEL,
    rotation=LOG_ROTATION,
    retention=LOG_RETENTION,
    compression="gz",
    enqueue=True,
    encoding="utf-8",
)
