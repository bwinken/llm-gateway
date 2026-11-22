"""
Aggregate DB data for the web dashboard.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy import case as sa_case
from sqlalchemy.orm import aliased as sa_aliased
from sqlmodel import Session, func, select

from app.core.timeutil import local_day_start_utc, local_month_start, local_today
from app.models.schema import (
    AppOwner,
    UsageLog,
    User,
    department_of,
    in_department,
    service_account_clause,
    is_legacy_api_key,
)
from app.services.usage_rollup import aggregate

# Days and months are local (Asia/Taipei) calendar days and months, the same
# day the daily quota resets on. Anything spanning more than today reads
# through ``usage_rollup.aggregate`` (closed days from ``usage_daily``, the
# rest from ``usage_logs``); only today-only figures query ``usage_logs``
# directly.


def _tomorrow():
    return local_today() + timedelta(days=1)


def _totals(entry: dict | None) -> dict:
    """An ``aggregate`` entry as the float/int dict the pages use."""
    entry = entry or {}
    return {
        "cost": float(entry.get("cost_usd", 0) or 0),
        "input_tokens": int(entry.get("input_tokens", 0)),
        "output_tokens": int(entry.get("output_tokens", 0)),
        "requests": int(entry.get("requests", 0)),
    }


def get_user_daily_summary(session: Session, user_id: int) -> dict:
    """Total tokens, cost, and request count for today (Asia/Taipei day).

    Also splits today's cost by backend (``azure_cost_usd`` /
    ``bedrock_cost_usd`` — the remainder is on-prem/vLLM) so the dashboard
    can show the per-cloud sub-budgets, and says how much of it was peak
    overflow (``overflow_cost_usd``, part of the two cloud figures).
    """
    today_start = local_day_start_utc()
    azure_case = sa_case((UsageLog.backend == "azure", UsageLog.cost_usd), else_=0)
    bedrock_case = sa_case((UsageLog.backend == "bedrock", UsageLog.cost_usd), else_=0)
    overflow_case = sa_case((UsageLog.overflow.is_(True), UsageLog.cost_usd), else_=0)  # type: ignore[attr-defined]
    stmt = (
        select(
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("total_input"),
            func.coalesce(func.sum(UsageLog.output_tokens), 0).label("total_output"),
            func.coalesce(func.sum(UsageLog.cost_usd), 0).label("total_cost"),
            func.count(UsageLog.id).label("total_requests"),
            func.coalesce(func.sum(azure_case), 0).label("azure_cost"),
            func.coalesce(func.sum(bedrock_case), 0).label("bedrock_cost"),
            func.coalesce(func.sum(overflow_case), 0).label("overflow_cost"),
        )
        .where(UsageLog.user_id == user_id)
        .where(UsageLog.created_at >= today_start)
    )
    row = session.exec(stmt).first()
    total_cost = float(row[2]) if row else 0.0
    azure_cost = float(row[4]) if row else 0.0
    bedrock_cost = float(row[5]) if row else 0.0
    return {
        "total_input_tokens": int(row[0]) if row else 0,
        "total_output_tokens": int(row[1]) if row else 0,
        "total_cost_usd": total_cost,
        "total_requests": int(row[3]) if row else 0,
        "azure_cost_usd": azure_cost,
        "bedrock_cost_usd": bedrock_cost,
        "vllm_cost_usd": total_cost - azure_cost - bedrock_cost,
        # The part of the cloud cost that is peak overflow: requests for an
        # on-prem model the gateway served from the cloud.
        "overflow_cost_usd": float(row[6]) if row else 0.0,
    }


def get_user_monthly_summary(session: Session, user_id: int) -> dict:
    """Total tokens and cost for the current local (Asia/Taipei) month."""
    t = _totals(aggregate(
        session, local_month_start(), _tomorrow(), user_ids=[user_id],
    ).get(()))
    return {
        "total_input_tokens": t["input_tokens"],
        "total_output_tokens": t["output_tokens"],
        "total_cost_usd": t["cost"],
        "total_requests": t["requests"],
    }


def get_daily_trends(session: Session, user_id: int, days: int = 30) -> list[dict]:
    """Return one row per local day for the last ``days`` days, today included.

    Days without usage are present with zeros. The chart plots one point per
    row on an evenly spaced axis, so a skipped day would draw the line
    straight across a quiet weekend and put non-adjacent days side by side.
    """
    first = local_today() - timedelta(days=days - 1)
    by_day = {}
    for (day,), entry in aggregate(
        session, first, _tomorrow(), ("day",), user_ids=[user_id],
    ).items():
        t = _totals(entry)
        by_day[day.isoformat()] = {
            "date": day.isoformat(),
            "reqs": t["requests"],
            "cost": round(t["cost"], 6),
            "input_tokens": t["input_tokens"],
            "output_tokens": t["output_tokens"],
        }
    out = []
    for i in range(days):
        day_str = (first + timedelta(days=i)).isoformat()
        out.append(by_day.get(day_str) or {
            "date": day_str, "reqs": 0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0,
        })
    return out


def human_user_ids(session: Session) -> set[int]:
    """Ids of every existing person, service accounts left out (``users`` is small)."""
    return {int(uid) for uid in session.exec(select(User.id).where(~service_account_clause())).all()}


def count_daily_active(per_day_user: dict[tuple, dict], humans: set[int]) -> dict:
    """``{day: distinct humans}`` from an ``aggregate`` keyed ``(day, user_id)``.

    Distinct-counting in Python over (day, user) groups replaces a
    ``count(DISTINCT user_id) ... GROUP BY day``, which made PostgreSQL sort
    every request row in the window."""
    out: dict = {}
    for day, uid in per_day_user:
        if uid in humans:
            out[day] = out.get(day, 0) + 1
    return out


def get_dau_trends(session: Session, days: int = 30) -> list[dict]:
    """Daily active people (app accounts excluded) for the last ``days``
    local days, today included. Days without activity are left out."""
    first = local_today() - timedelta(days=days - 1)
    per_day = count_daily_active(
        aggregate(session, first, _tomorrow(), ("day", "user_id")),
        human_user_ids(session),
    )
    return [{"date": day.isoformat(), "dau": n} for day, n in sorted(per_day.items())]


SPEND_BACKENDS = ("vllm", "azure", "bedrock")


def get_spend_trends(session: Session, days: int = 30) -> list[dict]:
    """Estimated spend of all accounts per local day and backend for the last
    ``days`` days, today included, every day present (zeros on quiet days:
    the chart's axis is evenly spaced). Read through ``aggregate``, so the
    same with or without the rollup. A backend outside ``SPEND_BACKENDS``
    counts in ``total`` only."""
    first = local_today() - timedelta(days=days - 1)
    by_day: dict[str, dict] = {}
    for (day, backend), entry in aggregate(session, first, _tomorrow(), ("day", "backend")).items():
        row = by_day.setdefault(day.isoformat(), {b: 0.0 for b in SPEND_BACKENDS} | {"total": 0.0})
        cost = float(entry["cost_usd"])
        if backend in SPEND_BACKENDS:
            row[backend] += cost
        row["total"] += cost
    out = []
    for i in range(days):
        day = (first + timedelta(days=i)).isoformat()
        row = by_day.get(day) or {b: 0.0 for b in SPEND_BACKENDS} | {"total": 0.0}
        out.append({"date": day, **{k: round(v, 4) for k, v in row.items()}})
    return out


def get_monthly_all_users_usage(
    session: Session, user_ids: Iterable[int] | None = None,
) -> dict[int, dict]:
    """Return per-user usage for the current local (Asia/Taipei) month.

    ``user_ids`` narrows the scan to those accounts (one page of the admin
    users table); ``None`` means every account."""
    out = {}
    for (uid,), entry in aggregate(
        session, local_month_start(), _tomorrow(), ("user_id",), user_ids=user_ids,
    ).items():
        t = _totals(entry)
        t["cost"] = round(t["cost"], 6)
        out[int(uid)] = t
    return out


def get_on_behalf_month(
    session: Session, service_account_ids: Iterable[int] | None = None,
) -> dict[int, dict]:
    """Per service account, what it sent for its users this local month
    (``usage_logs.via_user_id``; ``X-On-Behalf-Of``): cost, tokens, requests
    and ``users``, how many accounts it was sent for. That usage is the
    users' own (their bill, their limits), so it is in none of the service
    account's figures: this is how much went through it.

    ``service_account_ids`` narrows the read to those accounts; ``None``
    means all of them. Accounts with nothing are absent."""
    out: dict[int, dict] = {}
    for (via, _uid), entry in aggregate(
        session, local_month_start(), _tomorrow(), ("via_user_id", "user_id"),
        via_user_ids=service_account_ids,
    ).items():
        if not via:
            continue  # sent with the account's own key
        t = _totals(entry)
        row = out.setdefault(int(via), {"cost": 0.0, "input_tokens": 0, "output_tokens": 0, "requests": 0, "users": 0})
        for key in ("cost", "input_tokens", "output_tokens", "requests"):
            row[key] += t[key]
        row["users"] += 1
    for row in out.values():
        row["cost"] = round(row["cost"], 6)
    return out


def get_user_on_behalf_today(session: Session, user_id: int) -> list[dict]:
    """What service accounts sent for this user today (local day), by
    service account, highest cost first: ``service_account`` (its name, or
    ``#<id>`` once it was deleted), ``description`` when an admin wrote one,
    ``cost_usd``, ``requests``. Part of the
    user's own figures for the day (``get_user_daily_summary``), never added
    to them. Today only, so read from ``usage_logs``."""
    rows = session.exec(
        select(UsageLog.via_user_id, func.coalesce(func.sum(UsageLog.cost_usd), 0), func.count(UsageLog.id))
        .where(UsageLog.user_id == user_id)
        .where(UsageLog.created_at >= local_day_start_utc())
        .where(UsageLog.via_user_id != 0)
        .group_by(UsageLog.via_user_id)
    ).all()
    if not rows:
        return []
    labels = service_account_labels(session, [int(via) for via, _, _ in rows])
    out = [
        {**labels[int(via)], "cost_usd": round(float(cost), 6), "requests": int(n)}
        for via, cost, n in rows
    ]
    out.sort(key=lambda r: (-r["cost_usd"], r["service_account"]))
    return out


def service_account_labels(session: Session, ids: Iterable[int]) -> dict[int, dict]:
    """How a page names the service accounts a person's usage came through
    (``usage_logs.via_user_id``): ``{id: {"service_account": its name, or
    "#<id>" once it was deleted (the usage is kept), "description": what an
    admin wrote about it}}``. ``description`` is absent when there is none."""
    ids = sorted({int(i) for i in ids})
    found = {
        int(uid): (username, description)
        for uid, username, description in session.exec(
            select(User.id, User.username, User.description).where(User.id.in_(ids))  # type: ignore[attr-defined]
        ).all()
    } if ids else {}
    out: dict[int, dict] = {}
    for i in ids:
        username, description = found.get(i, (f"#{i}", ""))
        out[i] = {"service_account": username}
        if description:
            out[i]["description"] = description
    return out


SOURCE_OWN_KEY = "own_key"
SOURCE_SERVICE_ACCOUNT = "service_account"


def get_usage_sources(session: Session, user: User, period: str = "month") -> list[dict]:
    """Where ``user``'s usage came in, and which models it went to: one
    entry for the account's own key (``source`` ``own_key``) and one per
    service account that sent requests for it (``source``
    ``service_account``, with ``service_account`` and, when an admin wrote
    one, ``description``; ``X-On-Behalf-Of``). Each has its totals and
    ``models``, one row per model and backend in the order every page lists
    models, shaped like ``get_model_breakdown``'s rows.

    The same window and the same rows as the Cost by Model card
    (``period`` ``"month"`` or ``"day"``; cloud backends the account may no
    longer use left out, peak overflow kept: ``model_catalog.visible_usage_rows``),
    only split by the way they came in, so the entries add up to that card's
    total. The own key comes first, then the service accounts by cost.
    Entries without usage are absent. Read through ``aggregate``."""
    from app.services import model_catalog

    start = local_today() if period == "day" else local_month_start()
    by_via: dict[int, list[dict]] = {}
    for (via, model, model_type, backend, overflow), entry in aggregate(
        session, start, _tomorrow(), ("via_user_id", "model", "model_type", "backend", "overflow"),
        user_ids=[user.id],
    ).items():
        by_via.setdefault(int(via or 0), []).append({
            "model": model,
            "type": model_type,
            "backend": backend or "vllm",
            "overflow": bool(overflow),
            "requests": int(entry["requests"]),
            "input_tokens": int(entry["input_tokens"]),
            "output_tokens": int(entry["output_tokens"]),
            "cost_usd": round(float(entry["cost_usd"]), 6),
        })
    catalog = model_catalog.current_catalog_keys() if by_via else {}
    labels = service_account_labels(session, [via for via in by_via if via])
    sources = []
    for via, rows in by_via.items():
        rows = model_catalog.sort_usage_rows(model_catalog.visible_usage_rows(rows, user), catalog)
        if not rows:
            continue
        who = {"source": SOURCE_SERVICE_ACCOUNT, **labels[via]} if via else {"source": SOURCE_OWN_KEY}
        sources.append({
            **who,
            "requests": sum(r["requests"] for r in rows),
            "input_tokens": sum(r["input_tokens"] for r in rows),
            "output_tokens": sum(r["output_tokens"] for r in rows),
            "cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
            "models": rows,
        })
    sources.sort(key=lambda s: (
        s["source"] != SOURCE_OWN_KEY, -s["cost_usd"], s.get("service_account", ""),
    ))
    return sources


def get_monthly_overview(session: Session, leaderboard_size: int = 10) -> dict:
    """Everything the admin page shows about the current month, from ONE
    per-user aggregate over ``usage_logs`` (``get_monthly_all_users_usage``)
    plus a ``users`` lookup.

    The month window is the largest scan the page does — millions of rows on
    a busy month — so the totals, both leaderboards and the department table
    are derived here in Python instead of each re-scanning it. Returns:

      ``totals``            cost / input_tokens / output_tokens / requests
      ``per_user``          the per-user map (``get_monthly_all_users_usage``)
      ``app_leaderboard`` / ``user_leaderboard``  top-N by monthly cost; a
                            service account's rank counts what it sent for its
                            users too (``for_users_*``, billed to them)
      ``departments``       people (app accounts excluded) grouped by
                            ``org_code`` ("Unknown" when blank), by cost desc
    """
    per_user = get_monthly_all_users_usage(session)
    # What each service account sent for its users (billed to them, so in
    # ``per_user`` under the users): shown beside its own, and ranked with it.
    for_users = get_on_behalf_month(session)
    # Every user, unfiltered: ``users`` is small next to ``usage_logs``, and an
    # IN list of every active id would hit bind-parameter limits.
    users = {
        int(uid): (username, display_name, org_code, bool(is_service))
        for uid, username, display_name, org_code, is_service in session.exec(
            select(User.id, User.username, User.display_name, User.org_code, User.is_service_account)
        ).all()
    } if per_user or for_users else {}

    totals = {"cost": 0.0, "input_tokens": 0, "output_tokens": 0, "requests": 0}
    apps: list[dict] = []
    people: list[dict] = []
    departments: dict[str, dict] = {}
    for uid, u in per_user.items():
        totals["cost"] += u["cost"]
        totals["input_tokens"] += u["input_tokens"]
        totals["output_tokens"] += u["output_tokens"]
        totals["requests"] += u["requests"]
        if uid not in users:
            continue  # rows of a since-deleted account count only in the totals
        username, display_name, org_code, is_service = users[uid]
        row = {
            "id": uid,
            "username": username,
            "display_name": display_name,
            "org_code": org_code,
            "monthly_cost": u["cost"],
            "monthly_input": u["input_tokens"],
            "monthly_output": u["output_tokens"],
            "monthly_reqs": u["requests"],
        }
        if is_service:
            sent = for_users.get(uid, {})
            row.update(for_users_cost=sent.get("cost", 0.0), for_users_reqs=sent.get("requests", 0),
                       for_users_count=sent.get("users", 0))
            apps.append(row)
            continue
        people.append(row)
        dept = departments.setdefault(org_code or "Unknown", {
            "department": org_code or "Unknown", "user_count": 0, "cost": 0.0,
            "input_tokens": 0, "output_tokens": 0, "requests": 0,
        })
        dept["user_count"] += 1
        dept["cost"] += u["cost"]
        dept["input_tokens"] += u["input_tokens"]
        dept["output_tokens"] += u["output_tokens"]
        dept["requests"] += u["requests"]

    # A service account whose month is only what it sent for its users has
    # no usage of its own: it still belongs on the leaderboard.
    listed = {row["id"] for row in apps}
    for uid, sent in for_users.items():
        if uid in listed or uid not in users or not users[uid][3]:
            continue
        username, display_name, org_code, _ = users[uid]
        apps.append({
            "id": uid, "username": username, "display_name": display_name, "org_code": org_code,
            "monthly_cost": 0.0, "monthly_input": 0, "monthly_output": 0, "monthly_reqs": 0,
            "for_users_cost": sent["cost"], "for_users_reqs": sent["requests"], "for_users_count": sent["users"],
        })

    def _top(rows: list[dict]) -> list[dict]:
        return sorted(
            rows, key=lambda r: r["monthly_cost"] + r.get("for_users_cost", 0.0), reverse=True,
        )[:leaderboard_size]

    totals["cost"] = round(totals["cost"], 4)
    for dept in departments.values():
        dept["cost"] = round(dept["cost"], 6)
    return {
        "totals": totals,
        "per_user": per_user,
        "app_leaderboard": _top(apps),
        "user_leaderboard": _top(people),
        "departments": sorted(departments.values(), key=lambda d: d["cost"], reverse=True),
    }


def _assigned_apps(session: Session, owner_id: int) -> tuple[list[User], list[User]]:
    """The service accounts assigned to this person (``app_owners``), as
    ``(seen, withheld)``: the ones they may see, and the ones made for a
    department they are not in (``schema.in_department``)."""
    owner = session.get(User, owner_id)
    if owner is None:
        return [], []
    app_ids = select(AppOwner.app_id).where(AppOwner.owner_id == owner_id)
    apps = session.exec(select(User).where(User.id.in_(app_ids))).all()  # type: ignore[attr-defined]
    seen = [a for a in apps if in_department(owner, a)]
    return seen, [a for a in apps if a not in seen]


def get_withheld_apps(session: Session, owner_id: int) -> list[dict]:
    """The service accounts assigned to this person that they may not see
    now, because each is for another department than theirs: ``username``
    and ``department``, by name. Nothing else of the account is given."""
    _, withheld = _assigned_apps(session, owner_id)
    return sorted(
        ({"username": a.username, "department": department_of(a)} for a in withheld),
        key=lambda a: a["username"],
    )


def owners_to_review(session: Session) -> list[dict]:
    """For the admin: the service accounts with an owner who should no
    longer be one as things stand. Per account ``id``, ``username``,
    ``department`` and ``owners``, each ``{"username", "org_code",
    "reason"}`` with ``reason`` ``department`` (the account is for a
    department the owner is not in: ``schema.in_department``, so they see
    nothing of it meanwhile) or ``disabled`` (the owner's account is
    disabled: how the gateway knows someone left). By account name.

    Read from ``users`` and ``app_owners`` as they are now, so it clears
    itself once an admin removes the owner, changes the department or the
    person signs in from the right one. A person's department is what their
    last sign-in said: the gateway learns of a move when they next sign in."""
    owner = sa_aliased(User)
    rows = session.exec(
        select(User, owner)
        .join(AppOwner, AppOwner.app_id == User.id)
        .join(owner, owner.id == AppOwner.owner_id)
        .order_by(User.username, owner.username)
    ).all()
    found: dict[int, dict] = {}
    for account, person in rows:
        if not in_department(person, account):
            reason = "department"
        elif person.is_disabled:
            reason = "disabled"
        else:
            continue
        entry = found.setdefault(int(account.id), {
            "id": account.id, "username": account.username, "department": department_of(account), "owners": [],
        })
        entry["owners"].append({"username": person.username, "org_code": person.org_code or "", "reason": reason})
    return list(found.values())


FLOW_TOP_USERS = 10


def get_on_behalf_users(
    session: Session, service_account_ids: Iterable[int], top: int = FLOW_TOP_USERS,
) -> dict[int, dict]:
    """Per service account, who it sent requests for this local month and
    on which backend (``X-On-Behalf-Of``): for its owner's flow chart.

    ``{id: {"rows": [...], "users": n}}``. A row is one user on one backend:
    ``user`` (the username, or ``#<id>`` for a deleted account),
    ``display_name``, ``backend`` and the usual totals. Only the ``top``
    users by cost (tokens, then name, where the cost is the same: on-prem
    may be priced at $0) are named; the rest are one row per backend with
    ``user`` ``""`` and ``others``, how many users it stands for. ``users``
    counts all of them. Accounts with nothing are absent. It is those
    users' usage (their bills), read through ``aggregate``."""
    ids = sorted({int(i) for i in service_account_ids})
    per: dict[int, dict[int, dict[str, dict]]] = {}
    for (via, uid, backend), entry in aggregate(
        session, local_month_start(), _tomorrow(), ("via_user_id", "user_id", "backend"), via_user_ids=ids,
    ).items():
        if not via:
            continue
        per.setdefault(int(via), {}).setdefault(int(uid), {})[backend or "vllm"] = _totals(entry)
    if not per:
        return {}
    people = {
        int(uid): (username, display_name or "")
        for uid, username, display_name in session.exec(
            select(User.id, User.username, User.display_name)
            .where(User.id.in_(sorted({uid for users in per.values() for uid in users})))  # type: ignore[attr-defined]
        ).all()
    }

    def _sum(backends: dict[str, dict], key: str):
        return sum(t[key] for t in backends.values())

    def _in_order(backends: dict[str, dict]) -> list[tuple[str, dict]]:
        """On-prem, Azure, Bedrock, then any other: the pages' backend order."""
        return sorted(backends.items(), key=lambda item: (
            SPEND_BACKENDS.index(item[0]) if item[0] in SPEND_BACKENDS else len(SPEND_BACKENDS), item[0],
        ))

    out: dict[int, dict] = {}
    for via, users in per.items():
        ranked = sorted(users, key=lambda uid: (
            -_sum(users[uid], "cost"),
            -(_sum(users[uid], "input_tokens") + _sum(users[uid], "output_tokens")),
            people.get(uid, (f"#{uid}", ""))[0],
        ))
        rows: list[dict] = []
        for uid in ranked[:top]:
            username, display_name = people.get(uid, (f"#{uid}", ""))
            for backend, t in _in_order(users[uid]):
                rows.append({
                    "user": username, "display_name": display_name, "backend": backend,
                    "requests": t["requests"], "input_tokens": t["input_tokens"],
                    "output_tokens": t["output_tokens"], "cost_usd": round(t["cost"], 6),
                })
        rest: dict[str, dict] = {}
        for uid in ranked[top:]:
            for backend, t in _in_order(users[uid]):
                row = rest.setdefault(backend, {
                    "user": "", "display_name": "", "backend": backend, "others": len(ranked) - top,
                    "requests": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                })
                row["requests"] += t["requests"]
                row["input_tokens"] += t["input_tokens"]
                row["output_tokens"] += t["output_tokens"]
                row["cost_usd"] = round(row["cost_usd"] + t["cost"], 6)
        out[via] = {"rows": rows + [row for _, row in _in_order(rest)], "users": len(ranked)}
    return out


def get_owned_apps_summary(session: Session, owner_id: int) -> list[dict]:
    """The service accounts this user owns (``app_owners``), each with this
    local month's usage: the totals and one ``models`` row per model and
    backend, in the order every page lists models. One usage read for all
    of them, and one for ``on_behalf``: what the account sent for its users
    (``get_on_behalf_month``), which is on their bills and in none of the
    account's own figures. ("apps" in the names here is the older word for service
    accounts.)

    Only the accounts the owner may see now: one made for another
    department than the owner's is left out (``schema.in_department``;
    ``get_withheld_apps`` names those)."""
    apps, _ = _assigned_apps(session, owner_id)
    if not apps:
        return []

    from app.services import model_catalog

    rows_by_id: dict[int, list[dict]] = {}
    for (uid, model, model_type, backend), entry in aggregate(
        session, local_month_start(), _tomorrow(), ("user_id", "model", "model_type", "backend"),
        user_ids=[a.id for a in apps],
    ).items():
        rows_by_id.setdefault(int(uid), []).append({
            "model": model,
            "type": model_type,
            "backend": backend or "vllm",
            "requests": int(entry["requests"]),
            "input_tokens": int(entry["input_tokens"]),
            "output_tokens": int(entry["output_tokens"]),
            "cost_usd": round(float(entry["cost_usd"]), 6),
        })
    catalog = model_catalog.current_catalog_keys() if rows_by_id else {}
    # What each sent for its users (X-On-Behalf-Of): on those users' bills,
    # so apart from the account's own month below.
    on_behalf = get_on_behalf_month(session, [a.id for a in apps])
    nothing = {"cost": 0.0, "input_tokens": 0, "output_tokens": 0, "requests": 0, "users": 0}

    def _one(app: User) -> dict:
        models = model_catalog.sort_usage_rows(rows_by_id.get(app.id, []), catalog)
        return {
            "id": app.id,
            "username": app.username,
            "description": app.description or "",
            "department": department_of(app),
            "api_key": app.api_key,
            "legacy_key": is_legacy_api_key(app.api_key),
            "daily_limit_usd": app.daily_limit_usd,
            "acts_for_users": bool(app.acts_for_users),
            "on_behalf": dict(on_behalf.get(app.id, nothing)),
            "monthly_cost": round(sum(r["cost_usd"] for r in models), 6),
            "monthly_input": sum(r["input_tokens"] for r in models),
            "monthly_output": sum(r["output_tokens"] for r in models),
            "monthly_reqs": sum(r["requests"] for r in models),
            "models": models,
        }

    return [_one(app) for app in apps]


def get_model_breakdown(
    session: Session, user_id: int, period: str = "month",
) -> list[dict]:
    """Per-model usage breakdown, split by backend.

    Grouped by (model alias, backend) so the dashboard can show which models
    the user's spend went to, with on-prem (vLLM) and cloud (Azure / Bedrock)
    portions kept separate. ``period`` picks the window: ``"month"`` is the
    local (Asia/Taipei) month — the same window as
    ``get_user_monthly_summary`` so the per-model costs sum to the Monthly
    Cost card; ``"day"`` is the local day — the same window as
    ``get_user_daily_summary`` and the Today's Cost card. A row with
    ``overflow`` true is cloud usage from peak overflow
    (``app/services/overflow.py``): shown to the account whatever cloud
    access it has, since it is on its bill.
    """
    start = local_today() if period == "day" else local_month_start()
    groups = aggregate(
        session, start, _tomorrow(), ("model", "model_type", "backend", "overflow"), user_ids=[user_id],
    )
    rows = [
        {
            "model": model,
            "type": model_type,
            "backend": backend or "vllm",
            # Peak overflow: a cloud model the gateway served in place of a
            # congested on-prem one. Its own row, apart from the same model
            # asked for by name.
            "overflow": bool(overflow),
            "requests": int(entry["requests"]),
            "input_tokens": int(entry["input_tokens"]),
            "output_tokens": int(entry["output_tokens"]),
            "cost_usd": round(float(entry["cost_usd"]), 6),
        }
        for (model, model_type, backend, overflow), entry in groups.items()
    ]
    rows.sort(key=lambda r: r["cost_usd"], reverse=True)
    return rows


def get_today_totals(session: Session) -> dict:
    """All-account totals for today (local day): cost (and its Azure / AWS
    parts), requests, and how many people (app accounts excluded) made at
    least one request."""
    today_start = local_day_start_utc()
    azure_case = sa_case((UsageLog.backend == "azure", UsageLog.cost_usd), else_=0)
    bedrock_case = sa_case((UsageLog.backend == "bedrock", UsageLog.cost_usd), else_=0)
    row = session.exec(
        select(
            func.coalesce(func.sum(UsageLog.cost_usd), 0),
            func.count(UsageLog.id),
            func.coalesce(func.sum(azure_case), 0),
            func.coalesce(func.sum(bedrock_case), 0),
        ).where(UsageLog.created_at >= today_start)
    ).first()
    # Distinct users first, counted outside: lets PostgreSQL hash the
    # day's user ids instead of sorting every request row of the day.
    active_ids = (
        select(UsageLog.user_id)
        .join(User, UsageLog.user_id == User.id)
        .where(UsageLog.created_at >= today_start)
        .where(~service_account_clause())
        .distinct()
        .subquery()
    )
    active = session.exec(select(func.count()).select_from(active_ids)).one()
    return {
        "cost": round(float(row[0]), 4) if row else 0.0,
        "requests": int(row[1]) if row else 0,
        "active_users": int(active or 0),
        "azure_cost": round(float(row[2]), 4) if row else 0.0,
        "bedrock_cost": round(float(row[3]), 4) if row else 0.0,
    }


def get_budget_pressure(session: Session, threshold: float = 0.9) -> list[dict]:
    """Enabled accounts whose spend today has reached ``threshold`` of their
    daily limit. Accounts with a $0 limit are left out: they are blocked on
    purpose, not running out.
    Sorted by share used, highest first — the "about to get 429s" list."""
    today_start = local_day_start_utc()
    spent = (
        select(UsageLog.user_id, func.sum(UsageLog.cost_usd).label("cost"))
        .where(UsageLog.created_at >= today_start)
        .group_by(UsageLog.user_id)
        .subquery()
    )
    rows = session.exec(
        select(User.id, User.username, User.display_name, User.daily_limit_usd, spent.c.cost)
        .join(spent, spent.c.user_id == User.id)
        .where(User.daily_limit_usd > 0)
        .where(~User.is_disabled)
    ).all()
    out = []
    for uid, username, display_name, limit, cost in rows:
        cost = float(cost or 0)
        share = cost / float(limit)
        if share >= threshold:
            out.append({
                "id": uid, "username": username, "display_name": display_name or "",
                "cost": round(cost, 4), "limit": float(limit), "percent": round(share * 100, 1),
            })
    out.sort(key=lambda r: r["percent"], reverse=True)
    return out


QUEUE_OVERLOADED = 10  # waiting requests at which a server is shown as overloaded


def summarize_server_status(servers: list[dict]) -> dict:
    """Roll per-model on-prem status up for a page header.

    ``servers`` items carry ``name``, ``alive`` and ``waiting`` (None when the
    server's /metrics is unavailable). Returns counts plus the names behind
    each non-healthy bucket, so a page can say *which* models need a look.
    """
    down = [s["name"] for s in servers if not s["alive"]]
    queueing = [
        {"name": s["name"], "waiting": s["waiting"]}
        for s in servers
        if s["alive"] and s.get("waiting")
    ]
    queueing.sort(key=lambda s: s["waiting"], reverse=True)
    return {
        "total": len(servers),
        "online": len(servers) - len(down),
        "down": down,
        "queueing": queueing,
        "overloaded": [s for s in queueing if s["waiting"] >= QUEUE_OVERLOADED],
    }
