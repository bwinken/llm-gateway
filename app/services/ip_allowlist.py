"""API IP allowlist: which accounts may call the API from which addresses.

Rules live in ``ip_allow_rules``. Each has an address range (``cidr``), an
optional window (``starts_at`` / ``expires_at``; no end = permanent) and who
it admits: ``usernames`` and ``org_codes`` both empty means every account,
otherwise the account's username must be listed or its ``org_code`` must be
among the listed org codes (compared case-insensitively). A request is
admitted when at least one active rule covers its address **and** its
account. For example:

* 10.0.0.0/8, everyone, permanent: the office network.
* 203.0.113.0/24, alice + bob, until Friday: a partner site, for two people.
* 198.51.100.0/24, org code RD-7: a lab, for one department.

The mode is ``[app].ip_allowlist_mode``:

* ``off`` (default): no check.
* ``monitor``: a request no rule admits is served and logged
  (``IP allowlist would refuse (monitor)``, once per account and address
  per minute), with the reason: no rule for that address at all, or rules
  for it but none for that account.
* ``enforce``: it gets 403.

The check needs the account, so it runs inside API-key auth
(``deps._authenticate``, right after the key is resolved), which every
``/v1``, ``/azure`` and ``/aws`` route goes through (``TestEveryApiRouteIsChecked``).
The web UI, OAuth and ``/healthz`` / ``/readyz`` never authenticate with an
API key, so they are never checked and an admin can't lock themselves out of
the page that edits the list. A request without a valid key gets the usual
401, whatever its address. Only admins add, edit and end rules; users have
no way to open access themselves.

The client address is ``request.client.host``. Behind nginx that is the
address uvicorn's proxy-header middleware took from ``X-Forwarded-For``
(trusting only ``--forwarded-allow-ips``, 127.0.0.1 by default); this module
never parses the header itself, so a client cannot spoof it.

Every worker keeps an in-memory snapshot of the active and upcoming rules,
reloaded every ``REFRESH_S`` seconds (``refresh_loop``) and right after a
change made through this worker. The request path never touches the
database. A rule's own window is checked against the clock on every request,
so a preset end time is exact; a new rule, an edit, or a rule ended early
reaches the other workers within ``REFRESH_S``. A failed reload keeps the
previous snapshot. Only the start of a request is checked: a stream that
began inside a window runs to the end.
"""

from __future__ import annotations

import asyncio
import ipaddress
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.core.config import get_ip_allowlist_settings
from app.core.database import engine
from app.core.logger import logger
from app.models.schema import IpAllowRule, User
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
    usernames: frozenset[str]
    org_codes: frozenset[str]  # casefolded

    def active(self, now: datetime) -> bool:
        return (self.starts_at is None or self.starts_at <= now) and (
            self.expires_at is None or now < self.expires_at
        )

    def admits(self, username: str, org_code: str) -> bool:
        if not self.usernames and not self.org_codes:
            return True
        return username in self.usernames or bool(org_code and org_code.casefold() in self.org_codes)


_snapshot: tuple[_Rule, ...] = ()
_loaded = False
_last_logged: dict[tuple[str, str], float] = {}


