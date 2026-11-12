"""
Signing in from a client that is not a browser (a terminal tool).

    POST /auth/api-key      AuthCenter access token in, the account's API key out

A browser user signs in through oauth2-proxy and reads their key on the
dashboard. A terminal tool cannot hold that cookie session, so it signs in
with AuthCenter itself (AuthCenter's own CLI flow), gets the same access
token the web UI gets, and trades it here, once, for the account's API key.
From then on it uses the key like every other API client; the token is not
used again and nothing else in the gateway accepts it from a client:

- ``/v1/*``, ``/azure/*``, ``/aws/*`` look a bearer value up as an API key;
- ``/admin`` and ``/dashboard`` sit behind oauth2-proxy, and nginx replaces
  ``Authorization`` with the token of the cookie session.

Why its own prefix and not ``/v1``: ``/v1`` is the model API, authenticated
by API key on every route (``TestEveryApiRouteIsChecked``). This is the one
route authenticated by an SSO token, so it lives apart, in its own nginx
``location /auth/`` (no oauth2-proxy in front: the client's own
``Authorization`` header must arrive untouched).

The token is validated exactly as the web UI validates it
(``auth._decode_jwt``: RS256 with AuthCenter's public key, audience, issuer)
and the account is found or auto-provisioned the same way
(``auth._sync_user``), so a first sign-in from a terminal creates the
account just as a first visit to the dashboard does.

It hands out a credential, so:

- the existing key is returned, never a new one (a new one would log the
  user's other machines out);
- the failed-auth throttle counts invalid tokens like invalid keys, and a
  locked address is refused before the token is looked at;
- a disabled account is refused, except an admin by the token's scope:
  permissions are AuthCenter's to define, as on the web UI;
- the API IP allowlist refuses here as it would refuse the key (better told
  at sign-in than on every later request);
- every call by an identified account is in the audit log
  (``audit._AUDITED_TOKEN_ROUTES``), the key itself redacted.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session

from app.core import auth, deps
from app.core.logger import logger
from app.models.schema import is_legacy_api_key
from app.services import auth_throttle, ip_allowlist, metrics

router = APIRouter(tags=["auth"])
_bearer = HTTPBearer(auto_error=False)

# The scopes that let an account use the web UI at all (``get_web_user``
# with ``scopes=["read"]``, which ``admin`` also satisfies).
_SCOPES = frozenset({"read", "admin"})


@router.post("/auth/api-key")
def api_key_for_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> JSONResponse:
    """The API key of the account an AuthCenter access token belongs to.

    ``Authorization: Bearer <access token>``; no body. Replies
    ``{"object": "api_key", "username", "api_key", "legacy_key"}``.
    401 for a missing, expired or foreign token, 403 for a token without
    the ``read`` scope, a disabled account (unless the token has the
    ``admin`` scope) or an address the IP allowlist refuses, 429 while the
    address is locked after too many bad credentials.

    A plain ``def``: it reads and may write the database and awaits nothing.
    """
    token = credentials.credentials if credentials is not None else ""
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing access token.")

    client_addr = request.client.host if request.client else None
    wait_s = auth_throttle.retry_after(client_addr)
    if wait_s is not None:
        metrics.observe_rejection("auth_throttle")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed authentication attempts from this address. Try again later.",
            headers={"Retry-After": str(wait_s)},
        )

    payload = auth._decode_jwt(token)  # module attribute: tests patch it
    username = str((payload or {}).get("sub") or "")
    if not username:
        # An API key sent here by mistake lands in this branch too.
        logger.warning("Key exchange rejected: invalid access token | client={}", client_addr or "-")
        auth_throttle.record_failure(client_addr, [token])
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid access token.")

    scopes = payload.get("scopes") or []
    if not _SCOPES.intersection(scopes):
        logger.warning("Key exchange rejected: no read scope | user={}", username)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient scope: 'read' required.")

    # Own short-lived session, like the API-key auth: nothing here is tied
    # to the request's lifetime.
    with Session(deps.engine) as session:
        user = auth._sync_user(
            session,
            username,
            str(payload.get("display_name") or ""),
            str(payload.get("org_id") or payload.get("org_code") or ""),
        )
    # A disabled account gets no key, unless AuthCenter says it is an admin:
    # who is an admin is decided there (the token's scope), as on the web UI
    # (``get_web_user``), not by the ``is_admin`` column of the account.
    if user.is_disabled and "admin" not in scopes:
        logger.warning("Key exchange rejected: user '{}' is disabled", user.username)
        raise auth.AccountDisabledError(user.username)
    ip_allowlist.check(request, user)

    logger.info("API key handed out for an access token | user={} client={}", user.username, client_addr or "-")
    return JSONResponse(
        {
            "object": "api_key",
            "username": user.username,
            "api_key": user.api_key,
            "legacy_key": is_legacy_api_key(user.api_key),
        },
        headers={"Cache-Control": "no-store"},
    )
