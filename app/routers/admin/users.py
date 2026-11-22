"""
Admin *Users* tab: the Users / Service accounts tables (``_user_table``, also
served alone by ``GET /admin/user-table``), the account drawer and its
actions, the change to several accounts at once (``POST
/admin/users/bulk-update``), service accounts and their owners, the account
search of the quick search, and the JSON user API (``/admin/users``).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Query, Request, Security
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import case, delete, exists, false as sa_false, update
from sqlalchemy.orm import aliased
from sqlmodel import Session, func, select

from app.core.auth import get_web_user
from app.core.config import get_cloud_daily_limit
from app.core.database import get_session
from app.core.logger import logger
from app.models.schema import (
    AnomalyEvent,
    AppOwner,
    User,
    UsageLog,
    department_of,
    in_department,
    same_department,
    service_account_clause,
    is_legacy_api_key,
    legacy_api_key_clause,
    mask_api_key,
)
from app.routers.admin.common import (
    _limit_usd,
    _local_label,
    _reject_retired_admin_flag,
    _reject_retired_limits,
    _strict_bool,
    _wants_json,
    router,
    templates,
)
from app.services import audit, concurrency, overflow
from app.services.stats import get_monthly_all_users_usage, get_on_behalf_month, owners_to_review


_PAGE_SIZE = 15
# What the table's "per page" select offers. Any size up to _MAX_PAGE_SIZE
# still works in the URL.
USER_TABLE_PAGE_SIZES: tuple[int, ...] = (15, 30, 50, 100)


# ── Users / Apps tables ──
#
# The two tables in the Users tab render from one partial
# (_admin_user_table.html). /admin renders both inside the full page;
# /admin/user-table renders one of them alone, so searching, sorting,
# filtering, paging and refreshing after a delete swap just that panel
# instead of re-running every report on the page. Both read the same query
# parameters, so a panel link is also a working /admin URL without JS.

_MAX_PAGE_SIZE = 200


USER_TABLE_SORTS: dict[str, str] = {
    "id": "Oldest first",
    "name": "Username",
    "spend": "Spend this month",
}


USER_TABLE_STATUSES: dict[str, str] = {
    "all": "All accounts",
    "special": "Any special access",
    "azure": "Azure access",
    "bedrock": "AWS access",
    "cloud": "Cloud access",
    "cloud_cap": "Own cloud cap",
    "waived": "Concurrency waived",
    "waiver_changed": "Waiver not the default",
    "no_overflow": "No cloud overflow",
    "on_behalf": "On behalf of users",
    "disabled": "Disabled",
    # Not a permission: service accounts with an owner an admin should look
    # at (``stats.owners_to_review``). Deliberately not in SPECIAL_ACCESS_KEYS.
    "owner_review": "Owner to review",
    # Not a permission: tracks how many keys still predate the current format
    # (schema.is_legacy_api_key). Deliberately not in SPECIAL_ACCESS_KEYS.
    "legacy_key": "Older-format API key",
    # Access review: keys that have not authenticated in STALE_KEY_DAYS.
    "stale_key": "Key unused 90+ days",
}


STALE_KEY_DAYS = 90


# The "Special access" overview above the tables: one count per permission,
# each linking to its filter. The waiver counts only what differs from an
# account kind's default (users are created limited, service accounts waived).
SPECIAL_ACCESS_KEYS: tuple[str, ...] = (
    "azure", "bedrock", "cloud_cap", "waiver_changed", "no_overflow", "on_behalf", "disabled",
)


def _stale_key_clause():
    """Keys unused for STALE_KEY_DAYS: last use before the cutoff, or never
    used and issued before it (a key issued yesterday is not stale)."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=STALE_KEY_DAYS)
    used = User.api_key_last_used_at
    issued = User.api_key_created_at
    return (used < cutoff) | (  # type: ignore[operator]
        used.is_(None) & (issued.is_(None) | (issued < cutoff))  # type: ignore[union-attr,operator]
    )


def _is_stale(u: User, now: datetime) -> bool:
    cutoff = now - timedelta(days=STALE_KEY_DAYS)
    if u.api_key_last_used_at is not None:
        return u.api_key_last_used_at < cutoff
    return u.api_key_created_at is None or u.api_key_created_at < cutoff


def _key_use_label(last_used: datetime | None, now: datetime) -> str:
    if last_used is None:
        return "never used"
    days = (now - last_used).days
    if days <= 0:
        return "used today"
    return "used yesterday" if days == 1 else f"used {days} d ago"


def _owner_review_clause():
    """SQL twin of ``stats.owners_to_review``: a service account with an
    owner who is disabled, or (the account has a department) not in it."""
    owner = aliased(User)
    return exists().where(
        AppOwner.app_id == User.id,
        owner.id == AppOwner.owner_id,
        (owner.is_disabled == True)  # noqa: E712
        | (
            (func.trim(User.org_code) != "")
            & (func.lower(func.trim(owner.org_code)) != func.lower(func.trim(User.org_code)))
        ),
    )


def _status_clause(status: str, is_apps: bool):
    """The WHERE clause of a Users / Apps table filter (None for "all").
    ``special`` is every account with something other than the defaults of
    its kind: a flag granted, its own cloud cap, the concurrency waiver
    flipped from what new accounts of that kind get, (a person) peak
    overflow turned off, or (a service account) acting for its users."""
    waived_differs = (User.concurrency_waived == False) if is_apps else (User.concurrency_waived == True)  # noqa: E712
    # Peak overflow is allowed unless an admin turned it off, and a service
    # account is never moved whatever its flag says: only a person with it
    # off differs from the default.
    no_overflow = sa_false() if is_apps else (User.can_overflow_cloud == False)  # noqa: E712
    # Only a service account acts for users (X-On-Behalf-Of): the switch
    # means nothing on a person's account.
    on_behalf = (User.acts_for_users == True) if is_apps else sa_false()  # noqa: E712
    clauses = {
        "azure": User.can_use_azure == True,  # noqa: E712
        "bedrock": User.can_use_bedrock == True,  # noqa: E712
        "cloud": (User.can_use_azure == True) | (User.can_use_bedrock == True),  # noqa: E712
        "cloud_cap": User.cloud_daily_limit_usd.is_not(None),  # type: ignore[union-attr]
        "waived": User.concurrency_waived == True,  # noqa: E712
        "waiver_changed": waived_differs,
        "no_overflow": no_overflow,
        "on_behalf": on_behalf,
        "disabled": User.is_disabled == True,  # noqa: E712
        "owner_review": _owner_review_clause() if is_apps else sa_false(),
        "legacy_key": legacy_api_key_clause(),
        "stale_key": _stale_key_clause(),
    }
    if status == "special":
        return clauses["cloud"] | clauses["cloud_cap"] | waived_differs | no_overflow | on_behalf
    return clauses.get(status)


