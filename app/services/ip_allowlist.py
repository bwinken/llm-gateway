"""API IP allowlist: which client addresses may call the API surfaces.

Rules live in ``ip_allow_rules`` (one CIDR each, optionally time-limited);
the mode is ``[app].ip_allowlist_mode``:

* ``off`` (default): no check.
* ``monitor``: a request from outside the allowlist is served and logged
  (``IP not on allowlist (monitor)``, once per address per minute).
* ``enforce``: it gets 403 before authentication.

Only the API routers (``v1_api``, ``azure_api``, ``aws_api``) carry the check
(``require_allowed_ip``, a router-level dependency), so the web UI, OAuth and
``/healthz`` / ``/readyz`` stay reachable: an admin can never lock themselves
out of the page that edits the list. Only admins add or end rules (admin
panel); users have no way to open access themselves.

The client address is ``request.client.host``. Behind nginx that is the
address uvicorn's proxy-header middleware took from ``X-Forwarded-For``
(trusting only ``--forwarded-allow-ips``, 127.0.0.1 by default); this module
never parses the header itself, so a client cannot spoof it.

Every worker keeps an in-memory snapshot of the active and upcoming rules,
reloaded every ``REFRESH_S`` seconds (``refresh_loop``) and right after a
change made through this worker. The request path never touches the
database. A rule's own window is checked against the clock on every request,
so a preset end time is exact; a new rule, or one ended early, reaches the
other workers within ``REFRESH_S``. A failed reload keeps the previous
snapshot. Only the start of a request is checked: a stream that began inside
a window runs to the end.
"""

from __future__ import annotations

import asyncio
import ipaddress
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from sqlalchemy import or_
from sqlmodel import Session, select

from app.core.config import get_ip_allowlist_settings
from app.core.database import engine
from app.core.logger import logger
from app.models.schema import IpAllowRule
from app.services import metrics

REFRESH_S = 15.0
_LOG_EVERY_S = 60.0

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class _Rule:
    network: IPNetwork
    starts_at: datetime | None
    expires_at: datetime | None

    def active(self, now: datetime) -> bool:
        return (self.starts_at is None or self.starts_at <= now) and (
            self.expires_at is None or now < self.expires_at
        )


_snapshot: tuple[_Rule, ...] = ()
_loaded = False
_last_logged: dict[str, float] = {}


def utcnow() -> datetime:
    """Naive UTC, the convention of ``ip_allow_rules``."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def settings() -> dict:
    """``[app].ip_allowlist_*`` (``config.get_ip_allowlist_settings``). The
    routers read it through here, so a test patches one name."""
    return get_ip_allowlist_settings()


# ── Addresses ──

def parse_address(text: str | None) -> IPAddress | None:
    """An IP address, IPv4-mapped IPv6 (``::ffff:10.0.0.1``) unwrapped to
    IPv4 so IPv4 rules match it. None for anything that is not an address
    (``testclient``, a Unix socket, an empty value)."""
    if not text:
        return None
    try:
        addr = ipaddress.ip_address(text.strip())
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def client_address(request: Request) -> IPAddress | None:
    return parse_address(request.client.host if request.client else None)


def normalize_cidr(text: str) -> str:
    """``"203.0.113.5"`` → ``"203.0.113.5/32"``, ``"10.1.2.3/8"`` →
    ``"10.0.0.0/8"``. Raises ValueError on anything else, and on a /0
    (allowing every address is ``mode = off``, not a rule)."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Enter an IP address or a CIDR range.")
    try:
        network = ipaddress.ip_network(raw, strict=False)
    except ValueError:
        raise ValueError(f"{raw!r} is not an IP address or CIDR range (e.g. 203.0.113.5 or 10.0.0.0/8).")
    if isinstance(network, ipaddress.IPv6Network) and network.network_address.ipv4_mapped is not None:
        mapped = network.network_address.ipv4_mapped
        network = ipaddress.ip_network(f"{mapped}/{max(network.prefixlen - 96, 0)}", strict=False)
    if network.prefixlen == 0:
        raise ValueError("A /0 rule would allow every address; set the mode to Off instead.")
    return str(network)



# ── Snapshot ──

def _load(session: Session, now: datetime) -> tuple[_Rule, ...]:
    rows = session.exec(
        select(IpAllowRule).where(or_(
            IpAllowRule.expires_at.is_(None),  # type: ignore[union-attr]
            IpAllowRule.expires_at > now,  # type: ignore[operator]
        ))
    ).all()
    rules = []
    for row in rows:
        try:
            network = ipaddress.ip_network(row.cidr, strict=False)
        except ValueError:
            logger.warning("IP allowlist: skipping rule {} with invalid cidr {!r}", row.id, row.cidr)
            continue
        rules.append(_Rule(network, row.starts_at, row.expires_at))
    return tuple(rules)


