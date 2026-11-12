"""
Web dashboard endpoints (Jinja2 HTML pages).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.models.schema import AppOwner, User, is_legacy_api_key

from app.core.auth import get_web_user
from app.core.deps import effective_cloud_limit
from app.core.config import (
    APP_TITLE,
    get_azure_models_snapshot,
    get_bedrock_models_snapshot,
    get_display_order_snapshot,
    get_model_routing_snapshot,
    get_pricing_snapshot,
    get_site_links,
    setup_page_enabled,
)
from app.core.database import get_session
from app.core.logger import logger
from app.core.server_state import get_metrics, is_alive
from app.core.timeutil import LOCAL_TZ, seconds_until_local_midnight
from app.services import client_rules
from app.services.model_catalog import (
    catalog_keys,
    hidden_cloud_backends as _hidden_cloud_backends,
    resolve_prices as _resolve_prices,
    usage_row_key,
)
from app.services.model_order import (
    catalog_key,
    positions,
    type_rank,
)
from app.services.stats import (
    get_daily_trends,
    get_model_breakdown,
    get_owned_apps_summary,
    get_user_daily_summary,
    get_user_monthly_summary,
    summarize_server_status,
)

router = APIRouter()
_templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))
# base.html reads the admin-editable links (support bot / install guide) on
# every page, so expose them as a template global instead of threading them
# through every render context.
templates.env.globals["get_site_links"] = get_site_links
# Same for the Setup page switch (SETUP_PAGE_ENABLED): the navbar link is on
# every page.
templates.env.globals["setup_page_enabled"] = setup_page_enabled
_SETUP_DIR = Path(__file__).resolve().parent.parent.parent / "setup"


def _usage_percent(spent: float, limit: float) -> float:
    """Share of ``limit`` spent, capped at 100; a $0 limit is 100 % used."""
    if limit <= 0:
        return 100.0
    return min(100.0, (spent / limit) * 100)


def _budget_state(limit: float, spent: float) -> str:
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


def _format_duration(seconds: float) -> str:
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


def _build_model_breakdown(
    session: Session, user: User, period: str = "month",
) -> tuple[list[dict], float]:
    """Per-model cost breakdown grouped per backend, plus the period total.

    Group order is fixed (on-prem first) with unknown backend values appended
    so no spend can vanish from the breakdown — except the cloud backends the
    user has no access to (``_hidden_cloud_backends``), which are left out of
    the rows and the total alike. Each row gets its share of that total.
    Shared by the dashboard page render and the refresh API so both always
    agree.
    """
    hidden = _hidden_cloud_backends(user)
    breakdown_rows = [
        r for r in get_model_breakdown(session, user.id, period=period)
        if r["backend"] not in hidden
    ]
    total_cost = sum(r["cost_usd"] for r in breakdown_rows)
    backend_labels = {"vllm": "On-Prem", "azure": "Azure", "bedrock": "AWS Bedrock"}
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
            "label": backend_labels.get(backend_key, backend_key),
            "cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
            "rows": rows,
        })
    return groups, round(total_cost, 6)


def _common_ctx(request: Request, **extra) -> dict:
    """Base context shared by all pages."""
    # Build gateway base URL from Host header (reliable behind reverse proxy)
    host = request.headers.get("host", request.url.hostname or "localhost")
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme or "http")
    gateway_base = f"{scheme}://{host}/v1"
    return {
        "request": request,
        "app_title": APP_TITLE,
        "current_year": datetime.now(timezone.utc).year,
        "gateway_base": gateway_base,
        **extra,
    }


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """Getting-started welcome page with API guide and available models."""

    display_name = user.display_name or user.username
    org_code = user.org_code

    # Group models by type for display (skip hidden models). Each entry
    # carries optional admin-set metadata so the welcome page can show
    # capability badges (tools / vision / cache) and context window.
    pricing_map = get_pricing_snapshot()
    order = _display_positions()["vllm"]
    models_by_type: dict[str, list[dict]] = {}
    for alias, route in get_model_routing_snapshot().items():
        if route.get("hidden"):
            continue
        model_type = route["type"]
        in_price, out_price = _resolve_prices(route, model_type, pricing_map)
        if model_type not in models_by_type:
            models_by_type[model_type] = []
        models_by_type[model_type].append({
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

    return templates.TemplateResponse(
        request,
        "welcome.html",
        _common_ctx(
            request,
            title="Welcome",
            user=user,
            display_name=display_name,
            org_code=org_code,
            models_by_type=models_by_type,
            first_model=first_model or "your-model",
        ),
    )


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):

    display_name = user.display_name or user.username
    org_code = user.org_code

    summary = get_user_monthly_summary(session, user.id)
    trend_data = get_daily_trends(session, user.id)
    owned_apps = get_owned_apps_summary(session, user.id)

    # Monthly cost split by model alias, grouped per backend so on-prem and
    # cloud spend read separately (same data the refresh API serves).
    model_breakdown_groups, _ = _build_model_breakdown(session, user)

    # Build grouped server status keyed by raw type (skip hidden models).
    # Each entry carries optional metadata for capability badges next to the
    # online/down indicator, plus the effective billing prices so the list
    # can sort by (and display) cost.
    pricing_map = get_pricing_snapshot()
    order = _display_positions()
    server_groups: dict[str, list[dict]] = {}
    for model_name, route in get_model_routing_snapshot().items():
        if route.get("hidden"):
            continue
        base_url = route["base_url"]
        model_type = route["type"]
        if model_type not in server_groups:
            server_groups[model_type] = []
        metrics = get_metrics(base_url)
        in_price, out_price = _resolve_prices(route, model_type, pricing_map)
        server_groups[model_type].append(
            {
                "name": model_name,
                "base_url": base_url,
                "alive": is_alive(base_url),
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
    server_groups = _by_type(server_groups)

    system_status = summarize_server_status(
        [s for servers in server_groups.values() for s in servers]
    )

    # Today's totals + budget percentage based on today's cost vs daily limit
    today = get_user_daily_summary(session, user.id)
    today_cost = today["total_cost_usd"]
    usage_percent = _usage_percent(today_cost, user.daily_limit_usd)
    budget_state = _budget_state(user.daily_limit_usd, today_cost)

    azure_today_cost = today["azure_cost_usd"]
    bedrock_today_cost = today["bedrock_cost_usd"]

    # Cloud cap (the user's own, else the gateway-wide default): Azure +
    # Bedrock spend together. None = no cap, row hidden.
    cloud_limit = effective_cloud_limit(user)
    cloud_today_cost = azure_today_cost + bedrock_today_cost
    cloud_usage_percent = (
        _usage_percent(cloud_today_cost, cloud_limit) if cloud_limit is not None else 0.0
    )

    claude_code_available = (_SETUP_DIR / "install-claude-code.bat").is_file()

    # Azure access — list configured Azure model aliases when the user has
    # been granted access, grouped by model type in catalog order (same
    # shape as server_groups so the template renders all backends uniformly).
    # Hidden Azure entries are skipped so admins can stage models without
    # exposing them yet (mirrors the vLLM `hidden` flag).
    def _cloud_groups(snapshot: dict, backend: str) -> dict[str, list[dict]]:
        groups: dict[str, list[dict]] = {}
        for alias, entry in snapshot.items():
            if entry.get("hidden"):
                continue
            model_type = entry.get("type", "llm")
            in_price, out_price = _resolve_prices(entry, model_type, pricing_map)
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

    azure_groups: dict[str, list[dict]] = {}
    if user.can_use_azure or user.is_admin:
        azure_groups = _cloud_groups(get_azure_models_snapshot(), "azure")

    # Bedrock access — same contract as the Azure section above.
    bedrock_groups: dict[str, list[dict]] = {}
    if user.can_use_bedrock or user.is_admin:
        bedrock_groups = _cloud_groups(get_bedrock_models_snapshot(), "bedrock")

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _common_ctx(
            request,
            title="Dashboard",
            user=user,
            display_name=display_name,
            org_code=org_code,
            total_reqs=summary["total_requests"],
            total_cost=summary["total_cost_usd"],
            my_input=summary["total_input_tokens"],
            my_output=summary["total_output_tokens"],
            today_reqs=today["total_requests"],
            today_cost=round(today_cost, 4),
            today_input=today["total_input_tokens"],
            today_output=today["total_output_tokens"],
            usage_percent=round(usage_percent, 1),
            budget_state=budget_state,
            budget_remaining=round(max(user.daily_limit_usd - today_cost, 0.0), 4),
            budget_resets_in=_format_duration(seconds_until_local_midnight()),
            system_status=system_status,
            azure_today_cost=round(azure_today_cost, 4),
            bedrock_today_cost=round(bedrock_today_cost, 4),
            vllm_today_cost=round(today["vllm_cost_usd"], 4),
            cloud_limit=cloud_limit,
            cloud_today_cost=round(cloud_today_cost, 4),
            cloud_usage_percent=round(cloud_usage_percent, 1),
            trend_data=trend_data,
            model_breakdown_groups=model_breakdown_groups,
            today_str=datetime.now(LOCAL_TZ).strftime("%Y-%m-%d"),
            owned_apps=owned_apps,
            legacy_key=is_legacy_api_key(user.api_key),
            legacy_app_keys=sum(1 for a in owned_apps if a.get("legacy_key")),
            client_notices=_client_notices(session, user),
            client_combined=_client_combined(session, user),
            client_notice_days=client_rules.WINDOW_DAYS,
            api_key_env=client_rules.API_KEY_ENV,
            api_key_env_retired=client_rules.API_KEY_ENV_RETIRED,
            server_groups=server_groups,
            claude_code_available=claude_code_available,
            azure_access=user.can_use_azure or user.is_admin,
            azure_groups=azure_groups,
            bedrock_access=user.can_use_bedrock or user.is_admin,
            bedrock_groups=bedrock_groups,
        ),
    )


@router.get("/dashboard/api/model-breakdown")
def model_breakdown_api(
    period: str = "month",
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """Fresh per-model cost breakdown for the dashboard's Cost by Model
    card — backs both the Month/Today toggle and the refresh button, so the
    card updates in place without a full page reload."""
    if period not in ("month", "day"):
        raise HTTPException(status_code=400, detail="period must be 'month' or 'day'.")
    groups, total_cost = _build_model_breakdown(session, user, period=period)
    return JSONResponse({
        "period": period,
        "groups": groups,
        "total_cost_usd": total_cost,
    })


@router.post("/dashboard/refresh-key")
def refresh_own_key(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):

    # Re-fetch from DB for write operation (user was expunged in get_web_user)
    db_user = session.get(User, user.id)
    from app.models.schema import issue_api_key
    issue_api_key(db_user)
    session.add(db_user)
    session.commit()
    session.refresh(db_user)
    return JSONResponse({"ok": True, "api_key": db_user.api_key})


@router.post("/dashboard/app/{app_id}/refresh-key")
def refresh_owned_app_key(
    app_id: int,
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """Refresh API key for an app account owned by the current user."""

    target = session.exec(select(User).where(User.id == app_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="App account not found.")

    ownership = session.exec(
        select(AppOwner).where(AppOwner.app_id == app_id, AppOwner.owner_id == user.id)
    ).first()
    if not ownership:
        raise HTTPException(status_code=403, detail="You do not own this app account.")

    from app.models.schema import issue_api_key
    issue_api_key(target)
    session.add(target)
    session.commit()
    session.refresh(target)
    return JSONResponse({"ok": True, "api_key": target.api_key})


def _client_notices(session: Session, user: User) -> list[dict]:
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


def _client_combined(session: Session, user: User) -> dict | None:
    """The account's one patch for the notices panel (never takes the page down)."""
    try:
        return client_rules.combined_for(session, user)
    except Exception as exc:
        session.rollback()
        logger.warning("Client combined patch failed | user={} error={}: {}", user.username, type(exc).__name__, exc)
        return None


