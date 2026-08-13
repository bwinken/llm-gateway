"""
Web dashboard endpoints (Jinja2 HTML pages).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.models.schema import AppOwner, User

from app.core.auth import get_web_user
from app.core.config import (
    APP_TITLE,
    get_azure_models_snapshot,
    get_bedrock_models_snapshot,
    get_model_routing_snapshot,
    get_pricing_snapshot,
    get_site_links,
)
from app.core.database import get_session
from app.core.server_state import get_metrics, is_alive
from app.core.timeutil import LOCAL_TZ
from app.services.stats import (
    get_daily_trends,
    get_model_breakdown,
    get_owned_apps_summary,
    get_user_daily_summary,
    get_user_monthly_summary,
)

router = APIRouter()
_templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))
# base.html reads the admin-editable links (support bot / install guide) on
# every page, so expose them as a template global instead of threading them
# through every render context.
templates.env.globals["get_site_links"] = get_site_links
_SETUP_DIR = Path(__file__).resolve().parent.parent.parent / "setup"
_SETUP_ALLOWED = {
    "llm-gateway-ca.crt",
    "install-cert.bat",
}


def _resolve_prices(entry: dict, model_type: str, pricing_map: dict) -> tuple[float, float]:
    """(input, output) USD per 1M tokens for a model entry.

    Same priority as billing's ``_calc_cost``: per-model override (both
    ``input_price_per_1m`` and ``output_price_per_1m`` present) → per-type
    ``[pricing.<type>]`` → ``_default`` — so what the dashboard shows is
    exactly what the request will be billed at.
    """
    if "input_price_per_1m" in entry and "output_price_per_1m" in entry:
        return float(entry["input_price_per_1m"]), float(entry["output_price_per_1m"])
    p = pricing_map.get(model_type) or pricing_map.get("_default") or {}
    return float(p.get("input_price_per_1m", 0.0)), float(p.get("output_price_per_1m", 0.0))


def _price_sort_key(m: dict):
    """Cheapest first; ties broken by alias for a stable listing."""
    return (m["input_price"] + m["output_price"], m.get("name") or m.get("alias") or "")


def _build_model_breakdown(
    session: Session, user_id: int, period: str = "month",
) -> tuple[list[dict], float]:
    """Per-model cost breakdown grouped per backend, plus the period total.

    Group order is fixed (on-prem first) with unknown backend values appended
    so no spend can vanish from the breakdown. Each row gets its share of the
    period total. Shared by the dashboard page render and the refresh API so
    both always agree.
    """
    breakdown_rows = get_model_breakdown(session, user_id, period=period)
    total_cost = sum(r["cost_usd"] for r in breakdown_rows)
    backend_labels = {"vllm": "On-Prem", "azure": "Azure", "bedrock": "AWS Bedrock"}
    backend_order = ["vllm", "azure", "bedrock"]
    extra_backends = sorted({
        r["backend"] for r in breakdown_rows if r["backend"] not in backend_order
    })
    groups = []
    for backend_key in backend_order + extra_backends:
        rows = [r for r in breakdown_rows if r["backend"] == backend_key]
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
async def index(
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
    models_by_type: dict[str, list[dict]] = {}
    first_model = ""
    for alias, route in get_model_routing_snapshot().items():
        if route.get("hidden"):
            continue
        model_type = route["type"]
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
        })
        if not first_model and model_type in ("llm", "vlm"):
            first_model = alias

    return templates.TemplateResponse(
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
async def dashboard(
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
    model_breakdown_groups, _ = _build_model_breakdown(session, user.id)

    # Build grouped server status keyed by raw type (skip hidden models).
    # Each entry carries optional metadata for capability badges next to the
    # online/down indicator, plus the effective billing prices so the list
    # can sort by (and display) cost.
    pricing_map = get_pricing_snapshot()
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
                "input_price": in_price,
                "output_price": out_price,
                # Load snapshot from /metrics — None when unavailable.
                "running": metrics.get("running") if metrics else None,
                "waiting": metrics.get("waiting") if metrics else None,
            }
        )
    for servers in server_groups.values():
        servers.sort(key=_price_sort_key)

    # Today's totals + budget percentage based on today's cost vs daily limit
    today = get_user_daily_summary(session, user.id)
    today_cost = today["total_cost_usd"]
    daily_limit = user.daily_limit_usd if user.daily_limit_usd > 0 else 1.0
    usage_percent = min(100.0, (today_cost / daily_limit) * 100)

    # Azure sub-budget: shown only when an azure_daily_limit_usd is set.
    azure_today_cost = today["azure_cost_usd"]
    azure_limit = user.azure_daily_limit_usd or 0.0
    azure_usage_percent = (
        min(100.0, (azure_today_cost / azure_limit) * 100) if azure_limit > 0 else 0.0
    )

    # Bedrock sub-budget — same contract as the Azure one above.
    bedrock_today_cost = today["bedrock_cost_usd"]
    bedrock_limit = user.bedrock_daily_limit_usd or 0.0
    bedrock_usage_percent = (
        min(100.0, (bedrock_today_cost / bedrock_limit) * 100) if bedrock_limit > 0 else 0.0
    )

    claude_code_available = (_SETUP_DIR / "install-claude-code.bat").is_file()

    # Azure access — list configured Azure model aliases when the user has
    # been granted access, grouped by model type and sorted by price (same
    # shape as server_groups so the template renders all backends uniformly).
    # Hidden Azure entries are skipped so admins can stage models without
    # exposing them yet (mirrors the vLLM `hidden` flag).
    def _cloud_groups(snapshot: dict) -> dict[str, list[dict]]:
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
                "input_price": in_price,
                "output_price": out_price,
            })
        for models in groups.values():
            models.sort(key=_price_sort_key)
        return groups

    azure_groups: dict[str, list[dict]] = {}
    if user.can_use_azure or user.is_admin:
        azure_groups = _cloud_groups(get_azure_models_snapshot())

    # Bedrock access — same contract as the Azure section above.
    bedrock_groups: dict[str, list[dict]] = {}
    if user.can_use_bedrock or user.is_admin:
        bedrock_groups = _cloud_groups(get_bedrock_models_snapshot())

    return templates.TemplateResponse(
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
            azure_today_cost=round(azure_today_cost, 4),
            bedrock_today_cost=round(bedrock_today_cost, 4),
            vllm_today_cost=round(today["vllm_cost_usd"], 4),
            azure_limit=azure_limit,
            azure_usage_percent=round(azure_usage_percent, 1),
            bedrock_limit=bedrock_limit,
            bedrock_usage_percent=round(bedrock_usage_percent, 1),
            trend_data=trend_data,
            model_breakdown_groups=model_breakdown_groups,
            today_str=datetime.now(LOCAL_TZ).strftime("%Y-%m-%d"),
            owned_apps=owned_apps,
            server_groups=server_groups,
            claude_code_available=claude_code_available,
            azure_access=user.can_use_azure or user.is_admin,
            azure_groups=azure_groups,
            bedrock_access=user.can_use_bedrock or user.is_admin,
            bedrock_groups=bedrock_groups,
        ),
    )


@router.get("/dashboard/api/model-breakdown")
async def model_breakdown_api(
    period: str = "month",
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """Fresh per-model cost breakdown for the dashboard's Cost by Model
    card — backs both the Month/Today toggle and the refresh button, so the
    card updates in place without a full page reload."""
    if period not in ("month", "day"):
        raise HTTPException(status_code=400, detail="period must be 'month' or 'day'.")
    groups, total_cost = _build_model_breakdown(session, user.id, period=period)
    return JSONResponse({
        "period": period,
        "groups": groups,
        "total_cost_usd": total_cost,
    })


@router.post("/dashboard/refresh-key")
async def refresh_own_key(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):

    # Re-fetch from DB for write operation (user was expunged in get_web_user)
    db_user = session.get(User, user.id)
    from app.models.schema import _generate_api_key
    db_user.api_key = _generate_api_key()
    session.add(db_user)
    session.commit()
    session.refresh(db_user)
    return JSONResponse({"ok": True, "api_key": db_user.api_key})


@router.post("/dashboard/app/{app_id}/refresh-key")
async def refresh_owned_app_key(
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

    from app.models.schema import _generate_api_key
    target.api_key = _generate_api_key()
    session.add(target)
    session.commit()
    session.refresh(target)
    return JSONResponse({"ok": True, "api_key": target.api_key})


@router.get("/dashboard/install-claude-code.bat")
async def personalized_claude_installer(
    user: User = Security(get_web_user, scopes=["read"]),
):
    """Serve install-claude-code.bat with the user's API key inlined.

    Auth'd so the key is never exposed publicly; oauth2-proxy redirects
    unauthenticated users through SSO first.
    """
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
async def setup_page(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
):
    """Setup page with CA cert + Claude Code tabs. Requires login."""
    available = {name: (_SETUP_DIR / name).is_file() for name in _SETUP_ALLOWED}
    claude_code_available = (_SETUP_DIR / "install-claude-code.bat").is_file()
    return templates.TemplateResponse(
        "setup.html",
        _common_ctx(
            request,
            title="Setup",
            available=available,
            claude_code_available=claude_code_available,
            user=user,
            display_name=user.display_name or user.username,
            org_code=user.org_code,
        ),
    )


@router.get("/setup/files/{filename}")
async def setup_file(
    filename: str,
    user: User = Security(get_web_user, scopes=["read"]),
):
    """Serve a file from the setup/ directory. Requires login; whitelist-only."""
    if filename not in _SETUP_ALLOWED:
        raise HTTPException(status_code=404, detail="Not found.")
    filepath = _SETUP_DIR / filename
    if not filepath.is_file():
        raise HTTPException(status_code=404, detail="File not available.")
    return FileResponse(
        filepath,
        filename=filename,
        media_type="application/octet-stream",
    )
