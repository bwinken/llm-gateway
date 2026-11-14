"""
Keycloak as the SSO provider (``SSO_PROVIDER=keycloak``; the default is
AuthCenter, see ``app/core/auth.py``).

Only the token check differs between the two providers. ``decode`` verifies
a Keycloak access token and returns it in the shape AuthCenter's tokens
already have, so nothing above ``auth._decode_jwt`` knows which provider
signed it:

    Keycloak access token                          what the gateway reads
    ---------------------------------------------  ----------------------
    preferred_username  (KEYCLOAK_USERNAME_CLAIM)   sub           the account
    resource_access.<client>.roles                  scopes        read / admin
    name                                            display_name
    org_code            (KEYCLOAK_ORG_CLAIM)        org_code

What is checked:

- the signature, with the realm's own keys, fetched from its JWKS endpoint
  (they rotate; Keycloak has no key file to copy the way AuthCenter has);
- ``iss`` equals ``KEYCLOAK_ISSUER_URL`` (the realm URL) and ``exp``;
- it is an access token (``typ: Bearer``): an ID token is signed with the
  same key and names the same person, but it is the browser's proof of
  sign-in, not a permission to call anything;
- it was issued for this gateway: ``KEYCLOAK_CLIENT_ID`` is in ``aud``, or
  is the ``azp`` (a Keycloak access token names its own client in ``azp``
  and not in ``aud`` unless an audience mapper adds it).

Permissions are the **client's** roles (``read``, ``admin``), never realm
roles: a realm-wide role called ``admin`` is somebody else's permission for
something else, and must not open the gateway's admin pages.

The account is the username, as with AuthCenter, so existing accounts are
found again after the switch as long as Keycloak knows people by the same
usernames. Keycloak's own ``sub`` is a UUID and is not used.
"""

from __future__ import annotations

import asyncio
import os
import ssl
import threading
import time
from typing import Any, NamedTuple

import httpx
import jwt

from app.core.logger import logger

# Asymmetric only. The key always comes from the JWKS and the token's ``alg``
# must be the one that key is published for, so a token cannot pick its own.
_ALGORITHMS = frozenset({
    "RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512",
})
_LEEWAY_S = 5
_JWKS_TIMEOUT_S = 5.0
# The key set is read again after this long, so a key the realm has removed
# stops being trusted without a restart.
_KEYS_MAX_AGE_S = 600.0
# ... and at most this often, whatever arrives: a token naming a key id the
# gateway has not seen makes it look (that is how a rotated key is picked
# up), and ``POST /auth/api-key`` is reachable without a login, so without
# the pause anyone could make the gateway call Keycloak once per request.
_REFETCH_MIN_S = 30.0


def settings() -> dict[str, str]:
    """The Keycloak settings, read from the environment on each call."""
    issuer = os.getenv("KEYCLOAK_ISSUER_URL", "").strip().rstrip("/")
    return {
        # The realm URL, exactly as the tokens carry it in ``iss``:
        # https://<keycloak>/realms/<realm>
        "issuer": issuer,
        "client_id": os.getenv("KEYCLOAK_CLIENT_ID", "").strip() or "llm_gateway",
        # Only needed when the gateway reaches Keycloak at another address
        # than the one in ``iss`` (an internal name behind the public one).
        "jwks_url": os.getenv("KEYCLOAK_JWKS_URL", "").strip()
        or (f"{issuer}/protocol/openid-connect/certs" if issuer else ""),
        "username_claim": os.getenv("KEYCLOAK_USERNAME_CLAIM", "").strip() or "preferred_username",
        "org_claim": os.getenv("KEYCLOAK_ORG_CLAIM", "").strip() or "org_code",
        # PEM bundle for a Keycloak certificate signed by an internal CA.
        "ca_file": os.getenv("KEYCLOAK_CA_FILE", "").strip(),
    }


def missing_settings() -> list[str]:
    """Names of the settings Keycloak sign-in cannot work without."""
    return [] if settings()["issuer"] else ["KEYCLOAK_ISSUER_URL"]


# ---------------------------------------------------------------------------
# Signing keys
# ---------------------------------------------------------------------------

class _Keys(NamedTuple):
    url: str
    by_kid: dict[str, Any]  # kid -> jwt.PyJWK
    fetched_at: float  # time.monotonic() of the last successful read
    attempted_at: float  # ... of the last read, successful or not


_NEVER = float("-inf")
# Replaced as a whole, never mutated: readers take the reference without a
# lock, so a reader on the event loop can never wait behind a fetch.
_keys = _Keys("", {}, _NEVER, _NEVER)
_fetch_lock = threading.Lock()


def _fetch_jwks(url: str, ca_file: str) -> dict:
    """GET the realm's key set. Blocking: never call it on the event loop."""
    verify: Any = ssl.create_default_context(cafile=ca_file) if ca_file else True
    resp = httpx.get(url, timeout=_JWKS_TIMEOUT_S, verify=verify)
    resp.raise_for_status()
    return resp.json()


