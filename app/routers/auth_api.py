"""
Signing in from a client that is not a browser (a terminal tool).

    POST /auth/api-key      AuthCenter access token in, the account's API key out

A browser user signs in through oauth2-proxy and reads their key on the
dashboard. A terminal tool cannot hold that cookie session, so it signs in
with AuthCenter itself (AuthCenter's own CLI flow), gets the same access
token the web UI gets, and trades it here, once, for the account's API key.
From then on it uses the key like every other API client. This is the one
route that gives a key for the token:

- the model API (``/v1/*`` inference, ``/azure/*``, ``/aws/*``) looks a
  bearer value up as an API key, and a token is a 401 there;
- the account endpoints (``/v1/me``, ``/v1/usage`` ...) take the token as a
  sign-in too (``deps.get_account_user``): what the dashboard shows, never
  a key;
- ``/admin`` and ``/dashboard`` sit behind oauth2-proxy, and nginx replaces
  ``Authorization`` with the token of the cookie session.

Why its own prefix and not ``/v1``: it hands out a credential, and nothing
under ``/v1`` does. It lives apart, in its own nginx ``location /auth/``
(no oauth2-proxy in front: the client's own ``Authorization`` header must
arrive untouched).

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

from app.core import api_docs, auth, deps
from app.core.logger import logger
from app.models.schema import is_legacy_api_key
from app.services import auth_throttle, ip_allowlist, metrics

router = APIRouter(tags=["auth"])

_ERRORS: dict[int | str, dict] = {
    401: {"description": "No access token, or one that is expired, was not issued for this gateway, or is not "
                         "a token at all (an API key sent here lands here too)."},
    403: {"description": "The token lacks the `read` scope; the account is disabled (unless the token has the "
                         "`admin` scope); the name is a service account's; or the address is one the API IP "
                         "allowlist refuses."},
    429: {"description": "This address sent too many different invalid credentials and is locked for a while "
                         "(`Retry-After`, seconds)."},
}
_bearer = HTTPBearer(auto_error=False)

# The scopes that let an account use the web UI at all (``get_web_user``
# with ``scopes=["read"]``, which ``admin`` also satisfies).
_SCOPES = frozenset({"read", "admin"})


@router.post(
    "/auth/api-key", summary="Trade an SSO access token for the account's API key",
    responses={**api_docs.answers(api_docs.ApiKeyForToken), **_ERRORS},
)
def api_key_for_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> JSONResponse:
    """How a program that is not a browser signs in: it sends the access token it got from the SSO provider and gets its account's API key.

    **Sign-in.** An SSO access token: `Authorization: Bearer <access token>`. Not an API key, and no
    session cookie. No body.

    **Response**

    | Key | Type | Meaning |
    |---|---|---|
    | `object` | string | `api_key` |
    | `username` | string | The account the token names |
    | `api_key` | string | That account's API key, in full |
    | `legacy_key` | bool | The key is of the older, weaker format: regenerate it on the dashboard |

    **Limits and things to know**

    - It returns the key the account already has and never issues a new one, so the person's other
      machines keep working.
    - A first sign-in creates the account, with the default daily limit, as a first visit to the
      dashboard does.
    - Call it once and keep the key: the key does not expire, the token does. The account endpoints
      (`GET /v1/me`, ...) also take the token itself; the model API takes the key only.
    - The token is checked as the web UI checks it: signed by the SSO provider, for this gateway, not
      expired, with the `read` scope.
    - HTTPS only, and never cached (`Cache-Control: no-store`).
    - Every call that names an account is in the audit log, refusals included. The key is not.

    **Errors**

    | Status | When |
    |---|---|
    | 401 | No token, or one that does not verify |
    | 403 | No `read` scope; a disabled account (unless the token has the `admin` scope); a service account's name; an address the IP allowlist refuses |
    | 429 | Too many different invalid credentials from this address (`Retry-After`) |
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
