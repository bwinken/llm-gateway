"""
What an account is told about itself, as data: the one place each answer is
put together.

``GET /v1/me``, ``/v1/status``, ``/v1/usage``, ``/v1/service-accounts`` and
``/v1/notices`` (by API key, ``app/routers/v1_api.py``) and the dashboard's
``GET /dashboard/api/*`` of the same names (by SSO,
``app/routers/dashboard/api.py``) are the same answers for two ways of
signing in. Each route authenticates and calls the function here; neither
router calls the other. A function takes the ``User`` the route
authenticated and returns the response body. It does not care how that
user signed in. The one difference between the two is not decided here:
an admin by the token's scope, which only the SSO sign-in can make one,
sees the cloud models whatever their own access
(``model_catalog.hidden_cloud_backends``).

What the programs outside this repository are built against is decided
here: change an answer by addition only (never rename or remove a key, or
respell a ``state`` / ``backend`` value) and never return a ``null``: a key
without a value is left out.

The readers these call (``deps.get_today_usage`` and the others) each open
a short-lived session of their own, so a function here is blocking: call it
from a plain ``def`` handler or through ``run_in_threadpool``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.config import (
    get_cloud_budget_fallback,
    get_concurrency_cloud_limit,
    get_concurrency_offpeak,
    get_concurrency_settings,
)
from app.core.deps import (
    _enforce_daily_limit,
    effective_cloud_limit,
    get_client_notices,
    get_model_changes,
    get_owned_apps,
    get_today_usage,
    get_usage_history,
)
from app.core.timeutil import (
    LOCAL_TZ,
    local_month_start,
    local_today,
    next_local_midnight,
    seconds_until_local_midnight,
)
from app.models.schema import User, is_legacy_api_key
from app.services import client_rules, concurrency, model_catalog
from app.services.model_order import natural_key

# How far back ``usage`` goes at most.
USAGE_MAX_DAYS = 90


def _usd(value: float) -> float:
    """Round to the precision ``usage_logs.cost_usd`` stores (Numeric(12, 6))."""
    return round(value, 6)


def _remaining(limit: float, used: float) -> float:
    return _usd(max(0.0, limit - used))


def me(user: User) -> dict:
    """The account and today's budget: ``GET /v1/me``. Every amount is a
    number (every account has a finite limit); ``cloud`` and the off-peak
    hours are there only when they apply. Reads today's usage."""
    today = get_today_usage(user)

    daily_limit = max(0.0, user.daily_limit_usd)
    daily_remaining = _remaining(daily_limit, today["total_cost_usd"])

    azure_access = user.can_use_azure
    bedrock_access = user.can_use_bedrock
    # The cap only means something to a user who may reach a cloud backend.
    cloud_limit = effective_cloud_limit(user) if (azure_access or bedrock_access) else None
    cloud_used = today["azure_cost_usd"] + today["bedrock_cost_usd"]
    cloud_remaining = (
        daily_remaining if cloud_limit is None
        else min(daily_remaining, _remaining(cloud_limit, cloud_used))
    )

    def _backend(access: bool, used: float, remaining: float) -> dict:
        return {
            "access": access,
            "used_usd": _usd(used),
            "remaining_usd": remaining if access else 0.0,
        }

    mode, limit = get_concurrency_settings()
    concurrency_applies = mode != "off" and not user.concurrency_waived
    local_now = datetime.now(LOCAL_TZ)
    offpeak = get_concurrency_offpeak()

    return {
        "object": "account",
        "username": user.username,
        "display_name": user.display_name,
        # Kept for clients built against it; always false. An API key has no
        # admin right: that is the AuthCenter token's scope, on the web UI.
        "is_admin": False,
        "legacy_key": is_legacy_api_key(user.api_key),
        "daily": {
            "limit_usd": daily_limit,
            "used_usd": _usd(today["total_cost_usd"]),
            "remaining_usd": daily_remaining,
            "requests": today["total_requests"],
            "input_tokens": today["total_input_tokens"],
            "output_tokens": today["total_output_tokens"],
            "enforced": _enforce_daily_limit(),
            "timezone": str(LOCAL_TZ),
            "resets_at": next_local_midnight(local_now).isoformat(),
            "resets_in_seconds": seconds_until_local_midnight(),
        },
        "backends": {
            "onprem": _backend(True, today["vllm_cost_usd"], daily_remaining),
            "azure": _backend(
                azure_access,
                today["azure_cost_usd"],
                cloud_remaining,
            ),
            "aws": _backend(
                bedrock_access,
                today["bedrock_cost_usd"],
                cloud_remaining,
            ),
        },
        **({"cloud": {
            "limit_usd": cloud_limit,
            "used_usd": _usd(cloud_used),
            "remaining_usd": cloud_remaining,
        }} if cloud_limit is not None else {}),
        "cloud_budget_fallback": get_cloud_budget_fallback(),
        # "limit" is for local (on-prem) requests; cloud requests are counted
        # apart, against "cloud_limit" (only shown with cloud access).
        "concurrency": (
            {
                "mode": mode,
                "limit": limit,
                **({"cloud_limit": get_concurrency_cloud_limit()}
                   if azure_access or bedrock_access else {}),
                # Off-peak hours lift "limit" (local only); shown only when set.
                **({"offpeak": {
                    "start": offpeak["start"],
                    "end": offpeak["end"],
                    "weekends": offpeak["weekends"],
                    "active": concurrency.in_offpeak(offpeak, local_now),
                }} if offpeak["start"] or offpeak["weekends"] else {}),
            } if concurrency_applies else {"mode": "off"}
        ),
    }


