"""
What the dashboard shows an account, as data: the one place it is put
together.

The dashboard's routes (``app/routers/dashboard``) only authenticate and
answer; what they answer with is built here, from the same readers the
account endpoints use (``stats``, ``model_catalog``, ``client_rules``,
``model_changes``). The Jinja page (``page_context``) and the JSON API a
frontend of its own reads are two views of it, so they cannot disagree.

Nothing in this module knows a request, a template or a response: a
function takes a database session and the signed-in ``User`` and returns
plain dicts and lists.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session

from app.core.config import (
    get_azure_models_snapshot,
    get_bedrock_models_snapshot,
    get_display_order_snapshot,
    get_model_routing_snapshot,
    get_pricing_snapshot,
)
from app.core.deps import effective_cloud_limit
from app.core.logger import logger
from app.core.server_state import get_metrics, is_alive
from app.core.timeutil import LOCAL_TZ, local_month_start, seconds_until_local_midnight
from app.models.schema import (
    LEGACY_SERVICE_PREFIX,
    User,
    has_legacy_service_name,
    is_legacy_api_key,
)
from app.services import client_patch, client_rules, model_changes
from app.services.model_catalog import (
    catalog_keys,
    fallback_target,
    resolve_prices,
    usage_row_key,
    visible_usage_rows,
)
from app.services.model_order import (
    catalog_key,
    natural_key,
    positions,
    type_rank,
)
from app.services.stats import (
    FLOW_TOP_USERS,
    get_daily_trends,
    get_model_breakdown,
    get_on_behalf_users,
    get_owned_apps_summary,
    get_withheld_apps,
    get_user_daily_summary,
    get_usage_sources,
    get_user_on_behalf_today,
    get_user_monthly_summary,
    summarize_server_status,
)

# The Windows installer template the Setup page personalizes (``setup/``).
SETUP_DIR = Path(__file__).resolve().parent.parent.parent / "setup"


def installer_available() -> bool:
    """Whether this site ships the Claude Code installer template."""
    return (SETUP_DIR / "install-claude-code.bat").is_file()


def usage_percent(spent: float, limit: float) -> float:
    """Share of ``limit`` spent, capped at 100; a $0 limit is 100 % used."""
    if limit <= 0:
        return 100.0
    return min(100.0, (spent / limit) * 100)


def budget_state(limit: float, spent: float) -> str:
    """Colour band for the dashboard's budget meter.

    ``ok`` (< 70 %) · ``warn`` (< 90 %) · ``crit`` (< 100 %) · ``over``
    (limit reached: requests now get 429). Every account has a limit; a $0
    one is ``over`` from the start.
    """
    if limit <= 0:
        return "over"
    share = spent / limit
    if share >= 1:
        return "over"
    if share >= 0.9:
        return "crit"
    if share >= 0.7:
        return "warn"
    return "ok"


def format_duration(seconds: float) -> str:
    """``4h 07m`` / ``12m`` — how long until the daily budget resets."""
    minutes = max(int(seconds // 60), 0)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _catalog_sort_key(m: dict):
    """A model-table row's place within its type (see ``model_order``)."""
    return catalog_key(
        m.get("name") or m.get("alias") or "", m.get("position"),
        m["input_price"] + m["output_price"],
    )