def _special_access_summary(session: Session) -> list[dict]:
    """Counts for the overview: ``[{key, label, users, apps, url}]``, from one
    aggregate per account kind."""
    keys = ("special", *SPECIAL_ACCESS_KEYS)
    counts: dict[str, dict[str, int]] = {key: {} for key in keys}
    for kind, is_apps in (("users", False), ("apps", True)):
        kind_clause = service_account_clause() if is_apps else ~service_account_clause()
        row = session.exec(
            select(*[
                func.coalesce(func.sum(case((_status_clause(key, is_apps), 1), else_=0)), 0)
                for key in keys
            ]).select_from(User).where(kind_clause)
        ).one()
        for key, n in zip(keys, row):
            counts[key][kind] = int(n)
    return [
        {"key": key, "label": USER_TABLE_STATUSES[key], **counts[key],
         "url": _admin_url(_STATE_DEFAULTS, status=key)}
        for key in keys
    ]


def _table_state(
    q: str = "",
    sort: str = "id",
    status: str = "all",
    offset: int = 0,
    limit: int = _PAGE_SIZE,
    app_offset: int = 0,
    app_limit: int = _PAGE_SIZE,
) -> dict:
    """The Users-tab query parameters, normalized (unknown values → defaults)."""
    return {
        "q": q.strip(),
        "sort": sort if sort in USER_TABLE_SORTS else "id",
        "status": status if status in USER_TABLE_STATUSES else "all",
        "offset": max(0, offset),
        "limit": min(max(1, limit), _MAX_PAGE_SIZE),
        "app_offset": max(0, app_offset),
        "app_limit": min(max(1, app_limit), _MAX_PAGE_SIZE),
    }


_STATE_DEFAULTS = _table_state()


def _admin_url(state: dict, **overrides) -> str:
    """``/admin?…#users`` for ``state`` with ``overrides``, URL-encoded and
    without parameters left at their defaults."""
    merged = {**state, **overrides}
    params = {k: v for k, v in merged.items() if v != _STATE_DEFAULTS[k]}
    return "/admin" + (f"?{urlencode(params)}" if params else "") + "#users"


def _user_table(
    session: Session,
    kind: str,
    state: dict,
    admin_user: User,
    monthly_map: dict[int, dict] | None = None,
) -> dict:
    """One page of the Users or Apps table, ready for the partial.

    ``monthly_map`` is the month's per-user usage when the caller already has
    it (the full admin page does); otherwise only this page's accounts are
    aggregated, or every account when sorting by spend."""
    is_apps = kind == "apps"
    offset_key, limit_key = ("app_offset", "app_limit") if is_apps else ("offset", "limit")
    limit = state[limit_key]

    clauses = [service_account_clause() if is_apps else ~service_account_clause()]
    if state["q"]:
        like = f"%{state['q']}%"
        clauses.append(
            User.username.ilike(like)  # type: ignore[attr-defined]
            | User.display_name.ilike(like)  # type: ignore[union-attr]
            | User.org_code.ilike(like)  # type: ignore[union-attr]
            | User.description.ilike(like)  # type: ignore[attr-defined]
        )
    status = state["status"]
    status_clause = _status_clause(status, is_apps)
    if status_clause is not None:
        clauses.append(status_clause)

    total = session.exec(select(func.count()).select_from(User).where(*clauses)).one()
    offset = max(0, min(state[offset_key], max(total - 1, 0)))

    # What each service account sent for its users this month (billed to
    # them, so not in ``monthly_map``): shown beside its own, and counted
    # when sorting by spend.
    sent_for_users: dict[int, dict] = {}
    if state["sort"] == "spend":
        if monthly_map is None:
            monthly_map = get_monthly_all_users_usage(session)
        if is_apps:
            sent_for_users = get_on_behalf_month(session)
        ids = list(session.exec(select(User.id).where(*clauses)).all())
        ids.sort(key=lambda i: (
            -(monthly_map.get(i, {}).get("cost", 0.0) + sent_for_users.get(i, {}).get("cost", 0.0)), i,
        ))
        page_ids = ids[offset:offset + limit]
        by_id = {
            u.id: u for u in session.exec(select(User).where(User.id.in_(page_ids))).all()  # type: ignore[union-attr]
        } if page_ids else {}
        page = [by_id[i] for i in page_ids if i in by_id]
    else:
        order = (User.username, User.id) if state["sort"] == "name" else (User.id,)
        page = list(session.exec(
            select(User).where(*clauses).order_by(*order).offset(offset).limit(limit)
        ).all())
        if monthly_map is None:
            monthly_map = get_monthly_all_users_usage(session, user_ids=[u.id for u in page])
        if is_apps and page:
            sent_for_users = get_on_behalf_month(session, [u.id for u in page])

    # Owners of the apps on this page only.
    owners: dict[int, list[str]] = {}
    if is_apps and page:
        links = session.exec(
            select(AppOwner.app_id, User.username)
            .join(User, User.id == AppOwner.owner_id)  # type: ignore[arg-type]
            .where(AppOwner.app_id.in_([u.id for u in page]))  # type: ignore[attr-defined]
            .order_by(User.username)
        ).all()
        for app_id, owner_name in links:
            owners.setdefault(int(app_id), []).append(str(owner_name))

    # Owners an admin should look at (outside the account's department, or disabled).
    review = {entry["id"]: entry["owners"] for entry in owners_to_review(session)} if is_apps else {}
    empty = {"cost": 0.0, "input_tokens": 0, "output_tokens": 0, "requests": 0}
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = []
    for u in page:
        monthly = monthly_map.get(u.id, empty)
        for_users = sent_for_users.get(u.id, {})
        rows.append({
            "id": u.id,
            "username": u.username,
            "display_name": u.display_name,
            "description": u.description,
            "org_code": u.org_code,
            "daily_limit_usd": u.daily_limit_usd,
            "cloud_daily_limit_usd": u.cloud_daily_limit_usd,
            "is_self": u.id == admin_user.id,
            "is_disabled": u.is_disabled,
            "can_use_azure": u.can_use_azure,
            "can_use_bedrock": u.can_use_bedrock,
            "concurrency_waived": u.concurrency_waived,
            "can_overflow_cloud": u.can_overflow_cloud,
            "acts_for_users": u.acts_for_users,
            "for_users_cost": for_users.get("cost", 0.0),
            "for_users_reqs": for_users.get("requests", 0),
            "for_users_count": for_users.get("users", 0),
            "key_legacy": is_legacy_api_key(u.api_key),
            "key_use": _key_use_label(u.api_key_last_used_at, now_naive),
            "key_stale": _is_stale(u, now_naive),
            "key_detail": (
                f"Key issued {_local_label(u.api_key_created_at) or 'before tracking began'}; "
                f"last used {_local_label(u.api_key_last_used_at) or 'never'} (Asia/Taipei)."
            ),
            "owners": owners.get(u.id, []),
            "owners_to_review": review.get(u.id, []),
            "monthly_cost": monthly["cost"],
            "monthly_tokens": monthly["input_tokens"] + monthly["output_tokens"],
            "monthly_reqs": monthly["requests"],
        })

    return {
        "kind": kind,
        "rows": rows,
        "total": total,
        "offset": offset,
        "limit": limit,
        "first": offset + 1 if total else 0,
        "last": min(offset + limit, total),
        "prev_url": _admin_url(state, **{offset_key: max(offset - limit, 0)}) if offset > 0 else None,
        "next_url": _admin_url(state, **{offset_key: offset + limit}) if offset + limit < total else None,
        "filtered": bool(state["q"]) or status != "all",
    }


