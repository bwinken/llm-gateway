"""Lock out client addresses that try many different invalid API keys.

Guessing keys means sending many *different* wrong keys. A misconfigured
client (a stale key in a cron job, Claude Code retrying) sends the *same*
wrong key over and over. So an address is locked only after it has sent
``AUTH_FAIL_DISTINCT_KEYS`` distinct invalid keys within
``AUTH_FAIL_WINDOW_S``: one broken client behind an office NAT never locks
out its colleagues, while enumeration is cut off after a handful of guesses.

While locked, every API-key request from that address gets a 429 before the
key is looked up, valid or not (a lookup would let a guesser keep testing).
The lock lasts ``AUTH_LOCKOUT_S``.

State is per worker and in memory: with N workers a guesser gets up to N times
the budget before every worker has locked it, and a restart forgets locks.
Only digests of the failed keys are kept, never the keys.

Loopback addresses are never locked: if uvicorn's proxy headers are not
trusted, every request appears to come from nginx on 127.0.0.1 and one lock
would refuse everyone. ``AUTH_FAIL_DISTINCT_KEYS=0`` turns the throttle off.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import threading
import time

from app.core.logger import logger


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


DISTINCT_KEYS = _int_env("AUTH_FAIL_DISTINCT_KEYS", 10)
WINDOW_S = _int_env("AUTH_FAIL_WINDOW_S", 600)
LOCKOUT_S = _int_env("AUTH_LOCKOUT_S", 900)
# Bound on tracked addresses, so a spray from many addresses can't grow memory
# without limit. The entries touched longest ago are dropped first.
MAX_TRACKED = 10_000

_lock = threading.Lock()
# address -> {key digest: last seen (monotonic)}
_failures: dict[str, dict[str, float]] = {}
# address -> locked until (monotonic)
_locked_until: dict[str, float] = {}


def enabled() -> bool:
    return DISTINCT_KEYS > 0


def _exempt(address: str | None) -> bool:
    if not address:
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:16]


def retry_after(address: str | None, now: float | None = None) -> int | None:
    """Seconds until ``address`` may try again, or None when it is not locked."""
    if not enabled() or _exempt(address):
        return None
    now = time.monotonic() if now is None else now
    with _lock:
        until = _locked_until.get(address)  # type: ignore[arg-type]
        if until is None:
            return None
        if until <= now:
            del _locked_until[address]  # type: ignore[arg-type]
            return None
        return max(1, int(until - now + 0.999))


def record_failure(address: str | None, keys: list[str], now: float | None = None) -> bool:
    """Note invalid ``keys`` from ``address``. True when this locks it."""
    if not enabled() or _exempt(address) or not keys:
        return False
    now = time.monotonic() if now is None else now
    with _lock:
        seen = _failures.pop(address, {})  # type: ignore[arg-type]
        cutoff = now - WINDOW_S
        seen = {d: t for d, t in seen.items() if t > cutoff}
        for key in keys:
            seen[_digest(key)] = now
        if len(seen) >= DISTINCT_KEYS:
            _locked_until[address] = now + LOCKOUT_S  # type: ignore[index]
            distinct = len(seen)
            locked = True
        else:
            _failures[address] = seen  # type: ignore[index]  # re-inserted: most recent last
            locked = False
        while len(_failures) > MAX_TRACKED:
            _failures.pop(next(iter(_failures)))
        while len(_locked_until) > MAX_TRACKED:
            _locked_until.pop(next(iter(_locked_until)))
    if locked:
        logger.warning(
            "Auth throttled | client={} distinct_invalid_keys={} window_s={} lockout_s={}",
            address, distinct, WINDOW_S, LOCKOUT_S,
        )
    return locked


def reset() -> None:
    """Forget every failure and lock (tests)."""
    with _lock:
        _failures.clear()
        _locked_until.clear()
