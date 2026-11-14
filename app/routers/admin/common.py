"""
What every part of the admin panel shares: the one ``/admin`` router (admin
scope required on every route), the template environment, and the small
helpers more than one tab needs.

The other modules of this package register their routes on ``router`` when
they are imported; ``app.routers.admin`` imports them all.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Security
from fastapi.templating import Jinja2Templates

from app.core.auth import get_web_user
from app.core.config import get_site_links, setup_page_enabled
from app.core.timeutil import LOCAL_TZ


router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Security(get_web_user, scopes=["admin"])],
)
_templates_dir = Path(__file__).resolve().parent.parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))
# base.html reads the admin-editable links (support bot / install guide) on
# every page — same global as web_ui's template environment.
templates.env.globals["get_site_links"] = get_site_links
templates.env.globals["setup_page_enabled"] = setup_page_enabled


# The per-backend (Azure / Bedrock) amounts are gone: cloud spend has one
# cap for both, cloud_daily_limit_usd. A script still sending them gets told
# rather than silently ignored.
_RETIRED_LIMIT_FIELDS: tuple[str, ...] = (
    "azure_daily_limit_usd", "bedrock_daily_limit_usd",
)


def _reject_retired_admin_flag(body: dict) -> None:
    """``is_admin`` is no longer a stored permission (see ``schema.User``).
    Refused rather than ignored, so a caller that still sends it finds out."""
    if "is_admin" in body:
        raise HTTPException(
            status_code=400,
            detail=(
                "is_admin no longer exists: who is an admin is decided by AuthCenter "
                "(the admin scope of the access token), not stored on the account."
            ),
        )


def _reject_retired_limits(body: dict) -> None:
    retired = [f for f in _RETIRED_LIMIT_FIELDS if f in body]
    if retired:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{', '.join(retired)} no longer exist: Azure and AWS share one cloud "
                "cap, cloud_daily_limit_usd (per user; blank follows the default "
                "in admin panel → Cloud Daily Limit)."
            ),
        )


def _limit_usd(raw: object, field: str) -> float:
    """A daily limit (or the cloud cap) in USD: a finite number >= 0.

    There is no "unlimited": 0 is a $0 budget (every request refused), and
    negative / inf / nan are rejected rather than quietly meaning "no cap".
    """
    if isinstance(raw, bool):
        raise HTTPException(status_code=400, detail=f"{field} must be a number.")
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} must be a number.")
    if not math.isfinite(value) or value < 0:
        raise HTTPException(status_code=400, detail=f"{field} must be a number >= 0.")
    return value


def _local_label(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def _wants_json(request: Request) -> bool:
    """True when the caller asked for a JSON reply (``Accept: application/json``).

    The admin page's in-place toggles send this so a click updates one
    button instead of reloading (and re-scrolling) the whole page; a plain
    form submit keeps the classic POST → 303 → ``/admin`` round-trip.
    """
    return "application/json" in (request.headers.get("accept") or "").lower()


def _strict_bool(value: object, field: str) -> bool:
    """A JSON boolean, nothing else — ``bool("false")`` is True, which would
    silently waive a user whose admin sent the string by mistake."""
    if not isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{field} must be true or false.")
    return value