@router.get("/user-table", response_class=HTMLResponse)
def user_table_panel(
    request: Request,
    kind: str = Query(..., pattern="^(users|apps)$"),
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
    limit: int = _PAGE_SIZE,
    offset: int = 0,
    app_limit: int = _PAGE_SIZE,
    app_offset: int = 0,
    q: str = "",
    sort: str = "id",
    status: str = "all",
):
    """One Users-tab panel (``kind`` = users | apps) as an HTML fragment.

    Takes the same query parameters as ``/admin``, whose page swaps the
    fragment in. Plain ``def`` for the same reason as ``admin_page``."""
    state = _table_state(q, sort, status, offset, limit, app_offset, app_limit)
    return templates.TemplateResponse(
        request,
        "_admin_user_table.html",
        {
            "request": request,
            "table": _user_table(session, kind, state, admin_user),
            "cloud_daily_limit": get_cloud_daily_limit(),
        },
    )


# ── Account drawer ──
#
# Clicking an account's name opens a side panel with what an admin asks
# about one account: today against its limits, 30 days of spend, what it
# uses, its client notices and anomalies, its key, and what was changed on it.
# Read-only and revealing no key, so it is not an audited read.

_DETAIL_DAYS = 30
_DETAIL_AUDIT_ROWS = 12


def _account_detail(session: Session, target: User) -> dict:
    from app.core.deps import effective_cloud_limit
    from app.core.timeutil import local_today
    from app.routers.admin.security import _audit_rows
    from app.services import client_rules
    from app.services.stats import get_daily_trends, get_user_daily_summary
    from app.services.usage_rollup import aggregate

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    today = get_user_daily_summary(session, target.id)
    trend = get_daily_trends(session, target.id, days=_DETAIL_DAYS)
    first = local_today() - timedelta(days=_DETAIL_DAYS - 1)
    by_model = aggregate(
        session, first, local_today() + timedelta(days=1), ("backend", "model"), user_ids=[target.id],
    )
    models = sorted(
        (
            {"backend": backend, "model": model, "requests": e["requests"],
             "tokens": e["input_tokens"] + e["output_tokens"], "cost": float(e["cost_usd"])}
            for (backend, model), e in by_model.items()
        ),
        key=lambda m: -m["cost"],
    )
    total_30d = sum(m["cost"] for m in models)
    for m in models:
        m["share"] = round(m["cost"] / total_30d * 100, 1) if total_30d else 0.0

    cloud = target.can_use_azure or target.can_use_bedrock
    cloud_limit = effective_cloud_limit(target) if cloud else None
    cloud_spent = today["azure_cost_usd"] + today["bedrock_cost_usd"]

    owner_issues: list[dict] = []
    if target.is_service_account:
        owners = session.exec(
            select(User).join(AppOwner, AppOwner.owner_id == User.id).where(AppOwner.app_id == target.id)
        ).all()
        related = [o.username for o in owners]
        # Owners an admin should look at: the same two tests as ``stats.owners_to_review``.
        for o in sorted(owners, key=lambda o: o.username):
            if not in_department(o, target):
                owner_issues.append({"username": o.username, "org_code": o.org_code or "", "reason": "department"})
            elif o.is_disabled:
                owner_issues.append({"username": o.username, "org_code": o.org_code or "", "reason": "disabled"})
    else:
        related = session.exec(
            select(User.username).join(AppOwner, AppOwner.app_id == User.id).where(AppOwner.owner_id == target.id)
        ).all()

    anomalies = session.exec(
        select(AnomalyEvent)
        .where(AnomalyEvent.user_id == target.id, AnomalyEvent.status != "resolved")
        .order_by(AnomalyEvent.created_at.desc())  # type: ignore[attr-defined]
        .limit(5)
    ).all()

    return {
        "user": target,
        "today": today,
        "limit_share": (today["total_cost_usd"] / target.daily_limit_usd * 100) if target.daily_limit_usd else None,
        "cloud": cloud,
        "cloud_limit": cloud_limit,
        "cloud_spent": cloud_spent,
        "trend": [{"date": d["date"], "cost": d["cost"], "reqs": d["reqs"]} for d in trend],
        "total_30d": total_30d,
        "requests_30d": sum(d["reqs"] for d in trend),
        "active_days": sum(1 for d in trend if d["reqs"]),
        "models": models[:8],
        "more_models": max(len(models) - 8, 0),
        "related": sorted(related),
        "owner_issues": owner_issues,
        "department": department_of(target) if target.is_service_account else "",
        "violations": client_rules.account_violations(session, target.username),
        "anomalies": [
            {"rule": a.rule, "severity": a.severity, "status": a.status, "at": _local_label(a.created_at)}
            for a in anomalies
        ],
        "key_use": _key_use_label(target.api_key_last_used_at, now_naive),
        "key_stale": _is_stale(target, now_naive),
        "key_legacy": is_legacy_api_key(target.api_key),
        "key_issued": _local_label(target.api_key_created_at),
        "key_last_used": _local_label(target.api_key_last_used_at),
        "created": _local_label(target.created_at),
        "audit": _audit_rows(session, limit=_DETAIL_AUDIT_ROWS, about=target),
        "local_label": _local_label,
    }


