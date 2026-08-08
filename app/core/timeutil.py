"""Time helpers for daily-window calculations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Taipei")


def seconds_until_local_midnight() -> int:
    """Seconds until the next local midnight — when daily quotas reset.

    Used as the Retry-After value on daily-limit 429s so well-behaved
    clients back off until the reset instead of hammering the gateway.
    Never returns less than 1.
    """
    local_now = datetime.now(LOCAL_TZ)
    next_midnight = (local_now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(1, int((next_midnight - local_now).total_seconds()))


def local_day_start_utc() -> datetime:
    """Return the UTC-aware datetime that corresponds to the most recent
    midnight in Asia/Taipei. Used to gate the per-user daily spending limit
    so that quotas reset at local 00:00 (UTC+8 → 16:00 UTC the previous day).
    """
    local_now = datetime.now(LOCAL_TZ)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc)
