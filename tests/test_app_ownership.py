"""Tests for app account ownership feature."""

from __future__ import annotations

import pytest
from sqlmodel import Session

from app.models.schema import User
from tests.conftest import auth_header, web_auth_header


@pytest.fixture()
def owner_user(db_session: Session) -> User:
    """A regular user who will own app accounts."""
    user = User(
        username="owner_person",
        api_key="sk-ownerkey789",
        daily_limit_usd=100.0,
        is_admin=False,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def owned_app(db_session: Session, owner_user: User) -> User:
    """An app account owned by owner_user."""
    app = User(
        username="app_my_service",
        api_key="sk-appkey-owned",
        daily_limit_usd=50.0,
        owner_id=owner_user.id,
    )
    db_session.add(app)
    db_session.commit()
    db_session.refresh(app)
    return app


@pytest.fixture()
def unowned_app(db_session: Session) -> User:
    """An app account with no owner."""
    app = User(
        username="app_no_owner",
        api_key="sk-appkey-noowner",
        daily_limit_usd=50.0,
    )
    db_session.add(app)
    db_session.commit()
    db_session.refresh(app)
    return app


class TestCreateWithOwner:
    """Admin API: create app accounts with owner_id."""

    def test_create_app_with_owner(self, client, db_session, admin_user, owner_user):
        resp = client.post(
            "/admin/users",
            json={
                "username": "app_new_service",
                "owner_id": owner_user.id,
            },
            headers=auth_header(admin_user.api_key),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["owner_id"] == owner_user.id

    def test_create_app_with_invalid_owner(self, client, db_session, admin_user):
        resp = client.post(
            "/admin/users",
            json={
                "username": "app_bad_owner",
                "owner_id": 99999,
            },
            headers=auth_header(admin_user.api_key),
        )
        assert resp.status_code == 400
        assert "Owner user not found" in resp.json()["detail"]

    def test_create_app_without_owner(self, client, db_session, admin_user):
        resp = client.post(
            "/admin/users",
            json={"username": "app_solo"},
            headers=auth_header(admin_user.api_key),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["owner_id"] is None


class TestUpdateOwner:
    """Admin API: update owner_id via PATCH."""

    def test_set_owner(self, client, db_session, admin_user, unowned_app, owner_user):
        resp = client.patch(
            f"/admin/users/{unowned_app.id}",
            json={"owner_id": owner_user.id},
            headers=auth_header(admin_user.api_key),
        )
        assert resp.status_code == 200
        db_session.refresh(unowned_app)
        assert unowned_app.owner_id == owner_user.id

    def test_clear_owner(self, client, db_session, admin_user, owned_app):
        resp = client.patch(
            f"/admin/users/{owned_app.id}",
            json={"owner_id": None},
            headers=auth_header(admin_user.api_key),
        )
        assert resp.status_code == 200
        db_session.refresh(owned_app)
        assert owned_app.owner_id is None

    def test_set_invalid_owner(self, client, db_session, admin_user, unowned_app):
        resp = client.patch(
            f"/admin/users/{unowned_app.id}",
            json={"owner_id": 99999},
            headers=auth_header(admin_user.api_key),
        )
        assert resp.status_code == 400


class TestListUsersIncludesOwner:
    """Admin API: GET /admin/users includes owner_id."""

    def test_list_includes_owner_id(self, client, db_session, admin_user, owned_app):
        resp = client.get(
            "/admin/users",
            headers=auth_header(admin_user.api_key),
        )
        assert resp.status_code == 200
        users = resp.json()
        app_entry = next(u for u in users if u["username"] == "app_my_service")
        assert "owner_id" in app_entry
        assert app_entry["owner_id"] is not None


class TestRefreshOwnedAppKey:
    """Dashboard: POST /dashboard/app/{id}/refresh-key."""

    def test_refresh_owned_app_key_success(self, client, db_session, owner_user, owned_app):
        """Owner can refresh their own app's key."""
        old_key = owned_app.api_key
        resp = client.post(
            f"/dashboard/app/{owned_app.id}/refresh-key",
            headers=web_auth_header(sub=owner_user.username, scopes=["read"]),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["api_key"] != old_key

    def test_refresh_non_owner_rejected(self, client, db_session, owner_user, owned_app, test_user):
        """Non-owner cannot refresh another user's app key."""
        resp = client.post(
            f"/dashboard/app/{owned_app.id}/refresh-key",
            headers=web_auth_header(sub=test_user.username, scopes=["read"]),
        )
        assert resp.status_code == 403

    def test_refresh_nonexistent_app(self, client, db_session, owner_user):
        """Refreshing a non-existent app returns 404."""
        resp = client.post(
            "/dashboard/app/99999/refresh-key",
            headers=web_auth_header(sub=owner_user.username, scopes=["read"]),
        )
        assert resp.status_code == 404

    def test_refresh_unauthenticated(self, client, db_session, owned_app):
        """Refreshing without auth returns 401."""
        resp = client.post(f"/dashboard/app/{owned_app.id}/refresh-key")
        assert resp.status_code == 401
