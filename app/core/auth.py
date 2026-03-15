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
from fastapi import HTTPException, Request
from sqlmodel import Session, select

from app.core.config import AUTH_BASE_URL, AUTH_CENTER_APP_ID, AUTH_CENTER_PUBLIC_KEY_PATH
from app.core.logger import logger
from app.models.schema import User

_ALGORITHM = "RS256"

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


def get_web_user(request: Request, session: Session) -> tuple[User, list[str], dict]:
    """Extract and validate the JWT from the Authorization header.

    Returns (User, scopes, payload).
    Raises HTTPException(401) if the token is missing or invalid.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing access token.")

    token = auth.removeprefix("Bearer ")
    payload = _decode_jwt(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid access token.")

    username: str = payload.get("sub", "")
    scopes: list[str] = payload.get("scopes", [])

    display_name: str = payload.get("display_name", "")
    org_code: str = payload.get("org_id", "") or payload.get("org_code", "")

    # Auto-provision user on first visit
    user = session.exec(select(User).where(User.username == username)).first()
    if user is None:
        user = User(username=username, display_name=display_name, org_code=org_code)
        session.add(user)
        session.commit()
        session.refresh(user)
        logger.info("Auto-provisioned user '{}' via JWT", username)
    else:
        # Update display_name / org_code if changed in IdP
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
    user.is_admin = "admin" in scopes

    return user, scopes, payload
