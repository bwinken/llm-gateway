"""Tests for admin user-creation endpoints."""

from __future__ import annotations

from tests.conftest import auth_header
from app.models.schema import User


class TestCreateUserAPI:
    """POST /admin/users (Bearer token auth)."""

    def test_create_app_account(self, client, db_session, admin_user):
        resp = client.post(
            "/admin/users",
            json={"username": "app_my_service", "daily_limit_usd": 50.0},
            headers=auth_header(admin_user.api_key),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["username"] == "app_my_service"
        assert data["api_key"].startswith("sk-")
        assert data["daily_limit_usd"] == 50.0

    def test_create_regular_user(self, client, db_session, admin_user):
        resp = client.post(
            "/admin/users",
            json={"username": "new_person"},
            headers=auth_header(admin_user.api_key),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "new_person"
        assert data["daily_limit_usd"] == 10.0  # default

    def test_create_duplicate_returns_409(self, client, db_session, admin_user):
        client.post(
            "/admin/users",
            json={"username": "app_dup"},
            headers=auth_header(admin_user.api_key),
        )
        resp = client.post(
            "/admin/users",
            json={"username": "app_dup"},
            headers=auth_header(admin_user.api_key),
        )
        assert resp.status_code == 409

    def test_create_empty_username_returns_400(self, client, db_session, admin_user):
        resp = client.post(
            "/admin/users",
            json={"username": ""},
            headers=auth_header(admin_user.api_key),
        )
        assert resp.status_code == 400

    def test_non_admin_cannot_create(self, client, db_session):
        resp = client.post(
            "/admin/users",
            json={"username": "app_sneaky"},
            headers=auth_header("sk-testkey123"),
        )
        assert resp.status_code == 403
