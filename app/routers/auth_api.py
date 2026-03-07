"""
OAuth2 SSO authentication via AuthCenter + session logout.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app.core.config import (
    AUTH_CENTER_APP_ID,
    AUTH_CENTER_BASE_URL,
    AUTH_CENTER_CLIENT_SECRET,
    AUTH_CENTER_PUBLIC_KEY_PATH,
    AUTH_CENTER_REDIRECT_URI,
)
from app.core.database import get_session
from app.core.logger import logger
from app.core.server_state import get_client
from app.models.schema import User

router = APIRouter(prefix="/auth", tags=["auth"])

LOGIN_URL = (
    f"{AUTH_CENTER_BASE_URL}/auth/login"
    f"?app_id={AUTH_CENTER_APP_ID}"
    f"&redirect_uri={AUTH_CENTER_REDIRECT_URI}"
)

_ALGORITHM = "RS256"
_TOKEN_MAX_AGE = 12 * 60 * 60  # 12 hours


@lru_cache
def _load_public_key() -> str:
    return Path(AUTH_CENTER_PUBLIC_KEY_PATH).read_text()


def decode_jwt(token: str) -> dict | None:
    """Decode and verify a JWT from AuthCenter. Returns payload or None."""
    try:
        return jwt.decode(
            token,
            _load_public_key(),
            algorithms=[_ALGORITHM],
            audience=AUTH_CENTER_APP_ID,
        )
    except jwt.PyJWTError:
        return None


@router.get("/callback")
async def auth_callback(
    request: Request,
    code: str = Query(...),
    session: Session = Depends(get_session),
):
    """OAuth2 callback: exchange authorization code for JWT, provision user, set session."""
    client = get_client()
    resp = await client.post(
        f"{AUTH_CENTER_BASE_URL}/auth/token",
        json={
            "code": code,
            "app_id": AUTH_CENTER_APP_ID,
            "client_secret": AUTH_CENTER_CLIENT_SECRET,
        },
        timeout=10.0,
    )

    if resp.status_code != 200:
        error = resp.json().get("error", "unknown")
        logger.warning("OAuth token exchange failed: {}", error)
        if error == "invalid_grant":
            return RedirectResponse(LOGIN_URL, status_code=303)
        raise HTTPException(500, f"Token exchange failed: {error}")

    access_token = resp.json()["access_token"]
    payload = decode_jwt(access_token)
    if payload is None:
        raise HTTPException(500, "Failed to validate JWT from AuthCenter.")

    employee_name = payload["sub"]
    scopes = payload.get("scopes", [])

    # Auto-provision or update user in DB
    user = session.exec(select(User).where(User.username == employee_name)).first()
    if user is None:
        user = User(username=employee_name)
        session.add(user)
        session.commit()
        session.refresh(user)
        logger.info("Auto-provisioned user '{}' via OAuth", employee_name)

    # Store in session
    request.session["user_id"] = user.id
    request.session["username"] = user.username
    request.session["scopes"] = scopes

    response = RedirectResponse("/dashboard", status_code=303)
    return response


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)
