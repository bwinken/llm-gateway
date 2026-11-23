"""Model changes: telling accounts that a model is being replaced.

Retiring a model means every Claude Code setup that names it has to name
another one. The gateway cannot edit a settings file; what it can do is say
so where people look, for as long as an admin decides, and hand them a
script that does the edit:

- **A model change** (``model_changes``, made by an admin on the *Clients*
  tab) is a notice (title, message), the renames (old model name, new
  model name) and a switch, ``shown``: an admin turns it on to show the
  change and off to take it away, as often as needed. There is no schedule.
- **Tiers.** Each rename is for a tier (``TIERS``): everyone, or what
  accounts with Azure / AWS access get on top (AWS wins where both name
  the same model). ``effective_renames`` is the one
  answer to "what does this account replace", and an account for which that
  is empty is not shown the change and not mailed: a change with only the
  Azure box filled is a change for Azure accounts.
- **On the dashboard** (``for_account``) the change stays for as long as
  it is switched on, whatever the account does: *I've switched* folds it away
  and stops mail, but the notice and its script stay available (for a
  second machine).
- **The script** (``/dashboard/model-changes/patch.{bat,sh}``) carries the
  renames of every change the account is shown (``renames_for``) and
  nothing else. What a rename does to settings.json is
  ``client_patch.apply_renames``.
- **Its own flow.** A change is not a violation: nothing was detected. It
  is not among the notices (``GET /v1/notices``), its script is not the
  notices' patch, and "I've applied it" there does not touch it. By API key
  it is the ``model_changes`` block of ``GET /v1/notices``.
- **Mail** goes out only when an admin asks (``request_mail``), to the
  accounts the change is for that have not said they switched, through the
  client-rule mail job and its pace (``client_mail.send_due``). This module
  keeps who is waiting (``due_mail``, ``mail_items``); sending, the mail's
  text and the mail log are ``client_mail``'s.

Nothing here runs on the request path, and nothing here detects anything:
who still asks for the old model is in the usage reports, and once the model
is removed from the config, check ``CC004`` tells whoever still names it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import false, or_, true
from sqlmodel import Session, func, select, update

from app.core import config
from app.models.schema import ModelChange, ModelChangeState, User, service_account_clause
from app.services.client_patch import dump_renames, load_renames, parse_renames
from app.services.client_rules import AUDIENCES, audience_flags, audience_of, stored_audience


# How a change is named in mail, the mail log and the API: ``MC`` + its id.
REF_PREFIX = "MC"


# The script an account downloads for its changes: this name + ``.bat`` / ``.sh``.
PATCH_FILE = "claude-code-model-change"


_BOXES = ("renames", "renames_azure", "renames_aws")


# Who a rename is for, as the admin picks it per model: the tier's name in
# the form, its label, and the column it is stored in.
TIERS = (
    ("everyone", "Everyone", "renames"),
    ("azure", "Azure accounts", "renames_azure"),
    ("aws", "AWS accounts", "renames_aws"),
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ref(change_id: int | None) -> str:
    """``MC3``: the id of a change as people see it."""
    return f"{REF_PREFIX}{change_id}"


def parse_ref(text: str) -> int | None:
    """``MC3`` (or ``3``) back to the id; None for anything else."""
    text = (text or "").strip().upper()
    if text.startswith(REF_PREFIX):
        text = text[len(REF_PREFIX):]
    return int(text) if text.isdigit() else None


# ── What an account replaces ──

def effective_renames(change: Any, azure: bool, aws: bool) -> dict[str, str]:
    """The renames one account gets from a change: the base, then the Azure
    box if it has Azure access, then the AWS box if it has AWS access.
    ``change`` is anything with ``renames`` / ``renames_azure`` /
    ``renames_aws`` in stored form. Empty: the change is not for this account."""
    renames = dict(load_renames(change.renames))
    if azure:
        renames.update(load_renames(change.renames_azure))
    if aws:
        renames.update(load_renames(change.renames_aws))
    return renames


def is_showing(change: ModelChange) -> bool:
    """Switched on: on dashboards, in the API, and mail can be sent."""
    return bool(change.shown)


def showing(session: Session) -> list[ModelChange]:
    """The changes being shown now (switched on), oldest first."""
    return list(session.exec(
        select(ModelChange).where(ModelChange.shown.is_(True)).order_by(ModelChange.id)  # type: ignore[attr-defined,arg-type]
    ).all())


def get(session: Session, change_id: int) -> ModelChange | None:
    return session.get(ModelChange, change_id)


def _states(session: Session, username: str, change_ids: list[int]) -> dict[int, ModelChangeState]:
    if not change_ids:
        return {}
    return {
        row.change_id: row for row in session.exec(
            select(ModelChangeState).where(
                ModelChangeState.username == username,
                ModelChangeState.change_id.in_(change_ids),  # type: ignore[attr-defined]
            )
        ).all()
    }


def for_account(session: Session, user: User) -> list[dict[str, Any]]:
    """What ``user``'s dashboard shows: every change being shown that has
    renames for this account, oldest first, with whether the account said it
    switched. A change stays listed after that, for as long as it is shown."""
    azure, aws = audience_of(user)
    found = [
        (change, renames) for change in showing(session)
        if (renames := effective_renames(change, azure, aws))
    ]
    states = _states(session, user.username, [change.id for change, _ in found])  # type: ignore[misc]
    return [
        {
            "id": change.id, "ref": ref(change.id), "title": change.title, "message": change.message,
            "renames": renames,
            "applied": bool(states.get(change.id) and states[change.id].applied_at),  # type: ignore[index,arg-type]
        }
        for change, renames in found
    ]


def renames_for(session: Session, user: User) -> dict[str, str]:
    """The renames in the account's model-change script: those of every
    change it is shown, merged (a later change wins where two replace the
    same model), whether or not it said it switched: a rename already
    applied changes nothing, and the script is also for the account's other
    machines."""
    renames: dict[str, str] = {}
    for item in for_account(session, user):
        renames.update(item["renames"])
    return renames


def set_applied(session: Session, change_id: int, user: User, now: datetime | None = None) -> bool:
    """*I've switched*: record that ``user`` says it applied a change it is
    shown. False when there is no such change for this account. The notice
    stays, folded; the account is no longer mailed about it."""
    now = now or utcnow()
    if not any(item["id"] == change_id for item in for_account(session, user)):
        return False
    row = _states(session, user.username, [change_id]).get(change_id)
    if row is None:
        row = ModelChangeState(change_id=change_id, username=user.username)
    row.applied_at = now
    session.add(row)
    session.commit()
    return True


# ── Admin: a change's form ──

def rows_to_boxes(rows: list[tuple[str, str, str]]) -> dict[str, str]:
    """The form's model rows, ``(old name, new name, tier)``, as the three
    rename texts ``clean_fields`` takes. An empty row is skipped; a row
    with one name missing or an unknown tier raises ValueError."""
    boxes: dict[str, list[str]] = {column: [] for _, _, column in TIERS}
    columns = {name: column for name, _, column in TIERS}
    for old, new, tier in rows:
        old, new, tier = (old or "").strip(), (new or "").strip(), (tier or "everyone").strip().lower()
        if not old and not new:
            continue
        if not old or not new:
            raise ValueError(f"Give both the old and the new model name ({old or new} has only one).")
        if tier not in columns:
            raise ValueError(f"Tier must be one of: {', '.join(columns)}.")
        boxes[columns[tier]].append(f"{old} -> {new}")
    return {column: "\n".join(lines) for column, lines in boxes.items()}


def rows(change: Any) -> list[dict[str, str]]:
    """A change's renames as the form's rows: old name, new name, tier."""
    return [
        {"old": old, "new": new, "tier": name}
        for name, _, column in TIERS for old, new in load_renames(getattr(change, column)).items()
    ]


def _check_models(boxes: dict[str, dict[str, str]]) -> None:
    """A replacement must be a model the accounts told about it can use: one
    the gateway has, and for the *Everyone* box not a cloud model."""
    local = set(config.get_model_routing_snapshot())
    azure = set(config.get_azure_models_snapshot())
    bedrock = set(config.get_bedrock_models_snapshot())
    for box, renames in boxes.items():
        for new in renames.values():
            if new not in local | azure | bedrock:
                raise ValueError(f"{new} is not a model this gateway has. Add it under Models first.")
            if box == "renames" and new not in local:
                where = "Azure" if new in azure else "AWS"
                raise ValueError(
                    f"{new} is an {where} model, which accounts without {where} access cannot use: "
                    f"put that line in the {where} accounts box."
                )
            if box == "renames_azure" and new in bedrock:
                raise ValueError(f"{new} is an AWS model: put that line in the AWS accounts box.")
            if box == "renames_aws" and new in azure:
                raise ValueError(f"{new} is an Azure model: put that line in the Azure accounts box.")


def clean_fields(form: dict[str, Any]) -> dict[str, Any]:
    """Validate an admin's form for a change. Returns column values (never
    ``shown``: that is the switch, ``set_shown``); raises ValueError."""
    title = str(form.get("title") or "").strip()
    if not title or len(title) > 200:
        raise ValueError("Give the notice a title (200 characters at most).")
    message = str(form.get("message") or "").strip()
    if len(message) > 4000:
        raise ValueError("The message is too long (4000 characters at most).")
    boxes = {name: parse_renames(str(form.get(name) or "")) for name in _BOXES}
    if not any(boxes.values()):
        raise ValueError("Say which model is replaced by which: an old and a new model name.")
    _check_models(boxes)
    return {
        "title": title, "message": message,
        **{name: dump_renames(renames) for name, renames in boxes.items()},
        "email_subject": str(form.get("email_subject") or "").strip()[:200],
    }


def save(
    session: Session, values: dict[str, Any], by: str = "", change: ModelChange | None = None,
    shown: bool | None = None, now: datetime | None = None,
) -> ModelChange:
    """Create a change (``shown``: switched on at once, off when not given),
    or update ``change`` (its switch stays as it is unless ``shown`` says)."""
    now = now or utcnow()
    if change is None:
        change = ModelChange(created_by=by, created_at=now, shown=bool(shown))
    elif shown is not None:
        change.shown = shown
    for key, value in values.items():
        setattr(change, key, value)
    change.updated_by = by
    change.updated_at = now
    session.add(change)
    session.commit()
    session.refresh(change)
    return change


def set_shown(session: Session, change: ModelChange, shown: bool, by: str = "", now: datetime | None = None) -> ModelChange:
    """The switch: show a change now, or take it away. Off, it leaves every
    dashboard, its script and the API at once and nobody is mailed about it;
    on again, it is back as it was (who said they switched, who was mailed)."""
    if change.shown != shown:
        change.shown = shown
        change.updated_by = by
        change.updated_at = now or utcnow()
        session.add(change)
        session.commit()
        session.refresh(change)
    return change


# ── Who a change is for ──

def _people():
    """Accounts that can be told anything: a person, not disabled."""
    return (~service_account_clause(), User.is_disabled.is_(False))  # type: ignore[attr-defined]


def _audience_clause(change: Any):
    """SQL for "``effective_renames`` is not empty for this account"."""
    if load_renames(change.renames):
        return true()
    parts = []
    if load_renames(change.renames_azure):
        parts.append(User.can_use_azure.is_(True))  # type: ignore[attr-defined]
    if load_renames(change.renames_aws):
        parts.append(User.can_use_bedrock.is_(True))  # type: ignore[attr-defined]
    return or_(*parts) if parts else false()


def audience(session: Session, change: ModelChange) -> list[str]:
    """The accounts a change is for: people (not service accounts, not
    disabled) who get renames from it, by their stored access."""
    return list(session.exec(
        select(User.username).where(*_people(), _audience_clause(change)).order_by(User.username)
    ).all())


def audience_label(change: Any) -> str:
    """Who a change is for, in words, for the admin."""
    if load_renames(change.renames):
        return "everyone"
    tiers = [
        label for box, label in (("renames_azure", "Azure accounts"), ("renames_aws", "AWS accounts"))
        if load_renames(getattr(change, box))
    ]
    return " and ".join(tiers) or "nobody"


def is_queued(state: ModelChangeState) -> bool:
    """An admin asked for this account to be mailed and it has not happened."""
    requested = state.mail_requested_at
    if requested is None or state.applied_at is not None:
        return False
    return state.mailed_at is None or state.mailed_at < requested


def counts(session: Session, change: ModelChange) -> dict[str, int]:
    """For the admin's list: how many accounts a change is for, and how many
    of them said they switched, were mailed, are waiting for a mail or
    could not be mailed."""
    people = session.exec(
        select(func.count()).select_from(User).where(*_people(), _audience_clause(change))
    ).one()
    rows = session.exec(select(ModelChangeState).where(ModelChangeState.change_id == change.id)).all()
    return {
        "accounts": int(people),
        "applied": sum(1 for row in rows if row.applied_at is not None),
        "mailed": sum(1 for row in rows if row.mailed_at is not None),
        "queued": sum(1 for row in rows if is_queued(row)),
        "failed": sum(1 for row in rows if row.mail_error and is_queued(row)),
    }


def overview(session: Session, off_limit: int = 30) -> dict[str, list[ModelChange]]:
    """Every change, for the admin: those being shown (oldest first) and
    the ``off_limit`` most recently saved that are switched off."""
    off = session.exec(
        select(ModelChange).where(ModelChange.shown.is_(False))  # type: ignore[attr-defined]
        .order_by(ModelChange.updated_at.desc(), ModelChange.id.desc())  # type: ignore[attr-defined,union-attr]
        .limit(off_limit)
    ).all()
    return {"showing": showing(session), "off": list(off)}


def preview_renames(change: Any, audience_name: str) -> dict[str, str]:
    """The renames one kind of account gets (``local``, ``azure`` or
    ``aws``), for the admin's preview and test mail. Raises ValueError."""
    if audience_name not in AUDIENCES:
        raise ValueError(f"Audience must be one of: {', '.join(AUDIENCES)}.")
    return effective_renames(change, *audience_flags(audience_name))


