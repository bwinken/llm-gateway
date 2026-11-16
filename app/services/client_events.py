"""What a setup tool reports about its user's machine: ``POST /v1/events``.

The gateway sees requests, not machines. A tool that installs or configures a
client for its user (a terminal tool, a desktop window; they live in their own
repositories) knows what it just did, so it says so: which account installed
or updated what, in which version, on which operating system. An admin reads
it on the *Clients* tab (*Client installs*): what is installed where now, and
what recently happened, failures included.

One event is one row of ``client_events``, appended and never changed::

    {"event": "installed", "tool": "gateway-tui", "tool_version": "0.3.1",
     "component": "claude-code", "component_version": "2.1.310",
     "os": "windows", "os_version": "10.0.22631", "arch": "x64",
     "method": "npm", "machine_id": "5d0c1e0a-...", "ok": true}

- ``event``: one of ``EVENTS``. The vocabulary is code; an unknown one is
  refused, so a table an admin reads holds no made-up kinds.
- ``tool`` / ``tool_version``: the program reporting.
- ``component`` / ``component_version``: what the event is about (``claude-code``,
  ``node``, ``git``, or the tool itself). ``previous_version`` for an update.
- ``os``: one of ``OS_FAMILIES``; required, because the same account may have a
  Windows desktop and a Linux server. ``os_version`` and ``arch`` say which.
- ``method``: how it was installed (``npm``, ``native``, ``offline``, ...).
- ``machine_id``: an id the tool made up once and kept on that machine, so two
  machines of one account stay apart. Opaque: not a hostname, not an address.
- ``ok`` false: the attempt failed; ``message`` says why.

Everything is text the client sent. It is validated to short slugs and bounded
text (``clean``), shown escaped, and never decides anything: nothing routes,
limits or refuses by it, and it is no Prometheus label. An account may report
``MAX_PER_HOUR`` events an hour, so a key cannot fill the table. ``username``
is not a foreign key: deleting an account leaves its rows.

This module opens no session of its own (``deps.record_client_event`` does).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import Session, func, select

from app.models.schema import ClientEvent

# What a tool can report. A new kind is added here, never by a client.
EVENTS: tuple[str, ...] = ("installed", "updated", "uninstalled", "settings_applied")
# The events that change what is installed on a machine (``installs``).
_INSTALL_EVENTS = frozenset({"installed", "updated", "uninstalled"})
OS_FAMILIES: tuple[str, ...] = ("windows", "linux", "macos")

# Events one account may report per hour.
MAX_PER_HOUR = 60
# How far back ``installs`` looks, and how many rows at most.
INSTALL_WINDOW_DAYS = 365
_INSTALL_ROWS = 20000

_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,39}$")
_MACHINE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MAX_OS_VERSION = 80
_MAX_MESSAGE = 500


class TooManyEvents(Exception):
    """The account reported ``MAX_PER_HOUR`` events in the last hour."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _slug(body: dict, key: str, required: bool = False) -> str:
    value = body.get(key)
    if value is None or value == "":
        if required:
            raise ValueError(f"{key} is required.")
        return ""
    if not isinstance(value, str) or not _SLUG.match(value.strip().lower()):
        raise ValueError(f"{key} must be a short name: letters, digits, '.', '_' or '-', 40 characters at most.")
    return value.strip().lower()


def _version(body: dict, key: str, required: bool = False) -> str:
    value = body.get(key)
    if value is None or value == "":
        if required:
            raise ValueError(f"{key} is required.")
        return ""
    if not isinstance(value, str) or not _VERSION.match(value.strip()):
        raise ValueError(f"{key} must be a version such as 2.1.310 (40 characters at most).")
    return value.strip()


def _text(body: dict, key: str, limit: int, secret: str = "") -> str:
    value = body.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text.")
    if secret:
        value = value.replace(secret, "<redacted>")
    return _CONTROL.sub(" ", value).strip()[:limit]


