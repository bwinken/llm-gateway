"""
JWT authentication helper for web UI routes.

With oauth2-proxy in front of nginx, the flow is:
  Browser → nginx → auth_request to oauth2-proxy → inject Authorization header → Gateway

This module decodes the JWT from the Authorization header (injected by nginx)
and returns the authenticated user.
"""

from __future__ import annotations

import os
from pathlib import Path

import jwt
from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, SecurityScopes
from sqlmodel import Session, select

from app.core.config import AUTH_BASE_URL, AUTH_CENTER_APP_ID, AUTH_CENTER_PUBLIC_KEY_PATH, get_default_daily_limit


class AccountDisabledError(Exception):
    """Raised when an authenticated user has is_disabled=True.

    Caught by a global exception handler that renders an HTML page for
    browser requests and returns JSON 403 for API clients.
    """
    def __init__(self, username: str = ""):
        self.username = username
        super().__init__(f"Account '{username}' is disabled.")
from app.core.database import get_session
from app.core.logger import logger
from app.models.schema import User

_ALGORITHM = "RS256"
_bearer = HTTPBearer(auto_error=False)

# Cache public key with mtime check so key rotation takes effect without restart.
_pk_cache: tuple[float, str] = (0.0, "")


def _load_public_key() -> str:
    global _pk_cache
    p = Path(AUTH_CENTER_PUBLIC_KEY_PATH)
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        # File missing — return cached value if any, otherwise raise
        if _pk_cache[1]:
            return _pk_cache[1]
        raise
    if mtime != _pk_cache[0]:
        _pk_cache = (mtime, p.read_text())
    return _pk_cache[1]


def _decode_jwt(token: str) -> dict | None:
    """Decode and verify a JWT from AuthCenter. Returns payload or None."""
    try:
        return jwt.decode(
            token,
            _load_public_key(),
            algorithms=[_ALGORITHM],
            audience=AUTH_CENTER_APP_ID,
            issuer=AUTH_BASE_URL,
            leeway=5,
        )
    except jwt.ExpiredSignatureError:
        logger.warning("JWT expired")
        return None
    except jwt.InvalidAudienceError:
        logger.warning("JWT audience mismatch")
        return None
    except jwt.InvalidIssuerError:
        logger.warning("JWT issuer mismatch")
        return None
    except jwt.PyJWTError as e:
        logger.warning("JWT validation failed: {}", e)
        return None


def _sync_user(session: Session, username: str, display_name: str, org_code: str) -> User:
    """Find or auto-provision a user, syncing IdP fields."""
    user = session.exec(select(User).where(User.username == username)).first()
    if user is None:
        user = User(
            username=username,
            display_name=display_name,
            org_code=org_code,
            daily_limit_usd=get_default_daily_limit(),
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        logger.info("Auto-provisioned user '{}' via JWT", username)
    else:
        changed = False
        if display_name and user.display_name != display_name:
            user.display_name = display_name
            changed = True
        if org_code and user.org_code != org_code:
            user.org_code = org_code
            changed = True
        if changed:
            session.add(user)
            session.commit()
            session.refresh(user)
    session.expunge(user)
    return user


def get_web_user(
    security_scopes: SecurityScopes,
    request: Request,
    session: Session = Depends(get_session),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User:
    """FastAPI Security dependency: validate JWT and enforce declared scopes.

    Usage in routes:
        user: User = Security(get_web_user, scopes=["read"])   # read or admin
        user: User = Security(get_web_user, scopes=["admin"])  # admin only
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing access token.")

    payload = _decode_jwt(credentials.credentials)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid access token.")

    username: str = payload.get("sub", "")
    token_scopes: list[str] = payload.get("scopes", [])
    display_name: str = payload.get("display_name", "")
    org_code: str = payload.get("org_id", "") or payload.get("org_code", "")

    # Check required scopes — "admin" satisfies any scope requirement
    if security_scopes.scopes:
        if "admin" not in token_scopes:
            for scope in security_scopes.scopes:
                if scope not in token_scopes:
                    raise HTTPException(
                        status_code=403,
                        detail=f"Insufficient scope: '{scope}' required.",
                    )

    user = _sync_user(session, username, display_name, org_code)
    user.is_admin = "admin" in token_scopes
    if user.is_disabled and not user.is_admin:
        # Admins keep web-UI access even when their own row is flagged
        # disabled, so they can never accidentally lock themselves out.
        logger.warning("Web auth rejected: user '{}' is disabled", user.username)
        raise AccountDisabledError(user.username)
    return user