# ── Mail: who is waiting ──

def request_mail(
    session: Session, change: ModelChange, only_unmailed: bool = True, now: datetime | None = None,
) -> int:
    """An admin's click on a change's mail button: queue the accounts it is
    for that have not said they switched (those never mailed about it, or
    all of them). Nothing is sent here: the mail job sends the queue in its
    batches. Returns how many were queued."""
    now = now or utcnow()
    if not is_showing(change):
        return 0
    states = {
        row.username: row
        for row in session.exec(select(ModelChangeState).where(ModelChangeState.change_id == change.id)).all()
    }
    queued = 0
    for username in audience(session, change):
        row = states.get(username)
        if row is not None and (row.applied_at is not None or (only_unmailed and row.mailed_at is not None)):
            continue
        if row is None:
            row = ModelChangeState(change_id=change.id, username=username)  # type: ignore[arg-type]
        row.mail_requested_at = now
        session.add(row)
        queued += 1
    session.commit()
    return queued


def split_names(text: str) -> list[str]:
    """Account names as an admin types them into *Send to*: separated by
    commas, spaces or new lines; a mail address is read as its account."""
    names = [part.partition("@")[0].strip() for part in (text or "").replace(",", " ").split()]
    return list(dict.fromkeys(name for name in names if name))


def request_mail_to(
    session: Session, change: ModelChange, usernames: list[str], now: datetime | None = None,
) -> dict[str, Any]:
    """An admin's *Send to* these people: queue the named accounts, whether
    or not they were mailed before. Every name must be an account the
    change is for (ValueError names the ones that are not, and nobody is
    queued); one that said it switched is skipped and reported. Returns
    ``{"queued": n, "skipped": [names]}``. Nothing is sent here."""
    now = now or utcnow()
    if not is_showing(change) or not usernames:
        return {"queued": 0, "skipped": []}
    allowed = set(audience(session, change))
    strangers = [name for name in usernames if name not in allowed]
    if strangers:
        raise ValueError(
            f"Not accounts this announcement is for: {', '.join(strangers)}. "
            "Check the spelling, and that each has the access its tier needs."
        )
    states = {
        row.username: row
        for row in session.exec(select(ModelChangeState).where(
            ModelChangeState.change_id == change.id, ModelChangeState.username.in_(usernames),  # type: ignore[attr-defined]
        )).all()
    }
    skipped = []
    queued = 0
    for username in usernames:
        row = states.get(username)
        if row is not None and row.applied_at is not None:
            skipped.append(username)
            continue
        if row is None:
            row = ModelChangeState(change_id=change.id, username=username)  # type: ignore[arg-type]
        row.mail_requested_at = now
        session.add(row)
        queued += 1
    session.commit()
    return {"queued": queued, "skipped": skipped}