def status(user: User) -> dict:
    """The models the account can see and whether they are up:
    ``GET /v1/status``. No database read."""
    models = model_catalog.visible_models(user)
    onprem = [m for m in models if m["backend"] == "onprem"]
    return {
        "object": "status",
        "onprem": {
            "total": len(onprem),
            "up": sum(1 for m in onprem if m["state"] == "up"),
            "down": [m["id"] for m in onprem if m["state"] == "down"],
        },
        "models": models,
    }


def _model_usage_row(row: dict) -> dict:
    """One model's usage as the account endpoints give it: the backend under
    its client-facing name (``onprem`` / ``azure`` / ``aws``)."""
    out = {
        "model": row["model"],
        "backend": model_catalog.BACKEND_NAMES.get(row["backend"], row["backend"]),
        "type": row["type"],
        "requests": row["requests"],
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "cost_usd": row["cost_usd"],
    }
    if "overflow" in row:
        # The account's own usage (``/v1/usage``). True: it asked for an
        # on-prem model and the gateway served it from this cloud model
        # while on-prem was congested. Service accounts are never moved, so
        # their rows have no such key.
        out["overflow"] = bool(row["overflow"])
    return out


def _usage_source(source: dict) -> dict:
    """One way an account's usage came in (``stats.get_usage_sources``) as
    ``/v1/usage`` gives it: its own key, or a service account that sent
    requests for it, with the totals and the models they went to."""
    out = {"source": source["source"]}
    for key in ("service_account", "description"):
        if source.get(key):
            out[key] = source[key]
    out.update(
        requests=source["requests"],
        input_tokens=source["input_tokens"],
        output_tokens=source["output_tokens"],
        cost_usd=_usd(source["cost_usd"]),
        models=[_model_usage_row(r) for r in source["models"]],
    )
    return out


def usage(user: User, days: int) -> dict:
    """The account's usage per local day for the last ``days`` days, and
    today and this month by model and by the way it came in:
    ``GET /v1/usage``."""
    history = get_usage_history(user, days)
    catalog = model_catalog.current_catalog_keys()

    def by_model(rows: list[dict], sources: list[dict]) -> dict:
        """Totals and one row per model and backend, in the dashboard's
        order, without the cloud backends the account may not use; then
        the same usage by the way it came in."""
        rows = model_catalog.sort_usage_rows(model_catalog.visible_usage_rows(rows, user), catalog)
        return {
            "requests": sum(r["requests"] for r in rows),
            "input_tokens": sum(r["input_tokens"] for r in rows),
            "output_tokens": sum(r["output_tokens"] for r in rows),
            "cost_usd": _usd(sum(r["cost_usd"] for r in rows)),
            "models": [_model_usage_row(r) for r in rows],
            "sources": [_usage_source(s) for s in sources],
        }

    return {
        "object": "usage",
        "timezone": str(LOCAL_TZ),
        "days": [
            {
                "date": day["date"],
                "requests": day["reqs"],
                "input_tokens": day["input_tokens"],
                "output_tokens": day["output_tokens"],
                "cost_usd": day["cost"],
            }
            for day in history["days"]
        ],
        "today": {
            "date": local_today().isoformat(),
            **by_model(history["today_models"], history["today_sources"]),
        },
        "month": {
            "month": local_month_start().strftime("%Y-%m"),
            **by_model(history["models"], history["sources"]),
        },
    }


