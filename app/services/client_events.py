"""What a setup tool reports about its user's machine: ``POST /v1/events``.

The gateway sees requests, not machines. A tool that installs or configures a
client for its user (a terminal tool, a desktop window; they live in their own
repositories) knows what it just did, so it says so: which account installed
or updated what, in which version, on which operating system. An admin reads
it on the *Clients* tab (*Client installs*): what is installed where now, and
what recently happened, failures included.

One event is one row of ``client_events``, appended and never changed::

    {"event": "installed", "tool": "gateway-tui", "tool_version": "0.3.1",
     "kind": "app", "component": "claude-code", "component_version": "2.1.310",
     "os": "windows", "os_version": "10.0.22631", "arch": "x64",
     "method": "npm", "hostname": "pc-0421", "ok": true}

- ``event``: one of ``EVENTS``. The vocabulary is code; an unknown one is
  refused, so a table an admin reads holds no made-up kinds.
- ``tool`` / ``tool_version``: the program reporting.
- ``kind`` / ``component`` / ``component_version``: what the event is about.
  ``kind`` is one of ``KINDS``: ``app`` (a program of its own: ``claude-code``,
  ``codex``, ``vscode``, ``claude-desktop``, the tool itself; the default) or
  something added to one, ``skill`` / ``mcp`` / ``plugin``, which then names
  that program in ``client`` (the same MCP server in Claude Code and in Claude
  Desktop is two installs). ``previous_version`` for an update.
- ``os``: one of ``OS_FAMILIES``; required, because the same account may have a
  Windows desktop and a Linux server. ``os_version`` and ``arch`` say which.
- ``method``: how it was installed (``npm``, ``native``, ``offline``, ...).
- ``hostname``: the machine's name, if the tool sends it: what an admin
  recognizes a machine by, and what keeps two machines of one account apart
  (together with ``os``: a WSL distribution carries its Windows host's name).
  Without it the account's machines of one OS count as one.
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
# What sort of thing a component is. ``app``: a program of its own (an
# editor, a CLI, a desktop application). The other three are added to a
# client, which the event names in ``client``.
KINDS: tuple[str, ...] = ("app", "skill", "mcp", "plugin")

# Events one account may report per hour. Setting a machine up is a burst:
# a few programs and a few dozen skills and MCP servers, an event each.
MAX_PER_HOUR = 300
# An install older than this is no longer listed (``installs``).
INSTALL_WINDOW_DAYS = 365

_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
# A component is named as its ecosystem names it: ``@scope/server-github``,
# ``anthropic-skills:pdf``.
_COMPONENT = re.compile(r"^[a-z0-9@][a-z0-9@._/:+-]{0,99}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,39}$")
_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9._-]{0,98}[a-z0-9])?$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MAX_OS_VERSION = 80
_MAX_MESSAGE = 500


# The fields kept as the client spelled them (names and versions): a key in
# one of them is refused, where the free text has it taken out.
_NAMES = (
    "tool", "tool_version", "component", "component_version", "previous_version", "client", "method", "arch",
    "hostname",
)


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
    kind = body.get("kind") or KINDS[0]
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}.")
    component = body.get("component")
    if component is None or component == "":
        raise ValueError("component is required.")
    if not isinstance(component, str) or not _COMPONENT.match(component.strip().lower()):
        raise ValueError(
            "component must be a name of letters, digits and '@', '.', '_', '/', ':', '+', '-', 100 characters at most."
        )
    hostname = body.get("hostname") or ""
    if not isinstance(hostname, str) or (hostname and not _HOSTNAME.match(hostname.strip().lower())):
        raise ValueError("hostname must be a machine name of letters, digits, '.', '_' or '-', 100 characters at most.")
    hostname = hostname.strip().lower()
    if secret and any(secret.lower() in str(body.get(key) or "").lower() for key in _NAMES):
        raise ValueError("An event must not contain the API key.")
    return {
        "event": event, "ok": ok,
        "tool": _slug(body, "tool", required=True), "tool_version": _version(body, "tool_version", required=True),
        "kind": kind, "component": component.strip().lower(), "client": _slug(body, "client"),
        "component_version": _version(body, "component_version"),
        "previous_version": _version(body, "previous_version"),
        "method": _slug(body, "method"),
        "os": os_family.strip().lower(), "os_version": _text(body, "os_version", _MAX_OS_VERSION, secret),
        "arch": _slug(body, "arch"),
        "hostname": hostname, "message": _text(body, "message", _MAX_MESSAGE, secret),
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

def machine_key(row: ClientEvent) -> tuple[str, str, str]:
    """One machine: the account, the machine's name and its OS. The OS is
    part of it because a WSL distribution reports its Windows host's name;
    without a name the account's machines of one OS count as one."""
    return (row.username, row.hostname, row.os)