@router.get("/users/{user_id}/detail", response_class=HTMLResponse)
def user_detail_panel(
    request: Request,
    user_id: int,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """The account drawer as an HTML fragment: what the account used, and
    the forms that change it (the same routes a table row used to post to).
    The GET itself only reads and never contains a key. Plain ``def``: it
    reads usage (``aggregate``) and must not block the event loop."""
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="No such account.")
    return templates.TemplateResponse(
        request, "_admin_user_detail.html",
        {
            "request": request,
            "d": _account_detail(session, target),
            "is_self": target.id == admin_user.id,
            "cloud_daily_limit": get_cloud_daily_limit(),
            "description_max": DESCRIPTION_MAX,
            "departments": known_departments(session) if target.is_service_account else [],
        },
    )


_SERVICE_ACCOUNT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _service_account_name(raw: object) -> str:
    """A service account's name as typed, checked: letters, digits, ``.``
    ``_`` ``-``, starting with a letter or digit (it appears in URLs, logs
    and CSV exports). An SSO sign-in under the same name is refused from
    then on, so it must not be a person's AuthCenter username."""
    name = str(raw or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Username is required.")
    if not _SERVICE_ACCOUNT_NAME.match(name):
        raise HTTPException(
            status_code=400,
            detail="A service account name is 1 to 64 letters, digits, dots, dashes or underscores, starting with a letter or digit.",
        )
    return name


OWNER_SEARCH_LIMIT = 8


def _owner_names(raw: object) -> list[str]:
    """Comma-separated usernames as typed, in order, without repeats."""
    return list(dict.fromkeys(name.strip() for name in str(raw or "").split(",") if name.strip()))


def _owners(
    session: Session, names: list[str], already: frozenset[int] = frozenset(), department: str = "",
) -> list[User]:
    """The accounts behind ``names``, checked as owners of a service
    account: each must exist and be a person, and in the account's
    ``department`` when it has one (an owner outside it would see nothing
    of the account). A service account cannot own one (it has no dashboard
    to see it on). 400 naming what is wrong.

    ``already`` holds the ids of the account's current owners: an
    assignment made before a rule existed, or before the owner moved to
    another department, is never refused, so editing the list cannot take
    an existing owner away by accident."""
    if not names:
        return []
    found = {u.username: u for u in session.exec(select(User).where(User.username.in_(names))).all()}  # type: ignore[attr-defined]
    unknown = [name for name in names if name not in found]
    if unknown:
        raise HTTPException(status_code=400, detail=f"User(s) not found: {', '.join(unknown)}")
    service = [name for name in names if found[name].is_service_account and found[name].id not in already]
    if service:
        raise HTTPException(
            status_code=400,
            detail=f"Only a person can own a service account; {', '.join(service)} "
                   f"{'is a service account' if len(service) == 1 else 'are service accounts'}.",
        )
    outside = [
        name for name in names
        if department and found[name].id not in already and not same_department(found[name].org_code, department)
    ]
    if outside:
        raise HTTPException(
            status_code=400,
            detail=f"This account is for department {department}; not in it: "
                   + ", ".join(f"{name} ({found[name].org_code or 'no department'})" for name in outside) + ".",
        )
    return [found[name] for name in names]


def _current_owner_ids(session: Session, account_id: int) -> frozenset[int]:
    return frozenset(session.exec(select(AppOwner.owner_id).where(AppOwner.app_id == account_id)).all())


def _owners_by_id(
    session: Session, raw: object, already: frozenset[int] = frozenset(), department: str = "",
) -> list[User]:
    """``owner_ids`` of the JSON API, checked like ``_owners``."""
    if raw in (None, []):
        return []
    if not isinstance(raw, list) or not all(isinstance(oid, int) and not isinstance(oid, bool) for oid in raw):
        raise HTTPException(status_code=400, detail="owner_ids must be a list of user ids.")
    ids = list(dict.fromkeys(raw))
    found = {u.id: u for u in session.exec(select(User).where(User.id.in_(ids))).all()}  # type: ignore[attr-defined]
    unknown = [str(oid) for oid in ids if oid not in found]
    if unknown:
        raise HTTPException(status_code=400, detail=f"User id(s) not found: {', '.join(unknown)}")
    return _owners(session, [found[oid].username for oid in ids], already, department)


def _service_account_or_400(target: User) -> None:
    """Owners see the account's API key on their dashboard, so only an
    account made for a program may be given any: never a person's."""
    if not target.is_service_account:
        raise HTTPException(status_code=400, detail="Only a service account has owners.")


@router.get("/api/owner-search")
def search_owners(
    q: str = Query("", max_length=100),
    session: Session = Depends(get_session),
):
    """Who can be made an owner, as an admin types: the people (never a
    service account) whose username, display name or org code contains
    ``q``, names starting with it first, ``OWNER_SEARCH_LIMIT`` at most.
    Nothing for an empty ``q``. Plain ``def`` (database read)."""
    q = q.strip()
    if not q:
        return {"users": []}
    like = f"%{q}%"
    rows = session.exec(
        select(User.username, User.display_name, User.org_code, User.is_disabled)
        .where(
            ~service_account_clause(),
            User.username.ilike(like)  # type: ignore[attr-defined]
            | User.display_name.ilike(like)  # type: ignore[union-attr]
            | User.org_code.ilike(like),  # type: ignore[union-attr]
        )
        .order_by(case((User.username.ilike(f"{q}%"), 0), else_=1), User.username)  # type: ignore[attr-defined]
        .limit(OWNER_SEARCH_LIMIT)
    ).all()
    return {"users": [
        {"username": username, "display_name": display_name or "", "org_code": org_code or "", "disabled": bool(disabled)}
        for username, display_name, org_code, disabled in rows
    ]}


ACCOUNT_SEARCH_LIMIT = 8


@router.get("/api/account-search")
def search_accounts(
    q: str = Query("", max_length=100),
    session: Session = Depends(get_session),
):
    """The accounts the quick search (Ctrl+K) offers as an admin types:
    people and service accounts whose username, display name or org code
    contains ``q``, names starting with it first, ``ACCOUNT_SEARCH_LIMIT``
    at most. Nothing for fewer than two characters. Never a key. Plain
    ``def`` (database read)."""
    q = q.strip()
    if len(q) < 2:
        return {"accounts": []}
    like = f"%{q}%"
    rows = session.exec(
        select(User.id, User.username, User.display_name, User.org_code, User.is_disabled, User.is_service_account)
        .where(
            User.username.ilike(like)  # type: ignore[attr-defined]
            | User.display_name.ilike(like)  # type: ignore[union-attr]
            | User.org_code.ilike(like)  # type: ignore[union-attr]
        )
        .order_by(case((User.username.ilike(f"{q}%"), 0), else_=1), User.username)  # type: ignore[attr-defined]
        .limit(ACCOUNT_SEARCH_LIMIT)
    ).all()
    return {"accounts": [
        {
            "id": account_id, "username": username, "display_name": display_name or "", "org_code": org_code or "",
            "disabled": bool(disabled), "service_account": bool(service),
        }
        for account_id, username, display_name, org_code, disabled, service in rows
    ]}


# ── Several accounts at once ──
#
# The table's check boxes and the bar under it: one change for every account
# ticked. Each action takes an explicit value (never a flip: the accounts
# ticked may differ), everything is validated before anything is written,
# and an account the change cannot apply to is skipped and named in the
# reply, not silently left out. Deleting is not offered here.

BULK_MAX_ACCOUNTS = _MAX_PAGE_SIZE
_BULK_SWITCHES: dict[str, str] = {
    "azure": "can_use_azure",
    "bedrock": "can_use_bedrock",
    "waive": "concurrency_waived",
    "overflow": "can_overflow_cloud",
    "disable": "is_disabled",
}
BULK_ACTIONS: tuple[str, ...] = ("limit", "cloud-limit", *_BULK_SWITCHES)


def _bulk_ids(raw: str) -> list[int]:
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts or len(parts) > BULK_MAX_ACCOUNTS or not all(part.isdigit() for part in parts):
        raise HTTPException(status_code=400, detail=f"ids must be 1 to {BULK_MAX_ACCOUNTS} account ids.")
    return sorted({int(part) for part in parts})


@router.post("/users/bulk-update")
def bulk_update_users(
    ids: str = Form(""),
    action: str = Form(""),
    enabled: str = Form(""),
    value: str = Form(""),
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """One change to every account in ``ids`` (comma-separated).

    ``action`` is ``limit`` (``value``: the daily limit in USD),
    ``cloud-limit`` (``value``: the account's own cloud cap; empty = follow
    the default) or a switch with ``enabled=on|off``: ``azure``,
    ``bedrock``, ``waive``, ``overflow`` (people only: a service account is
    never moved), ``disable`` (never the admin's own account). Answers
    ``{changed, unchanged, skipped: [{username, reason}]}``. A bad request
    changes nothing. Plain ``def``.
    """
    if action not in BULK_ACTIONS:
        raise HTTPException(status_code=400, detail="action must be one of: " + ", ".join(BULK_ACTIONS) + ".")
    wanted = _bulk_ids(ids)
    amount: float | None = None
    on = False
    if action == "limit":
        amount = _limit_usd(value, "value")
    elif action == "cloud-limit":
        amount = None if value.strip() == "" else _limit_usd(value, "value")
    else:
        if enabled not in ("on", "off"):
            raise HTTPException(status_code=400, detail="enabled must be on or off.")
        on = enabled == "on"

    targets = list(session.exec(select(User).where(User.id.in_(wanted)).order_by(User.id)).all())  # type: ignore[union-attr]
    if len(targets) != len(wanted):
        raise HTTPException(status_code=400, detail="An account in ids no longer exists. Reload the table and try again.")

    field = {"limit": "daily_limit_usd", "cloud-limit": "cloud_daily_limit_usd"}.get(action) or _BULK_SWITCHES[action]
    new = amount if action in ("limit", "cloud-limit") else on
    changed: list[User] = []
    unchanged = 0
    skipped: list[dict[str, str]] = []
    for target in targets:
        if action == "disable" and on and target.id == admin_user.id:
            skipped.append({"username": target.username, "reason": "you cannot disable yourself"})
        elif action == "overflow" and target.is_service_account:
            skipped.append({"username": target.username, "reason": "a service account is never moved to the cloud"})
        elif getattr(target, field) == new:
            unchanged += 1
        else:
            setattr(target, field, new)
            session.add(target)
            changed.append(target)
    session.commit()

    names = [target.username for target in changed]
    if action == "overflow" and not on and changed:
        # As _overflow_changed does for one account: back on-prem at once.
        for name in names:
            overflow.release_account(session, name, "overflow turned off for this account")
        overflow.refresh()
    audit.note(accounts=names, changed=len(names), skipped=[item["username"] for item in skipped])
    logger.info(
        "Accounts changed together | admin={} action={} value={} changed={} unchanged={} skipped={} accounts={}",
        admin_user.username, action, new, len(names), unchanged, len(skipped), ",".join(names) or "-",
    )
    return {"ok": True, "action": action, "changed": len(names), "unchanged": unchanged, "skipped": skipped}


@router.post("/users/create")
async def create_app_account_web(
    request: Request,
    session: Session = Depends(get_session),
):
    """Create a service account (web UI form): an account for a program,
    used by API key only. The name is taken as given (the ``app_`` prefix
    older accounts carry is no longer added or needed). ``description``
    says what it is, for the dashboards that show it; ``org_code`` is the
    department it is for (its owners must be in it). ``owner_usernames``
    (comma-separated; ``owner_username`` is the older single field) are the
    people it is assigned to: each must be an existing person, checked
    before anything is created."""

    form = await request.form()
    username = _service_account_name(form.get("username"))
    daily_limit = form.get("daily_limit_usd")
    description = _description(form.get("description"))
    department = _department(session, form.get("org_code"))

    existing = session.exec(select(User).where(User.username == username)).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"User '{username}' already exists.")
    owners = _owners(
        session, _owner_names(f"{form.get('owner_usernames') or ''},{form.get('owner_username') or ''}"),
        department=department,
    )

    # Service accounts start waived from the concurrency limit.
    user = User(
        username=username, is_service_account=True, concurrency_waived=True,
        description=description, org_code=department,
    )
    if daily_limit is not None:
        user.daily_limit_usd = _limit_usd(daily_limit, "daily_limit_usd")
    session.add(user)
    session.commit()
    session.refresh(user)

    for owner in owners:
        session.add(AppOwner(app_id=user.id, owner_id=owner.id))
    session.commit()

    if _wants_json(request):
        return JSONResponse({"ok": True, "username": user.username, "owners": [o.username for o in owners]})
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/owner")
async def update_user_owner(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """Replace a service account's owners (comma-separated usernames): the
    people who see it on their dashboard. Each must be an existing person;
    with any name refused, nothing changes."""

    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    _service_account_or_400(target)

    form = await request.form()
    owners = _owners(
        session, _owner_names(form.get("owner_usernames")), _current_owner_ids(session, user_id),
        department_of(target),
    )

    session.execute(delete(AppOwner).where(AppOwner.app_id == user_id))
    for owner in owners:
        session.add(AppOwner(app_id=user_id, owner_id=owner.id))
    session.commit()

    return JSONResponse({"ok": True, "owners": [o.username for o in owners]})


@router.post("/users/{user_id}/limit")
async def update_user_limit(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
):

    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    form = await request.form()
    new_limit = form.get("new_limit")
    if new_limit is not None:
        target.daily_limit_usd = _limit_usd(new_limit, "new_limit")
        session.add(target)
        session.commit()

    if _wants_json(request):
        return JSONResponse({"ok": True, "daily_limit_usd": target.daily_limit_usd})
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/cloud-limit")
async def update_user_cloud_limit(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """Set the user's own cloud cap (Azure + Bedrock per day).

    Empty value clears it (NULL → the user follows the gateway-wide default,
    [app].cloud_daily_limit_usd); 0 is a $0 cap.
    """
    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    form = await request.form()
    raw = form.get("new_cloud_limit")
    if raw is not None:
        raw = str(raw).strip()
        if raw == "":
            target.cloud_daily_limit_usd = None
        else:
            target.cloud_daily_limit_usd = _limit_usd(raw, "new_cloud_limit")
        session.add(target)
        session.commit()

    if _wants_json(request):
        return JSONResponse({"ok": True, "cloud_daily_limit_usd": target.cloud_daily_limit_usd})
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/toggle-disable")
def toggle_disable(
    user_id: int,
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Flip the user's is_disabled flag. Refuses to disable yourself."""
    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    if target.id == admin_user.id and not target.is_disabled:
        raise HTTPException(status_code=400, detail="Cannot disable yourself.")
    target.is_disabled = not target.is_disabled
    session.add(target)
    session.commit()
    if _wants_json(request):
        return JSONResponse({"ok": True, "is_disabled": target.is_disabled})
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/toggle-azure")
def toggle_azure(
    user_id: int,
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Flip the user's can_use_azure flag."""
    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    target.can_use_azure = not target.can_use_azure
    session.add(target)
    session.commit()
    if _wants_json(request):
        return JSONResponse({"ok": True, "can_use_azure": target.can_use_azure})
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/toggle-bedrock")
def toggle_bedrock(
    user_id: int,
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Flip the user's can_use_bedrock flag."""
    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    target.can_use_bedrock = not target.can_use_bedrock
    session.add(target)
    session.commit()
    if _wants_json(request):
        return JSONResponse({"ok": True, "can_use_bedrock": target.can_use_bedrock})
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/toggle-concurrency-waive")
def toggle_concurrency_waive(
    user_id: int,
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Flip the user's concurrency_waived flag (exempt from the concurrency
    limit). Requests already in flight keep their slot until they finish."""
    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    target.concurrency_waived = not target.concurrency_waived
    session.add(target)
    session.commit()
    logger.info(
        "Concurrency limit {} | user={} admin={}",
        "waived" if target.concurrency_waived else "applied",
        target.username, admin_user.username,
    )
    if _wants_json(request):
        return JSONResponse({"ok": True, "concurrency_waived": target.concurrency_waived})
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/toggle-overflow")
def toggle_overflow(
    user_id: int,
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Flip the account's can_overflow_cloud flag: whether peak overflow
    (app/services/overflow.py) may serve its on-prem requests from the cloud.
    Turning it off moves the account back at once; a request already sent to
    the cloud finishes there."""
    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    target.can_overflow_cloud = not target.can_overflow_cloud
    session.add(target)
    session.commit()
    _overflow_changed(session, target, admin_user)
    if _wants_json(request):
        return JSONResponse({"ok": True, "can_overflow_cloud": target.can_overflow_cloud})
    return RedirectResponse(url="/admin", status_code=303)


# ── A service account that acts for its users ──
#
# ``acts_for_users``: every request of the account must name a person in
# X-On-Behalf-Of and is then that person's (``deps._on_behalf_of``): it lets
# whoever holds the account's key spend any person's budget, so it is an
# explicit switch, counted in the Special access strip.

def known_departments(session: Session) -> list[str]:
    """Every department a person is in (their ``org_code``, from their
    sign-in), in order: what a service account can be made for."""
    codes = session.exec(
        select(User.org_code).where(~service_account_clause(), User.org_code != "").distinct()
    ).all()
    return sorted({str(c).strip() for c in codes if c and str(c).strip()}, key=str.casefold)


def _department(session: Session, raw: object) -> str:
    """The department a service account is for, as an admin typed it: empty
    (none), or the org code of at least one person, returned as those
    people have it. A code nobody has is refused: it would be a typing
    mistake that hides the account from every owner."""
    text = str(raw or "").strip()
    if not text:
        return ""
    for code in known_departments(session):
        if same_department(code, text):
            return code
    raise HTTPException(
        status_code=400,
        detail=f"No user is in department '{text}'. Use an org code people sign in with.",
    )


def _department_or_400(target: User, department: str) -> None:
    """Only a service account is given a department here: a person's comes
    from their sign-in."""
    if department and not target.is_service_account:
        raise HTTPException(status_code=400, detail="A person's department comes from their sign-in.")


def _owners_outside(session: Session, target: User) -> list[str]:
    """The owners of ``target`` who are not in its department (they see
    nothing of it meanwhile), by name."""
    owners = session.exec(
        select(User).join(AppOwner, AppOwner.owner_id == User.id).where(AppOwner.app_id == target.id)
    ).all()
    return sorted(o.username for o in owners if not in_department(o, target))


@router.post("/users/{user_id}/department")
async def update_user_department(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """Say which department a service account is for (``org_code``: a code
    people sign in with; empty for none). Its owners see it, its key and
    its usage only while they are in that department. The owners are left
    as they are: ``outside`` names the ones who are not in it, for the
    admin to look at."""
    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    form = await request.form()
    department = _department(session, form.get("org_code"))
    _department_or_400(target, department)
    if target.is_service_account:
        target.org_code = department
        session.add(target)
        session.commit()
    if _wants_json(request):
        return JSONResponse({"ok": True, "org_code": target.org_code, "outside": _owners_outside(session, target)})
    return RedirectResponse(url="/admin", status_code=303)


DESCRIPTION_MAX = 120


def _description(raw: object) -> str:
    """A service account's description as an admin typed it: one line (any
    run of whitespace is one space), at most ``DESCRIPTION_MAX`` characters.
    It is shown beside the account's name on dashboards, so it stays
    short; pages escape it."""
    text = " ".join(str(raw or "").split())
    if len(text) > DESCRIPTION_MAX:
        raise HTTPException(
            status_code=400, detail=f"A description is at most {DESCRIPTION_MAX} characters.",
        )
    return text


def _description_or_400(target: User, description: str) -> None:
    """Only a service account is described: a person is named by their
    sign-in (``display_name``), which an admin does not edit."""
    if description and not target.is_service_account:
        raise HTTPException(status_code=400, detail="Only a service account has a description.")


@router.post("/users/{user_id}/description")
async def update_user_description(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """Say what a service account is (``description``, one line; empty
    clears it). Shown beside its name on its owners' dashboards and on the
    dashboard of every person it sent requests for."""
    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    form = await request.form()
    description = _description(form.get("description"))
    _description_or_400(target, description)
    target.description = description
    session.add(target)
    session.commit()
    if _wants_json(request):
        return JSONResponse({"ok": True, "description": target.description})
    return RedirectResponse(url="/admin", status_code=303)


def _acts_for_users_or_400(target: User, enabled: bool) -> None:
    """Only a service account acts for users: on a person's account the
    switch would let that person put requests on a colleague's bill."""
    if enabled and not target.is_service_account:
        raise HTTPException(status_code=400, detail="Only a service account can act for its users.")


@router.post("/users/{user_id}/acts-for-users")
async def set_acts_for_users(
    user_id: int,
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Turn a service account's ``acts_for_users`` on or off (explicit
    ``enabled=on|off``, never a flip: this decides whose budget its requests
    spend). On: every request must name a person in ``X-On-Behalf-Of`` and
    is billed to that person; off: its requests are its own again and the
    header is ignored."""
    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    form = await request.form()
    value = str(form.get("enabled") or "")
    if value not in ("on", "off"):
        raise HTTPException(status_code=400, detail="enabled must be on or off.")
    _acts_for_users_or_400(target, value == "on")
    target.acts_for_users = value == "on"
    session.add(target)
    session.commit()
    logger.info(
        "Acts for users {} | service_account={} admin={}",
        "on" if target.acts_for_users else "off", target.username, admin_user.username,
    )
    if _wants_json(request):
        return JSONResponse({"ok": True, "acts_for_users": target.acts_for_users})
    return RedirectResponse(url="/admin", status_code=303)


def _overflow_changed(session: Session, target: User, admin_user: User | None = None) -> None:
    """After an account's can_overflow_cloud was written: log it, and when
    it is now off, end the account's stays on the cloud and reload this
    worker's copy (the other workers follow within ``overflow.REFRESH_S``)."""
    logger.info(
        "Cloud overflow {} | user={} admin={}",
        "allowed" if target.can_overflow_cloud else "turned off",
        target.username, admin_user.username if admin_user else "-",
    )
    if not target.can_overflow_cloud:
        overflow.release_account(session, target.username, "overflow turned off for this account")
        overflow.refresh()


@router.post("/users/{user_id}/delete")
def delete_user(
    user_id: int,
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):

    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    # Prevent admin from deleting themselves
    if target.id == admin_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself.")

    # Clear every row that references users.id, then the user. PostgreSQL
    # enforces these foreign keys, so a missed one fails the delete with a 500.
    # usage_logs and usage_daily are deliberately KEPT: they are the billing
    # and audit record (no foreign key, so nothing forces their removal), and
    # deleting only one of them would make reports disagree with each other.
    # The log line below keeps the id -> account mapping those rows refer to.
    session.execute(delete(AppOwner).where(
        (AppOwner.app_id == user_id) | (AppOwner.owner_id == user_id)
    ))
    # Legacy single-owner column: superseded by app_owners, but migration
    # d5a2c3e67f04 copied it without clearing it, so old app accounts still
    # point at their original owner.
    session.execute(update(User).where(User.owner_id == user_id).values(owner_id=None))
    session.execute(delete(AnomalyEvent).where(AnomalyEvent.user_id == user_id))
    concurrency.delete_user_leases(session, user_id)
    deleted = (target.id, target.username, target.display_name or "", target.org_code or "")
    session.delete(target)
    session.commit()
    logger.warning(
        "User deleted | admin={} user_id={} username={} display_name={} org_code={} "
        "(usage_logs / usage_daily rows kept)",
        admin_user.username, *deleted,
    )

    if _wants_json(request):
        return JSONResponse({"ok": True, "deleted": user_id})
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/users/{user_id}/api-key")
def reveal_user_key(
    user_id: int,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    """Return one account's full API key, one deliberate request at a time.

    The user/app listings only carry masked keys, so this is the single way
    an admin gets a usable credential out of the panel — one account per
    call, logged with who asked for whose key.
    """
    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    logger.warning(
        "API key revealed | admin={} target={}", admin_user.username, target.username
    )
    return JSONResponse({"ok": True, "api_key": target.api_key})


@router.post("/users/{user_id}/refresh-key")
def refresh_user_key(
    user_id: int,
    session: Session = Depends(get_session),
):

    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    from app.models.schema import issue_api_key
    issue_api_key(target)
    session.add(target)
    session.commit()
    session.refresh(target)

    return JSONResponse({"ok": True, "api_key": target.api_key})


# ── API endpoints (Bearer token auth) ──

@router.get("/users")
def list_users_api(
    session: Session = Depends(get_session),
):
    users = session.exec(select(User)).all()

    # Build owner lookup
    all_app_owners = session.exec(select(AppOwner)).all()
    app_owners_map: dict[int, list[int]] = {}
    for ao in all_app_owners:
        app_owners_map.setdefault(ao.app_id, []).append(ao.owner_id)

    return [
        {
            "id": u.id,
            "username": u.username,
            # Masked: a bulk listing must not carry usable credentials.
            # The modal fetches the real key from /users/{id}/api-key.
            "api_key": mask_api_key(u.api_key),
            "daily_limit_usd": u.daily_limit_usd,
            "cloud_daily_limit_usd": u.cloud_daily_limit_usd,
            "concurrency_waived": u.concurrency_waived,
            "can_overflow_cloud": u.can_overflow_cloud,
            "is_service_account": u.is_service_account,
            "acts_for_users": u.acts_for_users,
            "description": u.description,
            "owner_ids": app_owners_map.get(u.id, []),
            "display_name": u.display_name,
            "org_code": u.org_code,
        }
        for u in users
    ]


@router.post("/users")
def create_user_api(
    body: dict,
    session: Session = Depends(get_session),
):
    """Create a new user account via API (requires admin JWT)."""

    username = (body.get("username") or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required.")

    existing = session.exec(select(User).where(User.username == username)).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"User '{username}' already exists.")

    _reject_retired_limits(body)
    _reject_retired_admin_flag(body)
    # What kind of account: "is_service_account" says so, and nothing else
    # does. A name starting with ``app_`` makes a person's account like any
    # other name (it made an app account when the name was the test).
    is_service = (
        _strict_bool(body["is_service_account"], "is_service_account")
        if "is_service_account" in body else False
    )
    if is_service:
        _service_account_name(username)
    # Owners are people, and only a service account has any: checked before
    # the account is created.
    department = _department(session, body.get("org_code"))
    if department and not is_service:
        raise HTTPException(status_code=400, detail="A person's department comes from their sign-in.")
    owners = _owners_by_id(session, body.get("owner_ids"), department=department)
    if owners and not is_service:
        raise HTTPException(status_code=400, detail="Only a service account has owners.")
    description = _description(body.get("description"))
    if description and not is_service:
        raise HTTPException(status_code=400, detail="Only a service account has a description.")
    # Service accounts start waived from the concurrency limit; an explicit
    # "concurrency_waived" in the body (handled below) still wins.
    new_user = User(
        username=username, is_service_account=is_service, concurrency_waived=is_service,
        description=description, org_code=department,
    )
    if "daily_limit_usd" in body:
        new_user.daily_limit_usd = _limit_usd(body["daily_limit_usd"], "daily_limit_usd")
    if "cloud_daily_limit_usd" in body:
        raw = body["cloud_daily_limit_usd"]
        new_user.cloud_daily_limit_usd = (
            None if raw is None else _limit_usd(raw, "cloud_daily_limit_usd")
        )
    if "concurrency_waived" in body:
        new_user.concurrency_waived = _strict_bool(body["concurrency_waived"], "concurrency_waived")
    session.add(new_user)
    session.commit()
    session.refresh(new_user)

    for owner in owners:
        session.add(AppOwner(app_id=new_user.id, owner_id=owner.id))
    session.commit()
    owner_ids = [owner.id for owner in owners]

    return {
        "ok": True,
        "id": new_user.id,
        "username": new_user.username,
        "api_key": new_user.api_key,
        "daily_limit_usd": new_user.daily_limit_usd,
        "is_service_account": new_user.is_service_account,
        "owner_ids": owner_ids,
    }


@router.patch("/users/{user_id}")
def update_user_api(
    user_id: int,
    body: dict,
    session: Session = Depends(get_session),
):
    target = session.exec(select(User).where(User.id == user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    _reject_retired_limits(body)
    _reject_retired_admin_flag(body)
    if "daily_limit_usd" in body:
        target.daily_limit_usd = _limit_usd(body["daily_limit_usd"], "daily_limit_usd")
    if "cloud_daily_limit_usd" in body:
        raw = body["cloud_daily_limit_usd"]
        target.cloud_daily_limit_usd = (
            None if raw is None else _limit_usd(raw, "cloud_daily_limit_usd")
        )
    if "concurrency_waived" in body:
        target.concurrency_waived = _strict_bool(body["concurrency_waived"], "concurrency_waived")
    overflow_written = "can_overflow_cloud" in body
    if overflow_written:
        target.can_overflow_cloud = _strict_bool(body["can_overflow_cloud"], "can_overflow_cloud")
    if "acts_for_users" in body:
        enabled = _strict_bool(body["acts_for_users"], "acts_for_users")
        _acts_for_users_or_400(target, enabled)
        target.acts_for_users = enabled
    if "description" in body:
        description = _description(body["description"])
        _description_or_400(target, description)
        target.description = description
    if "org_code" in body:
        department = _department(session, body["org_code"])
        _department_or_400(target, department)
        if target.is_service_account:
            target.org_code = department
    if "owner_ids" in body:
        current = _current_owner_ids(session, user_id)
        owners = _owners_by_id(
            session, body["owner_ids"], current, department_of(target) if target.is_service_account else "",
        )
        # A person's account gets no new owner; one it already has may stay
        # (or be taken away) like any other.
        if any(owner.id not in current for owner in owners):
            _service_account_or_400(target)
        session.execute(delete(AppOwner).where(AppOwner.app_id == user_id))
        for owner in owners:
            session.add(AppOwner(app_id=user_id, owner_id=owner.id))

    session.add(target)
    session.commit()
    session.refresh(target)
    if overflow_written:
        _overflow_changed(session, target)
    return {"ok": True, "user_id": target.id}


@router.get("/api/export/users.csv")
def export_users_csv(session: Session = Depends(get_session)):
    """Export all real users (service accounts left out) as CSV for mass mailing."""
    import csv
    import io

    # Last activity as a correlated subquery: one backwards probe of
    # ix_usage_user_date_cost per user, instead of a GROUP BY over the whole
    # (unbounded) usage_logs table.
    last_active_col = (
        select(func.max(UsageLog.created_at))
        .where(UsageLog.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    rows = session.exec(
        select(User, last_active_col)
        .where(~service_account_clause())
        .order_by(User.org_code, User.username)
    ).all()
    users = [u for u, _ in rows]
    last_active = {u.id: dt for u, dt in rows}

    monthly = get_monthly_all_users_usage(session)

    def fmt_dt(dt: datetime | None) -> str:
        if dt is None:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    buf = io.StringIO()
    buf.write("﻿")  # UTF-8 BOM so Excel opens CJK correctly
    writer = csv.writer(buf)
    writer.writerow([
        "username", "display_name", "org_code", "is_disabled",
        "can_use_azure", "can_use_bedrock",
        "daily_limit_usd", "created_at",
        "monthly_cost_usd", "monthly_requests", "last_active_at",
    ])
    for u in users:
        m = monthly.get(u.id or 0, {})
        writer.writerow([
            u.username,
            u.display_name or "",
            u.org_code or "",
            "true" if u.is_disabled else "false",
            "true" if u.can_use_azure else "false",
            "true" if u.can_use_bedrock else "false",
            f"{u.daily_limit_usd:.2f}",
            fmt_dt(u.created_at),
            f"{m.get('cost', 0.0):.6f}",
            m.get("requests", 0),
            fmt_dt(last_active.get(u.id or 0)),
        ])

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"users_{today}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