def service_account_row(account: dict) -> dict:
    """One service account (a row of ``stats.get_owned_apps_summary``) as the
    account endpoints give it: never a key. ``GET /dashboard/api/service-accounts`` adds to it
    what only a signed-in owner may see."""
    return {
        "username": account["username"],
        # What an admin wrote about it; absent when there is none.
        **({"description": account["description"]} if account.get("description") else {}),
        # The department it is for (its owners must be in it); absent when it has none.
        **({"department": account["department"]} if account.get("department") else {}),
        "daily_limit_usd": max(0.0, float(account["daily_limit_usd"])),
        "legacy_key": account["legacy_key"],
        "month": {
            "requests": account["monthly_reqs"],
            "input_tokens": account["monthly_input"],
            "output_tokens": account["monthly_output"],
            "cost_usd": _usd(account["monthly_cost"]),
            "models": [_model_usage_row(r) for r in account["models"]],
        },
        # A service account that sends requests for its users
        # (X-On-Behalf-Of): what it sent for them this month. Billed to
        # those users, so no part of "month" above.
        "acts_for_users": account["acts_for_users"],
        "on_behalf": {
            "users": account["on_behalf"]["users"],
            "requests": account["on_behalf"]["requests"],
            "input_tokens": account["on_behalf"]["input_tokens"],
            "output_tokens": account["on_behalf"]["output_tokens"],
            "cost_usd": _usd(account["on_behalf"]["cost"]),
        },
    }


def service_accounts(user: User) -> dict:
    """The service accounts ``user`` owns, by name in natural order, with
    this month's usage: ``GET /v1/service-accounts``. Never a key
    (``deps.get_owned_apps`` takes them out)."""
    accounts = sorted(get_owned_apps(user), key=lambda account: natural_key(account["username"]))
    return {
        "object": "service_accounts",
        "month": local_month_start().strftime("%Y-%m"),
        "service_accounts": [service_account_row(account) for account in accounts],
    }


def _patch(env: dict[str, str | None]) -> dict:
    """A settings patch as data: the variables to set in the ``env`` block
    of Claude Code's settings.json and the ones to take out of it."""
    return {
        "set": {name: value for name, value in env.items() if value is not None},
        "unset": [name for name, value in env.items() if value is None],
    }


def notices(user: User) -> dict:
    """The account's open client-rule notices with their patches, and
    beside them the model changes shown to it: ``GET /v1/notices``."""
    found = get_client_notices(user)
    notices = []
    for notice in found["notices"]:
        entry: dict[str, object] = {"id": notice["rule_id"], "title": notice["title"]}
        if notice["message"]:
            entry["message"] = notice["message"]
        entry["last_seen_at"] = (
            notice["seen_at"].replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).isoformat(timespec="seconds")
        )
        if notice["client_version"]:
            entry["client_version"] = notice["client_version"]
        if notice["env"]:
            entry["patch"] = _patch(notice["env"])
        notices.append(entry)
    body: dict[str, object] = {
        "object": "notices",
        "window_days": client_rules.WINDOW_DAYS,
        "notices": notices,
    }
    combined = found["combined"]
    if combined:
        body["patch"] = {**_patch(combined["env"]), "covers": combined["covered"], "manual": combined["manual"]}
    body["model_changes"] = model_changes_block(user)
    return body


def model_changes_block(user: User) -> dict:
    """The models being replaced for ``user``, as data: what
    ``GET /v1/notices`` carries under ``model_changes``. (It used to be an
    endpoint of its own too, ``GET /v1/model-changes``; that one is gone.)"""
    renames: dict[str, str] = {}
    changes = []
    for change in get_model_changes(user):
        entry: dict[str, object] = {"id": change["ref"], "title": change["title"]}
        if change["message"]:
            entry["message"] = change["message"]
        entry["models"] = change["renames"]
        entry["applied"] = change["applied"]
        changes.append(entry)
        renames.update(change["renames"])
    return {"changes": changes, "retiring": list(renames), "patch": {"models": renames}}