@router.get("/dashboard/client-notices", response_class=HTMLResponse)
def client_notices_panel(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """The dashboard's notices panel alone (empty when the account has
    nothing open): the page swaps it in after *I've applied it*, so the one
    patch and what it covers always match what is still open."""
    return templates.TemplateResponse(
        request,
        "_client_notices.html",
        {"client_notices": _client_notices(session, user), "client_combined": _client_combined(session, user)},
    )


def _script_response(content: str, filename: str, secret: bool = False) -> Response:
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if secret:
        headers["Cache-Control"] = "no-store"
    return Response(content=content.encode("utf-8"), media_type="application/octet-stream", headers=headers)


# "all" routes first: FastAPI matches in order, and no check id is "all".
@router.get("/dashboard/client-rules/all/patch.{fmt}")
def client_rules_patch_all(
    fmt: str,
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """The account's one patch: the patches of everything it has open
    merged (``client_rules.combined_for``; the variant for this account:
    local-only, Azure or AWS), as ``patch.bat`` (Windows) or ``patch.sh``
    (Linux / macOS). There is no per-check download: one notice or five, it
    is the same single script. Mail links here too, so a browser arriving
    with nothing left to apply is sent to the dashboard instead of a 404."""
    if fmt not in client_rules.PATCH_FORMATS:
        raise HTTPException(status_code=404, detail="No such patch format.")
    combined = client_rules.combined_for(session, user)
    if combined is None or not combined["env"]:
        if "text/html" in (request.headers.get("accept") or "").lower():
            return RedirectResponse(url="/dashboard", status_code=303)
        raise HTTPException(status_code=404, detail="No settings patch for this account.")
    filename = f"claude-code-patch.{fmt}"
    name = "fixes for " + ", ".join(combined["covered"])
    return _script_response(client_rules.render_patch(fmt, name, combined["env"], filename), filename)


@router.post("/dashboard/client-rules/all/resolved")
def client_rules_resolved_all(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """"I've applied everything": every open notice of the account goes
    away; each returns by itself if the gateway sees its problem again."""
    count = client_rules.resolve_all(session, user.username)
    logger.info("Client rules all marked as fixed by the account | user={} count={}", user.username, count)
    if "application/json" in (request.headers.get("accept") or "").lower():
        return JSONResponse({"ok": True, "resolved": count})
    return RedirectResponse(url="/dashboard", status_code=303)


@router.post("/dashboard/client-rules/{rule_id}/resolved")
def client_rule_resolved(
    rule_id: str,
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """The user says they applied the fix: their notice for this check goes
    away and they are no longer mailed about it. The gateway cannot see a
    settings file, so it takes their word; the next violating request
    reopens the notice by itself."""
    if not client_rules.resolve(session, rule_id, user.username):
        raise HTTPException(status_code=404, detail="Nothing to mark as done for this account.")
    logger.info("Client rule marked as fixed by the account | rule={} user={}", rule_id, user.username)
    if "application/json" in (request.headers.get("accept") or "").lower():
        return JSONResponse({"ok": True, "rule_id": rule_id})
    return RedirectResponse(url="/dashboard", status_code=303)


@router.get("/dashboard/api-key/patch.{fmt}")
def api_key_patch(
    fmt: str,
    user: User = Security(get_web_user, scopes=["read"]),
):
    """A script that puts the account's current API key into Claude Code
    (``client_rules.key_env``: ``ANTHROPIC_AUTH_TOKEN`` set in the ``env``
    block of settings.json, the older ``ANTHROPIC_API_KEY`` removed, nothing
    else), as ``patch.bat`` or ``patch.sh``: what a user runs on each
    machine after regenerating the key. Only ever the caller's own key; the
    download is in the audit log (``audit._AUDITED_READS``) and never cached."""
    if fmt not in client_rules.PATCH_FORMATS:
        raise HTTPException(status_code=404, detail="No such patch format.")
    filename = f"claude-code-key.{fmt}"
    script = client_rules.render_patch(
        fmt, f"your API key - {client_rules.API_KEY_ENV}", client_rules.key_env(user.api_key), filename,
    )
    logger.info("API key patch downloaded | user={} format={}", user.username, fmt)
    return _script_response(script, filename, secret=True)


@router.get("/dashboard/install-claude-code.bat")
def personalized_claude_installer(
    user: User = Security(get_web_user, scopes=["read"]),
):
    """Serve install-claude-code.bat with the user's API key inlined.

    Auth'd so the key is never exposed publicly; oauth2-proxy redirects
    unauthenticated users through SSO first. 404 with the Setup page off
    (``SETUP_PAGE_ENABLED=false``).
    """
    if not setup_page_enabled():
        raise HTTPException(status_code=404, detail="Not found.")
    template_path = _SETUP_DIR / "install-claude-code.bat"
    if not template_path.is_file():
        raise HTTPException(status_code=404, detail="Installer not available.")
    script = template_path.read_text(encoding="utf-8").replace(
        "__USER_API_KEY__", user.api_key
    )
    # cmd.exe is fragile with LF-only files (it can merge lines, swallow REM
    # blocks, and end up running comment text as commands). Normalize all
    # line endings to CRLF regardless of how the operator saved the template
    # on the (typically Linux) gateway host.
    script = script.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    return Response(
        content=script.encode("utf-8"),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": 'attachment; filename="install-claude-code.bat"',
        },
    )


# ── Setup page (requires SSO login) ──

@router.get("/setup", response_class=HTMLResponse)
def setup_page(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
):
    """Setup page for the Claude Code installer (Windows). Requires login.
    404 when a site turned it off (``SETUP_PAGE_ENABLED=false``)."""
    if not setup_page_enabled():
        raise HTTPException(status_code=404, detail="Not found.")
    claude_code_available = (_SETUP_DIR / "install-claude-code.bat").is_file()
    return templates.TemplateResponse(
        request,
        "setup.html",
        _common_ctx(
            request,
            title="Setup",
            claude_code_available=claude_code_available,
            user=user,
            display_name=user.display_name or user.username,
            org_code=user.org_code,
        ),
    )
