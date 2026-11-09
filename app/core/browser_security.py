"""Browser-facing protections: CORS policy and cross-site request refusal.

Two kinds of callers reach this app:

- API clients (``/v1/*``, ``/azure/*``, ``/aws/*``, ``/me`` and the unprefixed
  aliases) authenticate with a key they set themselves in ``Authorization`` or
  ``x-api-key``. A browser never attaches that key on its own, so cross-origin
  calls are safe to allow and browser-based tools rely on it.
- The Web UI and admin (``/dashboard*``, ``/admin*``) authenticate with the
  oauth2-proxy cookie, which nginx turns into a JWT. A browser attaches that
  cookie on its own, so another site must neither read these responses nor
  make the logged-in user change something.

CORS therefore allows any origin **without credentials**: Starlette never
sends ``Access-Control-Allow-Credentials``, and a browser withholds the
response of a credentialed (cookie-carrying) cross-origin fetch, e.g.
``GET /admin/users/{id}/api-key``, unless that header says ``true``. Starlette
still echoes the caller's ``Origin`` when a cookie is present; that alone
releases nothing. With ``allow_credentials=True`` the echo plus the header
would hand admin responses to any site the user visits.

CORS only governs reads. ``CrossSiteRequestGuard`` refuses state-changing
requests to the cookie-authenticated paths that a browser marks as coming from
another site. ``SameSite=lax`` does not cover this: a sibling host under the
same registrable domain is *same-site*, so its form posts carry the cookie.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.logger import logger

CORS_OPTIONS: dict = {
    "allow_origins": ["*"],
    # Never True: see the module docstring. API keys travel in headers the
    # client sets itself, which CORS does not count as credentials.
    "allow_credentials": False,
    "allow_methods": ["*"],
    "allow_headers": ["*"],
}

# Paths whose handlers authenticate with the browser's SSO cookie (via the
# JWT nginx injects). Every unsafe route depending on get_web_user must sit
# under one of them (tests/test_browser_security.py walks the routes).
COOKIE_AUTH_PREFIXES: tuple[str, ...] = ("/admin", "/dashboard")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Sec-Fetch-Site values a browser sends for requests made by our own pages
# ("same-origin") or by the user directly (typed URL, bookmark: "none").
_ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})


def is_cookie_auth_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in COOKIE_AUTH_PREFIXES)


def _hostname(value: str) -> str:
    """Hostname of an Origin URL or a Host header, lower-case, without port."""
    netloc = urlsplit(value).netloc if "://" in value else value
    return (urlsplit("//" + netloc).hostname or "").lower()


def refusal_reason(method: str, path: str, headers: dict[str, str]) -> str | None:
    """Why this request must be refused as cross-site, or None to let it through.

    Uses what the browser itself computed, ``Sec-Fetch-Site``, when present
    (pages cannot forge it and it stays correct behind proxies that rewrite
    ``Host``). Otherwise compares the ``Origin`` hostname with ``Host``.
    A request carrying neither header did not come from a browser page
    (curl, scripts), so there is no ambient cookie to abuse: let it through.
    """
    if method.upper() in SAFE_METHODS or not is_cookie_auth_path(path):
        return None
    fetch_site = headers.get("sec-fetch-site", "").strip().lower()
    if fetch_site:
        if fetch_site in _ALLOWED_FETCH_SITES:
            return None
        return f"sec-fetch-site={fetch_site}"
    origin = headers.get("origin", "").strip()
    if not origin:
        return None
    if origin.lower() == "null":
        return "origin=null"
    host = headers.get("host", "")
    if _hostname(origin) and _hostname(origin) == _hostname(host):
        return None
    return "origin does not match host"


class CrossSiteRequestGuard:
    """Pure-ASGI middleware: 403 cross-site unsafe requests to cookie-auth paths.

    Pure ASGI (not BaseHTTPMiddleware) so it never wraps streaming bodies;
    it only looks at the request headers before handing over.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            headers: dict[str, str] = {}
            for k, v in scope.get("headers") or []:
                try:
                    headers[k.decode("latin-1").lower()] = v.decode("latin-1")
                except Exception:
                    pass
            method = scope.get("method", "GET")
            path = scope.get("path", "")
            reason = refusal_reason(method, path, headers)
            if reason is not None:
                client = scope.get("client") or ("", 0)
                logger.warning(
                    "Cross-site request refused | method={} path={} reason={} origin={!r} client={}",
                    method, path, reason, headers.get("origin", ""), client[0],
                )
                response = JSONResponse(
                    {"detail": "Cross-site request refused."}, status_code=403,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def add_browser_security(app: FastAPI) -> None:
    """Install the CORS policy and the cross-site guard on ``app``.

    The guard is added first so it runs inside CORS: a refused request still
    gets CORS headers and the browser reports a 403 rather than a CORS error.
    """
    app.add_middleware(CrossSiteRequestGuard)
    app.add_middleware(CORSMiddleware, **CORS_OPTIONS)