def _parse_jwks(data: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    entries = data.get("keys") if isinstance(data, dict) else None
    for raw in entries or []:
        # A realm also publishes its encryption key (``use: enc``).
        if not isinstance(raw, dict) or not raw.get("kid") or raw.get("use") not in (None, "sig"):
            continue
        try:
            key = jwt.PyJWK.from_dict(raw)
        except Exception:
            continue  # a key type this PyJWT cannot use
        if key.algorithm_name in _ALGORITHMS:
            out[str(raw["kid"])] = key
    if not out:
        raise ValueError("no signing key in the key set")
    return out


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _read_keys(previous: _Keys, s: dict[str, str]) -> _Keys:
    now = time.monotonic()
    try:
        by_kid = _parse_jwks(_fetch_jwks(s["jwks_url"], s["ca_file"]))
    except Exception as exc:
        # Keep the keys already known: Keycloak being unreachable for a
        # moment must not sign everybody out of the web UI.
        logger.error(
            "Keycloak signing keys not read | url={} error={}: {} | known_keys={}",
            s["jwks_url"], type(exc).__name__, exc, len(previous.by_kid),
        )
        return previous._replace(attempted_at=now)
    return _Keys(s["jwks_url"], by_kid, now, now)


def _signing_key(kid: str, s: dict[str, str]) -> Any | None:
    """The realm's key with this id, reading the key set when it is unknown
    or old, at most every ``_REFETCH_MIN_S``.

    Never reads on the event loop: there it answers from what is already
    known. Every caller that decides something runs in a threadpool thread
    (``get_web_user``, ``POST /auth/api-key``); ``AuditMiddleware`` names its
    actor on the loop, after the handler has checked the same token.
    """
    global _keys
    keys = _keys
    if keys.url != s["jwks_url"]:
        keys = _Keys(s["jwks_url"], {}, _NEVER, _NEVER)

    def due(k: _Keys) -> bool:
        now = time.monotonic()
        wanted = kid not in k.by_kid or now - k.fetched_at > _KEYS_MAX_AGE_S
        return wanted and now - k.attempted_at >= _REFETCH_MIN_S

    if due(keys) and not _on_event_loop():
        with _fetch_lock:
            # Another thread may have read them while this one waited.
            if _keys.url == s["jwks_url"]:
                keys = _keys
            if due(keys):
                keys = _keys = _read_keys(keys, s)
    return keys.by_kid.get(kid)


def reset_keys() -> None:
    """Forget the cached keys (tests)."""
    global _keys
    _keys = _Keys("", {}, _NEVER, _NEVER)


# ---------------------------------------------------------------------------
# Token check
# ---------------------------------------------------------------------------

def _text(value: Any) -> str:
    """A claim as text. A multivalued user attribute arrives as a list."""
    if isinstance(value, list):
        value = value[0] if value else ""
    return value.strip() if isinstance(value, str) else ""


def _issued_for(claims: dict, client_id: str) -> bool:
    aud = claims.get("aud")
    audiences = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
    return client_id in audiences or claims.get("azp") == client_id


def decode(token: str) -> dict | None:
    """Verify a Keycloak access token. Returns the payload in AuthCenter's
    shape (``sub``, ``scopes``, ``display_name``, ``org_code``) or None."""
    s = settings()
    if not s["issuer"]:
        logger.error("SSO_PROVIDER=keycloak but KEYCLOAK_ISSUER_URL is not set: every sign-in is refused")
        return None

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        logger.warning("JWT validation failed: {}", e)
        return None
    kid = header.get("kid")
    key = _signing_key(kid, s) if isinstance(kid, str) and kid else None
    if key is None:
        logger.warning("JWT validation failed: not signed with a key of the Keycloak realm")
        return None

    try:
        claims = jwt.decode(
            token,
            key.key,
            # The algorithm the realm publishes this key for, never one the
            # token chose.
            algorithms=[key.algorithm_name],
            issuer=s["issuer"],
            leeway=_LEEWAY_S,
            # The audience is checked below: Keycloak names its own client
            # in ``azp``, which PyJWT's audience check does not read.
            options={"verify_aud": False, "require": ["exp", "iss"]},
        )
    except jwt.ExpiredSignatureError:
        logger.warning("JWT expired")
        return None
    except jwt.InvalidIssuerError:
        logger.warning("JWT issuer mismatch")
        return None
    except jwt.PyJWTError as e:
        logger.warning("JWT validation failed: {}", e)
        return None

    if claims.get("typ") != "Bearer":
        logger.warning("JWT validation failed: not an access token (typ={!r})", claims.get("typ"))
        return None
    if not _issued_for(claims, s["client_id"]):
        logger.warning("JWT audience mismatch")
        return None
    username = _text(claims.get(s["username_claim"]))
    if not username:
        logger.warning("JWT validation failed: no '{}' claim to name the account", s["username_claim"])
        return None

    access = claims.get("resource_access")
    client = access.get(s["client_id"]) if isinstance(access, dict) else None
    roles = client.get("roles") if isinstance(client, dict) else None
    return {
        "sub": username,
        "scopes": [r for r in roles if isinstance(r, str)] if isinstance(roles, list) else [],
        "display_name": _text(claims.get("name")),
        "org_code": _text(claims.get(s["org_claim"])),
    }