def _request_due(state: ModelChangeState, now: datetime, retry_s: float) -> bool:
    if not is_queued(state):
        return False
    attempted = state.mail_attempted_at
    if attempted is not None and attempted >= state.mail_requested_at:  # type: ignore[operator]
        return attempted <= now - timedelta(seconds=retry_s)     # tried since the request, and failed
    return True


def mail_items(
    session: Session, now: datetime | None = None, retry_s: float = 0.0, usernames: list[str] | None = None,
) -> dict[str, list[tuple[ModelChange, ModelChangeState]]]:
    """Per account, the changes it is waiting for a mail about (asked for by
    an admin, still shown, not marked as switched, not tried and failed in
    the last ``retry_s``), oldest change first. Accounts that no longer
    exist, were disabled or are service accounts are left out: one account
    gets one mail about all of it."""
    now = now or utcnow()
    live = {change.id: change for change in showing(session)}
    if not live:
        return {}
    query = select(ModelChangeState).where(
        ModelChangeState.change_id.in_(list(live)),  # type: ignore[attr-defined]
        ModelChangeState.mail_requested_at.is_not(None),  # type: ignore[union-attr]
    )
    if usernames is not None:
        if not usernames:
            return {}
        query = query.where(ModelChangeState.username.in_(usernames))  # type: ignore[attr-defined]
    grouped: dict[str, list[tuple[ModelChange, ModelChangeState]]] = {}
    for row in session.exec(query.order_by(ModelChangeState.change_id)).all():  # type: ignore[arg-type]
        if _request_due(row, now, retry_s):
            grouped.setdefault(row.username, []).append((live[row.change_id], row))
    if not grouped:
        return {}
    known = set(session.exec(
        select(User.username).where(User.username.in_(list(grouped)), *_people())  # type: ignore[attr-defined]
    ).all())
    return {username: items for username, items in grouped.items() if username in known}


