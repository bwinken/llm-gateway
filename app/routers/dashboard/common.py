"""
What the dashboard's modules share: the one router and two small helpers.

Every route is under ``/dashboard`` and signs the caller in by SSO
(``get_web_user``, scope ``read``), declared on the route so each handler
has its ``user``. Writes there are refused cross-site
(``browser_security.COOKIE_AUTH_PREFIXES``) and recorded in the audit log
(``AuditMiddleware``) because of that prefix: a dashboard route outside it
would have neither.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.core import api_docs
from fastapi.responses import Response

# 401 and 403 are every dashboard route's, so they are in /docs once, here.
# A route's docstring is its description there, written to the plan in
# ``app/core/api_docs.py``.
router = APIRouter(tags=["web"], responses=api_docs.DASHBOARD_ERRORS)


def wants_json(request: Request) -> bool:
    """True when the caller asked for a JSON reply (``Accept:
    application/json``): a script on the page, or a frontend of its own.
    A plain form submit gets the POST → 303 → ``/dashboard`` round trip."""
    return "application/json" in (request.headers.get("accept") or "").lower()


def wants_html(request: Request) -> bool:
    """True for a browser navigating (a link in a mail, a typed address)."""
    return "text/html" in (request.headers.get("accept") or "").lower()


def script_response(content: str, filename: str, secret: bool = False) -> Response:
    """A script as a download. ``secret``: it holds a key, never cache it."""
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if secret:
        headers["Cache-Control"] = "no-store"
    return Response(content=content.encode("utf-8"), media_type="application/octet-stream", headers=headers)
