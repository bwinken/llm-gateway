"""Tests for admin user-creation endpoints."""

from __future__ import annotations

from tests.conftest import web_auth_header


class TestCreateUserAPI:
    """POST /admin/users (JWT admin auth)."""

    def test_create_app_account(self, client, db_session, admin_user):
        resp = client.post(
            "/admin/users",
            json={"username": "app_my_service", "daily_limit_usd": 50.0},
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
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
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "new_person"
        assert data["daily_limit_usd"] == 10.0  # default

    def test_create_duplicate_returns_409(self, client, db_session, admin_user):
        client.post(
            "/admin/users",
            json={"username": "app_dup"},
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
        )
        resp = client.post(
            "/admin/users",
            json={"username": "app_dup"},
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
        )
        assert resp.status_code == 409

    def test_create_empty_username_returns_400(self, client, db_session, admin_user):
        resp = client.post(
            "/admin/users",
            json={"username": ""},
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
        )
        assert resp.status_code == 400

    def test_non_admin_cannot_create(self, client, db_session):
        resp = client.post(
            "/admin/users",
            json={"username": "app_sneaky"},
            headers=web_auth_header(sub="testuser", scopes=["read"]),
        )
        assert resp.status_code == 403

    def test_export_users_csv(self, client, db_session, admin_user):
        from app.models.schema import User

        db_session.add(User(username="alice", display_name="Alice Wu", org_code="ENG", daily_limit_usd=20.0))
        db_session.add(User(username="app_billing", display_name="", org_code="", daily_limit_usd=0.0))
        db_session.commit()

        resp = client.get(
            "/admin/api/export/users.csv",
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        body = resp.content.decode("utf-8-sig")
        assert "username,display_name,org_code" in body.splitlines()[0]
        assert "alice" in body
        assert "Alice Wu" in body
        assert "ENG" in body
        # app_* accounts excluded
        assert "app_billing" not in body

    def test_export_users_csv_requires_admin(self, client, db_session):
        resp = client.get(
            "/admin/api/export/users.csv",
            headers=web_auth_header(sub="testuser", scopes=["read"]),
        )
        assert resp.status_code == 403