def due_mail(session: Session, now: datetime | None = None, retry_s: float = 0.0) -> list[str]:
    """The accounts waiting for a model-change mail, oldest request first."""
    items = mail_items(session, now, retry_s)
    return sorted(items, key=lambda name: (min(row.mail_requested_at for _, row in items[name]), name))  # type: ignore[type-var]


def account_renames(user: User, changes: list[ModelChange]) -> dict[str, str]:
    """The renames of ``changes`` for an account read from the database with
    nobody signed in (mail): by its stored access."""
    azure, aws = stored_audience(user)
    renames: dict[str, str] = {}
    for change in changes:
        renames.update(effective_renames(change, azure, aws))
    return renames


def claim_mail(session: Session, state_id: int, attempted_before: datetime | None, stamp: datetime) -> bool:
    """Claim an account's mail before sending it: a compare-and-set on the
    state's last attempt as it was when the mail was prepared
    (``attempted_before``), so two senders never mail the same account."""
    unchanged = (
        ModelChangeState.mail_attempted_at.is_(None) if attempted_before is None  # type: ignore[union-attr]
        else ModelChangeState.mail_attempted_at == attempted_before
    )
    claimed = session.execute(
        update(ModelChangeState).where(ModelChangeState.id == state_id, unchanged).values(mail_attempted_at=stamp)
    ).rowcount == 1
    session.commit()
    return claimed


def stamp_mail(session: Session, state_ids: list[int], stamp: datetime, ok: bool, error: str = "") -> None:
    """Write how a mail ended on every state it covered."""
    values: dict[str, Any] = {"mail_attempted_at": stamp, "mail_error": "" if ok else (error or "")[:300]}
    if ok:
        values["mailed_at"] = stamp
    session.execute(update(ModelChangeState).where(ModelChangeState.id.in_(state_ids)).values(**values))  # type: ignore[union-attr]
    session.commit()
