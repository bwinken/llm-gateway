"""Time helpers for daily-window calculations."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Taipei")


def next_local_midnight(local_now: datetime | None = None) -> datetime:
    """The next local midnight (aware, Asia/Taipei) — when daily quotas reset."""
    local_now = local_now or datetime.now(LOCAL_TZ)
    return (local_now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def seconds_until_local_midnight() -> int:
    """Seconds until the next local midnight — when daily quotas reset.

    Used as the Retry-After value on daily-limit 429s so well-behaved
    clients back off until the reset instead of hammering the gateway.
    Never returns less than 1.
    """
    local_now = datetime.now(LOCAL_TZ)
    return max(1, int((next_local_midnight(local_now) - local_now).total_seconds()))


def local_day_start_utc() -> datetime:
    """Return the UTC-aware datetime that corresponds to the most recent
    midnight in Asia/Taipei. Used to gate the per-user daily spending limit
    so that quotas reset at local 00:00 (UTC+8 → 16:00 UTC the previous day).
    """
    local_now = datetime.now(LOCAL_TZ)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc)


# ── Local (Asia/Taipei) calendar days and months ──
#
# Reports bucket usage by local calendar day and local month (the same day
# the daily quota resets on). ``usage_logs.created_at`` holds UTC wall-clock
# time, so a local day ``d`` covers ``[local_day_start_utc_of(d),
# local_day_start_utc_of(d + 1))`` in that column.

def local_today() -> date:
    """Today's date in Asia/Taipei."""
    return datetime.now(LOCAL_TZ).date()


def local_day_start_utc_of(day: date) -> datetime:
    """UTC-aware instant of local (Asia/Taipei) midnight starting ``day``."""
    return datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ).astimezone(timezone.utc)


def next_month_first_day(year: int, month: int) -> date:
    return date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)


def local_month_start() -> date:
    """First day of the current local (Asia/Taipei) month."""
    return local_today().replace(day=1)
