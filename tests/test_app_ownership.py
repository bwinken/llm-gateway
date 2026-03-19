"""Tests for app account ownership feature (many-to-many via AppOwner)."""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from app.models.schema import AppOwner, User
from tests.conftest import web_auth_header


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
    """An app account owned by owner_user via AppOwner table."""
    app = User(
        username="app_my_service",
        api_key="sk-appkey-owned",
        daily_limit_usd=50.0,
    )
    db_session.add(app)
    db_session.commit()
    db_session.refresh(app)
    # Create ownership via AppOwner
    db_session.add(AppOwner(app_id=app.id, owner_id=owner_user.id))
    db_session.commit()
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
    """Admin API: create app accounts with owner_ids."""

    def test_create_app_with_owner(self, client, db_session, admin_user, owner_user):
        resp = client.post(
            "/admin/users",
            json={
                "username": "app_new_service",
                "owner_ids": [owner_user.id],
            },
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["owner_ids"] == [owner_user.id]

        # Verify AppOwner record created
        new_user = db_session.exec(
            select(User).where(User.username == "app_new_service")
        ).first()
        ao = db_session.exec(
            select(AppOwner).where(AppOwner.app_id == new_user.id)
        ).first()
        assert ao is not None
        assert ao.owner_id == owner_user.id

    def test_create_app_without_owner(self, client, db_session, admin_user):
        resp = client.post(
            "/admin/users",
            json={"username": "app_solo"},
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["owner_ids"] == []


class TestUpdateOwner:
    """Admin API: update owners via PATCH with owner_ids."""

    def test_set_owner(self, client, db_session, admin_user, unowned_app, owner_user):
        resp = client.patch(
            f"/admin/users/{unowned_app.id}",
            json={"owner_ids": [owner_user.id]},
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
        )
        assert resp.status_code == 200
        ao = db_session.exec(
            select(AppOwner).where(AppOwner.app_id == unowned_app.id)
        ).first()
        assert ao is not None
        assert ao.owner_id == owner_user.id

    def test_clear_owner(self, client, db_session, admin_user, owned_app):
        resp = client.patch(
            f"/admin/users/{owned_app.id}",
            json={"owner_ids": []},
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
        )
        assert resp.status_code == 200
        ao = db_session.exec(
            select(AppOwner).where(AppOwner.app_id == owned_app.id)
        ).first()
        assert ao is None


class TestListUsersIncludesOwner:
    """Admin API: GET /admin/users includes owner_ids."""

    def test_list_includes_owner_ids(self, client, db_session, admin_user, owned_app):
        resp = client.get(
            "/admin/users",
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
        )
        assert resp.status_code == 200
        users = resp.json()
        app_entry = next(u for u in users if u["username"] == "app_my_service")
        assert "owner_ids" in app_entry
        assert len(app_entry["owner_ids"]) > 0


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