def utcnow() -> datetime:
    """Naive UTC, the convention of ``ip_allow_rules``."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def settings() -> dict:
    """``[app].ip_allowlist_mode`` (``config.get_ip_allowlist_settings``).
    Every reader goes through here, so a test patches one name."""
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


# ── Who a rule admits ──

def split_names(text: str) -> list[str]:
    """``"alice, bob\\ncarol"`` → ``["alice", "bob", "carol"]`` (order kept,
    duplicates dropped)."""
    out: list[str] = []
    for part in (text or "").replace("\n", ",").split(","):
        name = part.strip()
        if name and name not in out:
            out.append(name)
    return out


def validate_who(session: Session, usernames: list[str], org_codes: list[str]) -> tuple[list[str], list[str]]:
    """Check that every username is an account and every org code belongs to
    at least one account (so a typo can't silently admit nobody). Returns the
    org codes spelled as the accounts carry them. Raises ValueError."""
    if usernames:
        known = set(session.exec(select(User.username).where(User.username.in_(usernames))).all())  # type: ignore[attr-defined]
        unknown = [n for n in usernames if n not in known]
        if unknown:
            raise ValueError(f"Unknown account(s): {', '.join(unknown)}.")
    canonical: list[str] = []
    if org_codes:
        wanted = {c.casefold(): c for c in org_codes}
        found = {
            str(code).casefold(): str(code)
            for code in session.exec(
                select(User.org_code).where(func.lower(User.org_code).in_([c.lower() for c in org_codes])).distinct()
            ).all()
        }
        unknown = [c for k, c in wanted.items() if k not in found]
        if unknown:
            raise ValueError(f"No account has org code(s): {', '.join(unknown)}.")
        canonical = [found[k] for k in wanted]
    return usernames, canonical


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
        rules.append(_Rule(
            network, row.starts_at, row.expires_at,
            frozenset(str(u) for u in (row.usernames or [])),
            frozenset(str(c).casefold() for c in (row.org_codes or [])),
        ))
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


def verdict(addr: IPAddress | None, username: str, org_code: str = "", now: datetime | None = None) -> str:
    """``"allowed"``, ``"not_for_this_account"`` (active rules cover the
    address, none admits the account) or ``"no_rule_for_address"``."""
    if addr is None:
        return "no_rule_for_address"
    now = now or utcnow()
    covering = [r for r in _snapshot if r.active(now) and addr in r.network]
    if not covering:
        return "no_rule_for_address"
    if any(r.admits(username, org_code) for r in covering):
        return "allowed"
    return "not_for_this_account"


def is_allowed(addr: IPAddress | None, username: str, org_code: str = "", now: datetime | None = None) -> bool:
    return verdict(addr, username, org_code, now) == "allowed"


# ── The check inside API-key auth ──

def _log_refusal(username: str, addr_text: str, mode: str, reason: str, path: str) -> None:
    now = time.monotonic()
    key = (username, addr_text)
    last = _last_logged.get(key)
    if last is not None and now - last < _LOG_EVERY_S:
        return
    if len(_last_logged) > 10_000:
        _last_logged.clear()
    _last_logged[key] = now
    logger.warning(
        "IP allowlist would refuse ({}) | user={} ip={} reason={} path={}{}",
        mode, username, addr_text, reason, path, "" if _loaded else " (allowlist not loaded yet)",
    )


def check(request: Request, user: User) -> None:
    """Called by ``deps._authenticate`` once the API key is resolved. From
    memory only (no DB). Raises 403 in ``enforce`` mode."""
    mode = settings()["mode"]
    if mode == "off":
        return
    addr = client_address(request)
    result = verdict(addr, user.username, user.org_code or "")
    if result == "allowed":
        return
    addr_text = str(addr) if addr is not None else str(request.client.host if request.client else "")
    _log_refusal(user.username, addr_text, mode, result, request.url.path)
    if mode == "enforce":
        metrics.observe_rejection("ip_allowlist")
        raise HTTPException(
            status_code=403,
            detail=(
                f"This account may not use the API from {addr_text or 'this address'}. "
                "Ask an administrator for access."
            ),
        )


# ── Reads and writes for the admin panel ──

def _network_key(rule: IpAllowRule) -> tuple:
    try:
        net = ipaddress.ip_network(rule.cidr, strict=False)
        return (net.version, int(net.network_address), net.prefixlen)
    except ValueError:
        return (99, 0, 0)


def list_rules(session: Session, now: datetime | None = None, expired_days: int = 7) -> dict[str, list[IpAllowRule]]:
    """``{"active", "upcoming", "expired"}``. Active and upcoming rules are
    sorted by address, then start, so the several rules one range can have
    (different people, different windows) sit together; the expired ones
    of the last ``expired_days`` days come newest first, as the record of
    recent access."""
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
    for key in ("active", "upcoming"):
        out[key].sort(key=lambda r: (_network_key(r), r.starts_at or datetime.min, r.id))
    out["expired"].sort(key=lambda r: r.expires_at, reverse=True)
    return out


def add_rule(
    session: Session,
    cidr: str,
    *,
    label: str = "",
    usernames: list[str] | None = None,
    org_codes: list[str] | None = None,
    created_by: str,
    starts_at: datetime | None = None,
    expires_at: datetime | None = None,
    now: datetime | None = None,
) -> IpAllowRule:
    """Validate and insert a rule. Raises ValueError with a message for the
    admin on a bad address, window, account or org code."""
    now = now or utcnow()
    normalized = normalize_cidr(cidr)
    if expires_at is not None:
        if expires_at <= now:
            raise ValueError("The end time is already in the past.")
        if starts_at is not None and expires_at <= starts_at:
            raise ValueError("The end time must be after the start time.")
    names, codes = validate_who(session, list(usernames or []), list(org_codes or []))
    rule = IpAllowRule(
        cidr=normalized, label=label.strip()[:200], usernames=names, org_codes=codes,
        created_by=created_by, starts_at=starts_at, expires_at=expires_at, created_at=now,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def set_who(session: Session, rule: IpAllowRule, usernames: list[str], org_codes: list[str]) -> None:
    """Replace who ``rule`` admits (both empty = everyone). Raises ValueError."""
    names, codes = validate_who(session, usernames, org_codes)
    rule.usernames, rule.org_codes = names, codes
    session.add(rule)
    session.commit()
    session.refresh(rule)


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