def refresh(bind=None) -> bool:
    """Reload this worker's snapshot. Blocking; never raises. On failure the
    previous snapshot stays in force."""
    global _snapshot, _loaded
    try:
        with Session(bind or engine) as session:
            _snapshot = _load(session, utcnow())
        _loaded = True
        return True
    except Exception as exc:
        logger.warning("IP allowlist refresh failed | error={}: {}", type(exc).__name__, exc)
        return False


def reset() -> None:
    """Forget the snapshot (tests)."""
    global _snapshot, _loaded
    _snapshot, _loaded = (), False
    _last_logged.clear()


async def refresh_loop(interval: float = REFRESH_S) -> None:
    """Background task, one per worker (started from the lifespan)."""
    while True:
        await asyncio.sleep(interval)
        await asyncio.to_thread(refresh)


def is_allowed(addr: IPAddress | None, now: datetime | None = None) -> bool:
    if addr is None:
        return False
    now = now or utcnow()
    return any(rule.active(now) and addr in rule.network for rule in _snapshot)


# ── The check on the API routers ──

def _log_outside(addr_text: str, mode: str, path: str) -> None:
    now = time.monotonic()
    last = _last_logged.get(addr_text)
    if last is not None and now - last < _LOG_EVERY_S:
        return
    if len(_last_logged) > 10_000:
        _last_logged.clear()
    _last_logged[addr_text] = now
    logger.warning(
        "IP not on allowlist ({}) | ip={} path={}{}",
        mode, addr_text, path, "" if _loaded else " (allowlist not loaded yet)",
    )


async def require_allowed_ip(request: Request) -> None:
    """Router-level dependency of the API routers. Runs before API-key auth,
    from memory only (no DB, no thread)."""
    mode = settings()["mode"]
    if mode == "off":
        return
    addr = client_address(request)
    if is_allowed(addr):
        return
    addr_text = str(addr) if addr is not None else str(request.client.host if request.client else "")
    _log_outside(addr_text, mode, request.url.path)
    if mode == "enforce":
        metrics.observe_rejection("ip_allowlist")
        raise HTTPException(
            status_code=403,
            detail=(
                f"Your IP address ({addr_text or 'unknown'}) is not on this gateway's allowlist. "
                "Ask an administrator for access."
            ),
        )


# ── Reads and writes for the admin panel ──

def list_rules(session: Session, now: datetime | None = None, expired_days: int = 7) -> dict[str, list[IpAllowRule]]:
    """``{"active", "upcoming", "expired"}``: the expired ones of the last
    ``expired_days`` days, newest first, as the record of recent access."""
    now = now or utcnow()
    rows = session.exec(
        select(IpAllowRule).where(or_(
            IpAllowRule.expires_at.is_(None),  # type: ignore[union-attr]
            IpAllowRule.expires_at > now - timedelta(days=expired_days),  # type: ignore[operator]
        )).order_by(IpAllowRule.id)
    ).all()
    out: dict[str, list[IpAllowRule]] = {"active": [], "upcoming": [], "expired": []}
    for row in rows:
        if row.expires_at is not None and row.expires_at <= now:
            out["expired"].append(row)
        elif row.starts_at is not None and row.starts_at > now:
            out["upcoming"].append(row)
        else:
            out["active"].append(row)
    out["expired"].sort(key=lambda r: r.expires_at, reverse=True)
    return out


def add_rule(
    session: Session,
    cidr: str,
    *,
    label: str = "",
    created_by: str,
    starts_at: datetime | None = None,
    expires_at: datetime | None = None,
    now: datetime | None = None,
) -> IpAllowRule:
    """Validate and insert a rule. Raises ValueError with a message for the
    admin on a bad address or window."""
    now = now or utcnow()
    normalized = normalize_cidr(cidr)
    if expires_at is not None:
        if expires_at <= now:
            raise ValueError("The end time is already in the past.")
        if starts_at is not None and expires_at <= starts_at:
            raise ValueError("The end time must be after the start time.")
    rule = IpAllowRule(
        cidr=normalized, label=label.strip()[:200], created_by=created_by,
        starts_at=starts_at, expires_at=expires_at, created_at=now,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def end_rule(session: Session, rule: IpAllowRule, now: datetime | None = None) -> None:
    """End a rule now. It is kept (as an expired rule) for the record;
    a rule that never started is simply deleted."""
    now = now or utcnow()
    if rule.starts_at is not None and rule.starts_at > now:
        session.delete(rule)
    else:
        rule.expires_at = now
        session.add(rule)
    session.commit()