def clean(body: Any, secret: str = "") -> dict[str, Any]:
    """A reported event as column values. Raises ValueError with what to
    fix. Keys this gateway does not know are ignored, so a newer tool can
    say more than an older gateway keeps. ``secret`` (the caller's key) is
    taken out of the free text: an error message may quote a command line."""
    if not isinstance(body, dict):
        raise ValueError("Send one event as a JSON object.")
    event = body.get("event")
    if event not in EVENTS:
        raise ValueError(f"event must be one of: {', '.join(EVENTS)}.")
    os_family = body.get("os")
    if not isinstance(os_family, str) or os_family.strip().lower() not in OS_FAMILIES:
        raise ValueError(f"os must be one of: {', '.join(OS_FAMILIES)}.")
    ok = body.get("ok", True)
    if not isinstance(ok, bool):
        raise ValueError("ok must be true or false.")
    machine_id = body.get("machine_id") or ""
    if not isinstance(machine_id, str) or (machine_id and not _MACHINE.match(machine_id)):
        raise ValueError("machine_id must be an id of letters, digits, '.', '_' or '-', 64 characters at most.")
    return {
        "event": event, "ok": ok,
        "tool": _slug(body, "tool", required=True), "tool_version": _version(body, "tool_version", required=True),
        "component": _slug(body, "component", required=True),
        "component_version": _version(body, "component_version"),
        "previous_version": _version(body, "previous_version"),
        "method": _slug(body, "method"),
        "os": os_family.strip().lower(), "os_version": _text(body, "os_version", _MAX_OS_VERSION, secret),
        "arch": _slug(body, "arch"),
        "machine_id": machine_id, "message": _text(body, "message", _MAX_MESSAGE, secret),
    }


def record(session: Session, username: str, values: dict[str, Any], now: datetime | None = None) -> ClientEvent:
    """Append one event of ``username`` (``values`` from ``clean``). Raises
    ``TooManyEvents`` past ``MAX_PER_HOUR``."""
    now = now or _utcnow()
    recent = session.exec(
        select(func.count()).select_from(ClientEvent)
        .where(ClientEvent.username == username, ClientEvent.created_at > now - timedelta(hours=1))
    ).one()
    if recent >= MAX_PER_HOUR:
        raise TooManyEvents(username)
    row = ClientEvent(username=username, created_at=now, **values)
    session.add(row)
    session.commit()
    return row


# ── Admin: what is installed where, and what happened ──

def recent(session: Session, limit: int = 100) -> list[ClientEvent]:
    """The newest events, newest first."""
    return list(session.exec(
        select(ClientEvent).order_by(ClientEvent.created_at.desc(), ClientEvent.id.desc()).limit(limit)
    ).all())


def installs(session: Session, now: datetime | None = None) -> list[ClientEvent]:
    """What is installed now: per account, machine and component the last
    install or update that worked, unless an uninstall followed it. Newest
    first. Read from the events of the last ``INSTALL_WINDOW_DAYS`` days."""
    now = now or _utcnow()
    rows = session.exec(
        select(ClientEvent)
        .where(
            ClientEvent.created_at >= now - timedelta(days=INSTALL_WINDOW_DAYS),
            ClientEvent.event.in_(_INSTALL_EVENTS), ClientEvent.ok.is_(True),
        )
        .order_by(ClientEvent.created_at.desc(), ClientEvent.id.desc()).limit(_INSTALL_ROWS)
    ).all()
    latest: dict[tuple[str, str, str], ClientEvent] = {}
    for row in rows:                                   # newest first: the first seen per key is its state
        latest.setdefault((row.username, row.machine_id, row.component), row)
    return [row for row in latest.values() if row.event != "uninstalled"]


def _newest_first(item: tuple[tuple[str, str], set[str]]) -> tuple:
    """Components by name, each one's versions newest first (numerically)."""
    (component, version), _ = item
    try:
        numbers = tuple(-int(part) for part in version.lstrip("vV").split("."))
    except ValueError:
        numbers = (1,)                       # not a dotted number: after the ones that are
    return (component, numbers, version)


def overview(session: Session, now: datetime | None = None) -> dict[str, Any]:
    """The *Client installs* card: the installs, and how many machines and
    accounts they are on per operating system and per component version."""
    found = installs(session, now)
    by_os: dict[str, set[tuple[str, str]]] = {}
    by_version: dict[tuple[str, str], set[str]] = {}
    for row in found:
        by_os.setdefault(row.os, set()).add((row.username, row.machine_id))
        by_version.setdefault((row.component, row.component_version), set()).add(row.username)
    return {
        "installs": found,
        "accounts": len({row.username for row in found}),
        "os": [{"os": name, "machines": len(by_os[name])} for name in OS_FAMILIES if name in by_os],
        "versions": [
            {"component": component, "version": version, "usernames": sorted(names), "count": len(names)}
            for (component, version), names in sorted(by_version.items(), key=_newest_first)
        ],
        "days": INSTALL_WINDOW_DAYS,
    }