def _by_type(groups: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """``groups`` with its type keys in display order (``TYPE_ORDER`` first)."""
    return {t: groups[t] for t in sorted(groups, key=type_rank)}


def _catalog_keys() -> dict[tuple[str, str], tuple]:
    """``(backend, alias) -> catalog_key`` for every configured model, hidden
    ones included (their past usage still has to sort sensibly)."""
    return catalog_keys(
        {
            "vllm": get_model_routing_snapshot(),
            "azure": get_azure_models_snapshot(),
            "bedrock": get_bedrock_models_snapshot(),
        },
        get_pricing_snapshot(),
        _display_positions(),
    )


def _display_positions() -> dict[str, dict[str, int]]:
    """``{backend: {alias: index}}`` from ``[display_order]``."""
    return {backend: positions(aliases) for backend, aliases in get_display_order_snapshot().items()}


def model_breakdown(
    session: Session, user: User, period: str = "month",
) -> tuple[list[dict], float]:
    """Per-model cost breakdown grouped per backend, plus the period total.

    Group order is fixed (on-prem first) with unknown backend values appended
    so no spend can vanish from the breakdown — except the cloud backends the
    user has no access to (``model_catalog.visible_usage_rows``), which are
    left out of the rows and the total alike; peak-overflow rows stay, marked
    ``overflow``. Each row gets its share of that total.
    Shared by the dashboard page and ``GET /dashboard/api/model-breakdown``
    so both always agree.
    """
    breakdown_rows = visible_usage_rows(get_model_breakdown(session, user.id, period=period), user)
    total_cost = sum(r["cost_usd"] for r in breakdown_rows)
    backend_order = ["vllm", "azure", "bedrock"]
    extra_backends = sorted({
        r["backend"] for r in breakdown_rows if r["backend"] not in backend_order
    })
    # Rows follow the model tables' order (type, then the catalog order),
    # not spend, so a model sits in the same place in both places. The
    # doughnut sorts its own copy by cost.
    catalog = _catalog_keys()

    def _row_key(r: dict) -> tuple:
        return usage_row_key(r, catalog)

    groups = []
    for backend_key in backend_order + extra_backends:
        rows = sorted((r for r in breakdown_rows if r["backend"] == backend_key), key=_row_key)
        if not rows:
            continue
        for r in rows:
            r["share"] = round(
                (r["cost_usd"] / total_cost) * 100 if total_cost > 0 else 0.0, 1
            )
        groups.append({
            "backend": backend_key,
            "label": BACKEND_LABELS.get(backend_key, backend_key),
            "cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
            "rows": rows,
        })
    return groups, round(total_cost, 6)


BACKEND_LABELS = {"vllm": "On-Prem", "azure": "Azure", "bedrock": "AWS Bedrock"}


def usage_flow(session: Session, user: User) -> dict:
    """The dashboard's *Usage Flow* card: this month's usage by the way it
    came in (the account's own key, or an application that sent requests for
    it) and the models each went to. ``stats.get_usage_sources``, the data
    ``GET /v1/usage`` gives as ``month.sources``, with what the page adds:
    a label, each source's share of the month's cost and the backends'
    display names. The same rows as Cost by Model, so the two cards add up
    to the same total."""
    sources = get_usage_sources(session, user, period="month")
    total = sum(s["cost_usd"] for s in sources)
    for source in sources:
        source["label"] = source.get("service_account") or "Your API key"
        source["share"] = round(source["cost_usd"] / total * 100 if total > 0 else 0.0, 1)
        for row in source["models"]:
            row["backend_label"] = BACKEND_LABELS.get(row["backend"], row["backend"])
    return {"sources": sources, "total_cost_usd": round(total, 6)}


def _service_flow(flow: dict | None) -> dict | None:
    """A service account's month by the users it sent requests for
    (``stats.get_on_behalf_users``), as its dashboard card shows it:
    ``rows`` for the drawing (one per user and backend) and ``people``,
    the same per user for the table under it, with ``share`` of the cost.
    None when it sent nothing for anyone."""
    if not flow:
        return None
    people: dict[str, dict] = {}
    for row in flow["rows"]:
        row["backend_label"] = BACKEND_LABELS.get(row["backend"], row["backend"])
        person = people.setdefault(row["user"], {
            "user": row["user"], "display_name": row["display_name"], "others": row.get("others", 0),
            "requests": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "backends": [],
        })
        for key in ("requests", "input_tokens", "output_tokens"):
            person[key] += row[key]
        person["cost_usd"] = round(person["cost_usd"] + row["cost_usd"], 6)
        person["backends"].append(row["backend"])
    total = sum(p["cost_usd"] for p in people.values())
    for person in people.values():
        person["share"] = round(person["cost_usd"] / total * 100 if total > 0 else 0.0, 1)
        person["backends"].sort(key=lambda b: _BACKEND_ORDER.index(b) if b in _BACKEND_ORDER else len(_BACKEND_ORDER))
    return {"rows": flow["rows"], "people": list(people.values()), "users": flow["users"]}


_BACKEND_ORDER = ("vllm", "azure", "bedrock")


def owned_service_accounts(session: Session, user: User) -> list[dict]:
    """The dashboard's *Service accounts* tab: the accounts ``user`` owns
    (``stats.get_owned_apps_summary``, the data ``GET /v1/service-accounts`` serves), by
    name in natural order, each model row with its share of that account's
    month. Every row is shown, whatever cloud access the viewer has: it is
    the service account's bill, not the viewer's."""
    accounts = sorted(get_owned_apps_summary(session, user.id), key=lambda a: natural_key(a["username"]))
    # Who each sent requests for this month, and on which backend: the card's flow chart.
    flows = get_on_behalf_users(session, [a["id"] for a in accounts if a["on_behalf"]["requests"]])
    for account in accounts:
        account["flow"] = _service_flow(flows.get(account["id"]))
        name = account["username"]
        # Older accounts are named app_<name>: the letters after the prefix say more.
        account["initials"] = (name[len(LEGACY_SERVICE_PREFIX):] if has_legacy_service_name(name) else name)[:2].upper() or "?"
        total = account["monthly_cost"]
        for row in account["models"]:
            row["backend_label"] = BACKEND_LABELS.get(row["backend"], row["backend"])
            row["share"] = round(row["cost_usd"] / total * 100 if total > 0 else 0.0, 1)
    return accounts


def client_notices(session: Session, user: User) -> list[dict]:
    """The client-rule notices for this account (never takes the page down)."""
    try:
        notices = client_rules.notices_for(session, user)
    except Exception as exc:  # e.g. before `alembic upgrade`
        session.rollback()
        logger.warning("Client notices failed | user={} error={}: {}", user.username, type(exc).__name__, exc)
        return []
    for n in notices:
        n["seen_local"] = n["seen_at"].replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    return notices


def client_combined(session: Session, user: User) -> dict | None:
    """The account's one patch for the notices panel (never takes the page down)."""
    try:
        return client_rules.combined_for(session, user)
    except Exception as exc:
        session.rollback()
        logger.warning("Client combined patch failed | user={} error={}: {}", user.username, type(exc).__name__, exc)
        return None


def shown_model_changes(session: Session, user: User) -> list[dict]:
    """The model changes shown to this account (never takes the page down)."""
    try:
        changes = model_changes.for_account(session, user)
    except Exception as exc:  # e.g. before `alembic upgrade`
        session.rollback()
        logger.warning("Model changes failed | user={} error={}: {}", user.username, type(exc).__name__, exc)
        return []
    return changes


def welcome_models() -> tuple[dict[str, list[dict]], str]:
    """The welcome page's model list: the on-prem models that are shown
    (not ``hidden``), by type in catalog order, each with its capability
    flags and billed prices; and the alias its examples use (the first
    chat model, ``""`` when there is none)."""
    pricing_map = get_pricing_snapshot()
    order = _display_positions()["vllm"]
    models_by_type: dict[str, list[dict]] = {}
    for alias, route in get_model_routing_snapshot().items():
        if route.get("hidden"):
            continue
        model_type = route["type"]
        in_price, out_price = resolve_prices(route, model_type, pricing_map)
        models_by_type.setdefault(model_type, []).append({
            "alias": alias,
            "display_name": route.get("display_name", ""),
            "context_window": route.get("context_window"),
            "max_output_tokens": route.get("max_output_tokens"),
            "supports_tools": route.get("supports_tools", False),
            "supports_vision": route.get("supports_vision", False),
            "supports_prompt_caching": route.get("supports_prompt_caching", False),
            "is_reasoning": route.get("is_reasoning", False),
            "position": order.get(alias),
            "input_price": in_price,
            "output_price": out_price,
        })
    for models in models_by_type.values():
        models.sort(key=_catalog_sort_key)
    models_by_type = _by_type(models_by_type)
    first_model = next(
        (models[0]["alias"] for t in ("llm", "vlm") if (models := models_by_type.get(t))), "",
    )
    return models_by_type, first_model


def _onprem_groups(pricing_map: dict, order: dict[str, dict[str, int]]) -> dict[str, list[dict]]:
    """The dashboard's on-prem model table, keyed by raw type (hidden models
    skipped). Each entry carries the optional metadata for the capability
    badges next to the online / down indicator, the effective billing
    prices, and the load its server reports."""
    server_groups: dict[str, list[dict]] = {}
    for model_name, route in get_model_routing_snapshot().items():
        if route.get("hidden"):
            continue
        base_url = route["base_url"]
        model_type = route["type"]
        metrics = get_metrics(base_url)
        in_price, out_price = resolve_prices(route, model_type, pricing_map)
        server_groups.setdefault(model_type, []).append(
            {
                "name": model_name,
                "base_url": base_url,
                "alive": is_alive(base_url),
                "falls_back_to": None if is_alive(base_url) else fallback_target(model_name),
                "display_name": route.get("display_name", ""),
                "context_window": route.get("context_window"),
                "max_output_tokens": route.get("max_output_tokens"),
                "supports_tools": route.get("supports_tools", False),
                "supports_vision": route.get("supports_vision", False),
                "supports_prompt_caching": route.get("supports_prompt_caching", False),
                "is_reasoning": route.get("is_reasoning", False),
                "position": order["vllm"].get(model_name),
                "input_price": in_price,
                "output_price": out_price,
                # Load snapshot from /metrics — None when unavailable.
                "running": metrics.get("running") if metrics else None,
                "waiting": metrics.get("waiting") if metrics else None,
            }
        )
    # Catalog order, except that servers that are down go last in their
    # type: the ones a user can pick come first.
    for servers in server_groups.values():
        servers.sort(key=lambda s: (not s["alive"], _catalog_sort_key(s)))
    return _by_type(server_groups)


def _cloud_groups(
    snapshot: dict, backend: str, pricing_map: dict, order: dict[str, dict[str, int]],
) -> dict[str, list[dict]]:
    """A cloud backend's model table, grouped by model type in catalog order
    (same shape as the on-prem groups so the page renders all backends
    alike). Hidden entries are skipped so admins can stage models without
    showing them yet (mirrors the vLLM ``hidden`` flag)."""
    groups: dict[str, list[dict]] = {}
    for alias, entry in snapshot.items():
        if entry.get("hidden"):
            continue
        model_type = entry.get("type", "llm")
        in_price, out_price = resolve_prices(entry, model_type, pricing_map)
        groups.setdefault(model_type, []).append({
            "alias": alias,
            "type": model_type,
            "display_name": entry.get("display_name", ""),
            "context_window": entry.get("context_window"),
            "supports_tools": entry.get("supports_tools", False),
            "supports_vision": entry.get("supports_vision", False),
            "supports_prompt_caching": entry.get("supports_prompt_caching", False),
            "is_reasoning": entry.get("is_reasoning", False),
            "position": order[backend].get(alias),
            "input_price": in_price,
            "output_price": out_price,
        })
    for models in groups.values():
        models.sort(key=_catalog_sort_key)
    return _by_type(groups)


def page_context(session: Session, user: User) -> dict:
    """Everything the dashboard page shows ``user``, as the template's
    variables: the budget hero, the model tables, the usage cards, the
    owned service accounts, and the notices and model changes."""
    summary = get_user_monthly_summary(session, user.id)
    owned_apps = owned_service_accounts(session, user)

    # Monthly cost split by model alias, grouped per backend so on-prem and
    # cloud spend read separately (same data the refresh API serves).
    model_breakdown_groups, _ = model_breakdown(session, user)

    pricing_map = get_pricing_snapshot()
    order = _display_positions()
    server_groups = _onprem_groups(pricing_map, order)
    system_status = summarize_server_status(
        [s for servers in server_groups.values() for s in servers]
    )

    # Today's totals + budget percentage based on today's cost vs daily limit
    today = get_user_daily_summary(session, user.id)
    today_cost = today["total_cost_usd"]
    azure_today_cost = today["azure_cost_usd"]
    bedrock_today_cost = today["bedrock_cost_usd"]

    # Cloud cap (the user's own, else the gateway-wide default): Azure +
    # Bedrock spend together. None = no cap, row hidden.
    cloud_limit = effective_cloud_limit(user)
    cloud_today_cost = azure_today_cost + bedrock_today_cost
    cloud_usage_percent = (
        usage_percent(cloud_today_cost, cloud_limit) if cloud_limit is not None else 0.0
    )

    # Cloud model tables: only for a user who has been granted the backend
    # (an admin by the token's scope sees both).
    azure_access = bool(user.can_use_azure or user.is_admin)
    bedrock_access = bool(user.can_use_bedrock or user.is_admin)
    azure_groups = _cloud_groups(get_azure_models_snapshot(), "azure", pricing_map, order) if azure_access else {}
    bedrock_groups = _cloud_groups(get_bedrock_models_snapshot(), "bedrock", pricing_map, order) if bedrock_access else {}

    return dict(
        user=user,
        display_name=user.display_name or user.username,
        org_code=user.org_code,
        total_reqs=summary["total_requests"],
        total_cost=summary["total_cost_usd"],
        my_input=summary["total_input_tokens"],
        my_output=summary["total_output_tokens"],
        today_reqs=today["total_requests"],
        today_cost=round(today_cost, 4),
        today_input=today["total_input_tokens"],
        today_output=today["total_output_tokens"],
        usage_percent=round(usage_percent(today_cost, user.daily_limit_usd), 1),
        budget_state=budget_state(user.daily_limit_usd, today_cost),
        budget_remaining=round(max(user.daily_limit_usd - today_cost, 0.0), 4),
        budget_resets_in=format_duration(seconds_until_local_midnight()),
        system_status=system_status,
        azure_today_cost=round(azure_today_cost, 4),
        bedrock_today_cost=round(bedrock_today_cost, 4),
        vllm_today_cost=round(today["vllm_cost_usd"], 4),
        overflow_today_cost=round(today["overflow_cost_usd"], 4),
        on_behalf_today=get_user_on_behalf_today(session, user.id),
        usage_flow=usage_flow(session, user),
        cloud_limit=cloud_limit,
        cloud_today_cost=round(cloud_today_cost, 4),
        cloud_usage_percent=round(cloud_usage_percent, 1),
        trend_data=get_daily_trends(session, user.id),
        model_breakdown_groups=model_breakdown_groups,
        today_str=datetime.now(LOCAL_TZ).strftime("%Y-%m-%d"),
        owned_apps=owned_apps,
        withheld_apps=get_withheld_apps(session, user.id),
        flow_top_users=FLOW_TOP_USERS,
        service_month=local_month_start().strftime("%Y-%m"),
        legacy_key=is_legacy_api_key(user.api_key),
        legacy_app_keys=sum(1 for a in owned_apps if a.get("legacy_key")),
        client_notices=client_notices(session, user),
        client_combined=client_combined(session, user),
        model_changes=shown_model_changes(session, user),
        client_notice_days=client_rules.WINDOW_DAYS,
        client_quiet_days=client_rules.RESOLVED_QUIET_DAYS,
        api_key_env=client_patch.API_KEY_ENV,
        api_key_env_retired=client_patch.API_KEY_ENV_RETIRED,
        server_groups=server_groups,
        claude_code_available=installer_available(),
        azure_access=azure_access,
        azure_groups=azure_groups,
        bedrock_access=bedrock_access,
        bedrock_groups=bedrock_groups,
    )
