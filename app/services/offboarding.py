"""People whose sign-in was disabled at the identity provider (leavers).

The gateway checks a token offline and an API key against its own database,
so nothing tells it that a person left: their key keeps working until an
admin disables the account. This job asks Keycloak who is disabled and
either tells the admin (``monitor``) or disables the account (``enforce``).

    OFFBOARDING_MODE = off (default) | monitor | enforce

- ``monitor``: a person Keycloak reports as disabled gets
  ``users.sso_disabled_at``. The admin page lists them under *Needs
  attention* until an admin disables the account; nothing else changes.
- ``enforce``: the same, and the account is disabled (``is_disabled``),
  with a row in the audit log. At most ``OFFBOARDING_MAX_PER_RUN`` accounts
  a run: above that nothing is disabled and the admin is told, because a
  list that long is more likely a mistake at the source than a real day.

It is independent of ``SSO_PROVIDER``: Keycloak is asked with a client of
its own (client credentials, ``realm-management`` → ``view-users``), whoever
signs the web UI's tokens. Accounts are matched by username, without regard
to case.

What it never does:

- act on anything but an explicit "disabled" from Keycloak. A person
  Keycloak does not know, a failed read or an empty answer flags nobody, so
  a wrong URL or a username scheme that differs cannot disable anyone;
- touch a service account (it has no sign-in to disable), delete an account
  (usage is the billing record), or enable one: a person who comes back is
  enabled again by an admin. Their mark clears once Keycloak reports them
  enabled.

The read blocks (HTTP), so it runs in the job's thread and before the
session's first query: no pooled connection is held while Keycloak answers.
"""

from __future__ import annotations

import os
import ssl
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlmodel import Session, select

from app.core.logger import logger
from app.models.schema import ScheduledJob, User, service_account_clause
from app.services import audit
from app.services.scheduler import Job, env_seconds

JOB_NAME = "offboarding"
MODES: tuple[str, ...] = ("off", "monitor", "enforce")
DEFAULT_INTERVAL_S = 3600.0
# Above this many accounts to disable in one run, ``enforce`` disables none.
DEFAULT_MAX_PER_RUN = 20
_TIMEOUT_S = 15.0
_PAGE = 200
# 200 pages of 200: a realm with more disabled users than that is not one
# this read was written for, and an endless loop must not be possible.
_MAX_PAGES = 200


class OffboardingError(RuntimeError):
    """Keycloak could not be read, or its answer cannot be relied on."""


# ---------------------------------------------------------------------------
# Settings (.env only)
# ---------------------------------------------------------------------------

