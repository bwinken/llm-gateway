"""Time helpers for daily-window calculations."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Taipei")


def local_day_start_utc() -> datetime:
    """Return the UTC-aware datetime that corresponds to the most recent
    midnight in Asia/Taipei. Used to gate the per-user daily spending limit
    so that quotas reset at local 00:00 (UTC+8 → 16:00 UTC the previous day).
    """
    local_now = datetime.now(LOCAL_TZ)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc)