def recent(session: Session, limit: int = 100) -> list[ClientEvent]:
    """The newest events, newest first."""
    return list(session.exec(
        select(ClientEvent).order_by(ClientEvent.created_at.desc(), ClientEvent.id.desc()).limit(limit)
    ).all())


# One thing installed in one place: a component of a kind, on a machine, and
# for a skill, an MCP server or a plugin the client it was added to.
_INSTALL_KEY = (
    ClientEvent.username, ClientEvent.hostname, ClientEvent.os,
    ClientEvent.kind, ClientEvent.component, ClientEvent.client,
)


def installs(session: Session, now: datetime | None = None) -> list[ClientEvent]:
    """What is installed now: per machine (``machine_key``), kind, component
    and client the last install or update that worked, unless an uninstall
    followed it or it is older than ``INSTALL_WINDOW_DAYS`` (a machine that
    is gone never says so). Newest first.

    The last event of each is picked in SQL (the highest id per key), so
    the answer does not depend on how many events there are."""
    now = now or _utcnow()
    last = (
        select(func.max(ClientEvent.id))
        .where(ClientEvent.event.in_(_INSTALL_EVENTS), ClientEvent.ok.is_(True))
        .group_by(*_INSTALL_KEY)
    )
    return list(session.exec(
        select(ClientEvent)
        .where(
            ClientEvent.id.in_(last), ClientEvent.event != "uninstalled",
            ClientEvent.created_at >= now - timedelta(days=INSTALL_WINDOW_DAYS),
        )
        .order_by(ClientEvent.id.desc())
    ).all())


def _newest_first(version: str) -> tuple:
    """Sort key: dotted numbers newest first, anything else after them."""
    try:
        return (0, tuple(-int(part) for part in version.lstrip("vV").split(".")), version)
    except ValueError:
        return (1, (), version)


def overview(session: Session, now: datetime | None = None, limit: int = 200) -> dict[str, Any]:
    """The *Client installs* card: how many machines per operating system,
    per kind and component how many accounts and machines have it and in
    which versions, and the newest ``limit`` installs themselves (``total``
    says how many there are)."""
    found = installs(session, now)
    by_os: dict[str, set[tuple]] = {}
    components: dict[tuple[str, str], dict[str, Any]] = {}
    for row in found:
        by_os.setdefault(row.os, set()).add(machine_key(row))
        entry = components.setdefault(
            (row.kind, row.component), {"accounts": set(), "machines": set(), "clients": set(), "versions": {}},
        )
        entry["accounts"].add(row.username)
        entry["machines"].add(machine_key(row))
        if row.client:
            entry["clients"].add(row.client)
        entry["versions"].setdefault(row.component_version, set()).add(machine_key(row))
    return {
        "installs": found[:limit], "total": len(found),
        "accounts": len({row.username for row in found}),
        "os": [{"os": name, "machines": len(by_os[name])} for name in OS_FAMILIES if name in by_os],
        "components": [
            {
                "kind": kind, "component": component,
                "accounts": len(entry["accounts"]), "machines": len(entry["machines"]),
                "clients": sorted(entry["clients"]),
                "versions": [
                    {"version": version, "machines": len(entry["versions"][version])}
                    for version in sorted(entry["versions"], key=_newest_first)
                ],
            }
            for (kind, component), entry in sorted(
                components.items(), key=lambda kv: (KINDS.index(kv[0][0]) if kv[0][0] in KINDS else len(KINDS), kv[0][1]),
            )
        ],
        "days": INSTALL_WINDOW_DAYS,
    }