def settings() -> dict[str, Any]:
    """Read from the environment on each call. A mode that is not one of
    ``MODES`` is ``off``: a typo never turns it on."""
    raw_mode = os.getenv("OFFBOARDING_MODE", "").strip().lower()
    mode = raw_mode if raw_mode in MODES else "off"
    if raw_mode and raw_mode not in MODES:
        logger.warning("Invalid OFFBOARDING_MODE={!r}; it stays off", raw_mode)
    try:
        max_per_run = int(os.getenv("OFFBOARDING_MAX_PER_RUN", "").strip() or DEFAULT_MAX_PER_RUN)
    except ValueError:
        max_per_run = DEFAULT_MAX_PER_RUN
    return {
        "mode": mode,
        # The realm URL: https://<keycloak>/realms/<realm>. The same value as
        # KEYCLOAK_ISSUER_URL, which is used when this one is not set.
        "realm_url": (
            os.getenv("OFFBOARDING_KEYCLOAK_URL", "").strip() or os.getenv("KEYCLOAK_ISSUER_URL", "").strip()
        ).rstrip("/"),
        "client_id": os.getenv("OFFBOARDING_KEYCLOAK_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("OFFBOARDING_KEYCLOAK_CLIENT_SECRET", "").strip(),
        "ca_file": os.getenv("OFFBOARDING_KEYCLOAK_CA_FILE", "").strip() or os.getenv("KEYCLOAK_CA_FILE", "").strip(),
        "max_per_run": max(max_per_run, 0),
    }


def missing_settings(s: dict[str, Any] | None = None) -> list[str]:
    """Names of the settings the read cannot work without."""
    s = s or settings()
    names = {
        "realm_url": "OFFBOARDING_KEYCLOAK_URL",
        "client_id": "OFFBOARDING_KEYCLOAK_CLIENT_ID",
        "client_secret": "OFFBOARDING_KEYCLOAK_CLIENT_SECRET",
    }
    return [name for key, name in names.items() if not s[key]]


def admin_url(realm_url: str) -> str:
    """The realm's Admin API base: ``.../realms/x`` → ``.../admin/realms/x``
    (also behind a path prefix such as the older ``/auth``)."""
    head, sep, realm = realm_url.rpartition("/realms/")
    if not sep or not realm or "/" in realm:
        raise OffboardingError("OFFBOARDING_KEYCLOAK_URL must be the realm URL: https://<keycloak>/realms/<realm>")
    return f"{head}/admin/realms/{realm}"


# ---------------------------------------------------------------------------
# Reading Keycloak
# ---------------------------------------------------------------------------

def _http(s: dict[str, Any]) -> httpx.Client:
    verify: Any = ssl.create_default_context(cafile=s["ca_file"]) if s["ca_file"] else True
    return httpx.Client(timeout=_TIMEOUT_S, verify=verify)


def _json(resp: httpx.Response, what: str) -> Any:
    if resp.status_code >= 400:
        # The status only: the body of a failed token request may echo the
        # client's credentials back.
        raise OffboardingError(f"Keycloak refused {what}: HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise OffboardingError(f"Keycloak's answer to {what} is not JSON") from exc


def read_disabled(s: dict[str, Any] | None = None) -> set[str]:
    """The usernames Keycloak reports as disabled, lower-cased. Blocking.

    Raises ``OffboardingError`` whenever the answer cannot be relied on,
    and an empty set is relied on: so before it is believed, the client
    must be seen to read users at all (one with ``query-users`` but not
    ``view-users`` is answered an empty list, not a refusal).
    """
    s = s or settings()
    missing = missing_settings(s)
    if missing:
        raise OffboardingError("not set: " + ", ".join(missing))
    users_url = admin_url(s["realm_url"]) + "/users"
    try:
        with _http(s) as http:
            token = _json(http.post(
                f"{s['realm_url']}/protocol/openid-connect/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": s["client_id"],
                    "client_secret": s["client_secret"],
                },
            ), "the client's sign-in").get("access_token")
            if not isinstance(token, str) or not token:
                raise OffboardingError("Keycloak gave the client no access token")
            auth = {"Authorization": f"Bearer {token}"}

            probe = _json(http.get(users_url, params={"first": 0, "max": 1}, headers=auth), "the user list")
            if not isinstance(probe, list) or not probe:
                raise OffboardingError(
                    "the client sees no users in the realm: give it realm-management → view-users"
                )

            disabled: set[str] = set()
            for page in range(_MAX_PAGES):
                rows = _json(http.get(
                    users_url,
                    params={"enabled": "false", "first": page * _PAGE, "max": _PAGE},
                    headers=auth,
                ), "the disabled users")
                if not isinstance(rows, list):
                    raise OffboardingError("Keycloak's user list is not a list")
                for row in rows:
                    if not isinstance(row, dict) or not isinstance(row.get("enabled"), bool):
                        # Without the field nobody could be told apart, and
                        # "nobody is disabled" would be read from it.
                        raise OffboardingError("a user in Keycloak's answer has no 'enabled' field")
                    name = row.get("username")
                    # A server that does not know the ``enabled`` filter
                    # answers with everyone: the field decides, not the query.
                    if row["enabled"] is False and isinstance(name, str) and name.strip():
                        disabled.add(name.strip().lower())
                if len(rows) < _PAGE:
                    return disabled
            raise OffboardingError(f"more than {_MAX_PAGES * _PAGE} users in the answer: not read to the end")
    except httpx.HTTPError as exc:
        # httpx timeouts stringify to nothing: the type is the message.
        raise OffboardingError(f"Keycloak not reached: {type(exc).__name__}") from exc


# ---------------------------------------------------------------------------
# Applying what was read
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _names(usernames: list[str], limit: int = 10) -> str:
    shown = ", ".join(usernames[:limit])
    return shown + (f" and {len(usernames) - limit} more" if len(usernames) > limit else "")


def reconcile(
    session: Session, disabled: set[str], *, mode: str, max_per_run: int = DEFAULT_MAX_PER_RUN,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Mark the people in ``disabled`` (lower-cased usernames), unmark the
    ones no longer in it and, in ``enforce``, disable the marked. Commits.

    Returns ``{"new", "back", "disabled", "held", "pending"}``: usernames
    marked in this run, unmarked in this run, disabled in this run, and
    (``held``) whether ``enforce`` disabled nobody because more than
    ``max_per_run`` were due; ``pending`` counts the marked accounts that
    are still active afterwards.
    """
    now = now or _utcnow()
    people = session.exec(select(User).where(~service_account_clause()).order_by(User.username)).all()
    new: list[str] = []
    back: list[str] = []
    for person in people:
        reported = person.username.strip().lower() in disabled
        if reported and person.sso_disabled_at is None:
            person.sso_disabled_at = now
            session.add(person)
            new.append(person.username)
        elif not reported and person.sso_disabled_at is not None:
            person.sso_disabled_at = None
            session.add(person)
            back.append(person.username)

    due = [p for p in people if p.sso_disabled_at is not None and not p.is_disabled]
    held = mode == "enforce" and len(due) > max_per_run
    to_disable = due if mode == "enforce" and not held else []
    for person in to_disable:
        person.is_disabled = True
        session.add(person)
    acted = [(int(p.id), p.username, p.sso_disabled_at) for p in to_disable]
    session.commit()

    # After the commit: the trail says what happened, never what was about to.
    for user_id, username, since in acted:
        audit.record_job(
            JOB_NAME, user_id=user_id, username=username,
            is_disabled=True, reason="sign-in disabled at Keycloak", reported_since=since.isoformat() + "Z",
        )
    return {
        "new": new, "back": back, "disabled": [name for _, name, _ in acted], "held": held,
        "pending": len(due) - len(acted),
    }


def summary(mode: str, result: dict[str, Any]) -> str:
    """One line for the log and ``scheduled_jobs.last_detail``."""
    parts = [mode]
    if result["new"]:
        parts.append(f"newly reported disabled: {_names(result['new'])}")
    if result["disabled"]:
        parts.append(f"disabled here: {_names(result['disabled'])}")
    if result["held"]:
        parts.append(f"{result['pending']} accounts are due, above OFFBOARDING_MAX_PER_RUN: none disabled, review them")
    elif result["pending"]:
        parts.append(f"{result['pending']} still active")
    if result["back"]:
        parts.append(f"enabled again at Keycloak: {_names(result['back'])}")
    if len(parts) == 1:
        parts.append("nothing new")
    return " | ".join(parts)


def run(session: Session) -> str:
    """One scheduled run. Raises when Keycloak cannot be read, which changes
    nothing: the marks stay as the last good read left them."""
    s = settings()
    if s["mode"] == "off":
        return "off"
    disabled = read_disabled(s)  # before the first query: no connection held meanwhile
    result = reconcile(session, disabled, mode=s["mode"], max_per_run=s["max_per_run"])
    line = summary(s["mode"], result)
    if result["new"] or result["disabled"] or result["held"]:
        logger.warning("Offboarding | {}", line)
    return line


def _interval() -> float:
    if settings()["mode"] == "off":
        return 0.0
    return env_seconds("OFFBOARDING_INTERVAL_S", DEFAULT_INTERVAL_S)


JOB = Job(name=JOB_NAME, interval=_interval, run=run)


# ---------------------------------------------------------------------------
# For the admin page
# ---------------------------------------------------------------------------

def pending_clause():
    """A person Keycloak reports as disabled whose account is still active:
    what the admin is asked to look at. The Users table's ``sso_disabled``
    filter and ``to_review`` are this one test."""
    return (
        User.sso_disabled_at.is_not(None)  # type: ignore[union-attr]
        & (User.is_disabled == False)  # noqa: E712
        & ~service_account_clause()
    )


def to_review(session: Session) -> list[dict]:
    """The accounts under ``pending_clause``, newest report first. Read from
    ``users`` as it is, so an entry goes once the account is disabled or
    Keycloak reports the person enabled again."""
    rows = session.exec(
        select(User).where(pending_clause()).order_by(User.sso_disabled_at.desc(), User.username)  # type: ignore[union-attr]
    ).all()
    return [
        {
            "id": u.id, "username": u.username, "display_name": u.display_name or "",
            "org_code": u.org_code or "", "since": u.sso_disabled_at,
            "key_last_used_at": u.api_key_last_used_at,
        }
        for u in rows
    ]


def status(session: Session) -> dict[str, Any]:
    """What the admin page shows: the mode, the accounts to review and, when
    the check itself is not working, why. A check that fails silently would
    look exactly like a day on which nobody left."""
    s = settings()
    out: dict[str, Any] = {"mode": s["mode"], "pending": to_review(session), "problem": "", "checked_at": None}
    if s["mode"] == "off":
        return out
    missing = missing_settings(s)
    job = session.get(ScheduledJob, JOB_NAME)
    if missing:
        out["problem"] = "not set: " + ", ".join(missing)
    elif job is not None and job.last_status == "error":
        out["problem"] = job.last_detail or "the last check failed"
    if job is not None:
        out["checked_at"] = job.last_finished_at
    return out
