"""
JWT authentication helper for web UI routes.

With oauth2-proxy in front of nginx, the flow is:
  Browser → nginx → auth_request to oauth2-proxy → inject Authorization header → Gateway

This module decodes the JWT from the Authorization header (injected by nginx)
and returns the authenticated user.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import jwt
from fastapi import HTTPException, Request
from sqlmodel import Session, select

from app.core.config import AUTH_CENTER_APP_ID, AUTH_CENTER_PUBLIC_KEY_PATH
from app.core.logger import logger
from app.models.schema import User

_ALGORITHM = "RS256"


@lru_cache
def _load_public_key() -> str:
    return Path(AUTH_CENTER_PUBLIC_KEY_PATH).read_text()


def _decode_jwt(token: str) -> dict | None:
    """Decode and verify a JWT from AuthCenter. Returns payload or None."""
    try:
        return jwt.decode(
            token,
            _load_public_key(),
            algorithms=[_ALGORITHM],
            audience=AUTH_CENTER_APP_ID,
            issuer="auth-center",
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

    # Auto-provision user on first visit
    user = session.exec(select(User).where(User.username == username)).first()
    if user is None:
        user = User(username=username)
        session.add(user)
        session.commit()
        session.refresh(user)
        logger.info("Auto-provisioned user '{}' via JWT", username)

    session.expunge(user)
    user.is_admin = "admin" in scopes

    return user, scopes, payload
