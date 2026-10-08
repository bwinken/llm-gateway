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

        db_session.add(User(username="alice", display_name="Alice Wu", org_code="ENG", daily_limit_usd=20.0, can_use_azure=True))
        db_session.add(User(username="carol", display_name="Carol Lin", org_code="RAD", daily_limit_usd=10.0))
        db_session.add(User(username="app_billing", display_name="", org_code="", daily_limit_usd=0.0))
        db_session.commit()

        resp = client.get(
            "/admin/api/export/users.csv",
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        body = resp.content.decode("utf-8-sig")
        header = body.splitlines()[0]
        assert "username,display_name,org_code" in header
        # Access-control flags exported so admins can filter cloud-enabled users
        assert "can_use_azure" in header
        assert "can_use_bedrock" in header
        assert "is_disabled" in header
        assert "alice" in body
        assert "Alice Wu" in body
        assert "ENG" in body
        # alice has the Azure flag, carol doesn't — spot-check both values
        alice_row = next(line for line in body.splitlines() if line.startswith("alice,"))
        carol_row = next(line for line in body.splitlines() if line.startswith("carol,"))
        assert alice_row.split(",")[5] == "true"   # can_use_azure column
        assert carol_row.split(",")[5] == "false"
        # app_* accounts excluded
        assert "app_billing" not in body

    def test_export_users_csv_requires_admin(self, client, db_session):
        resp = client.get(
            "/admin/api/export/users.csv",
            headers=web_auth_header(sub="testuser", scopes=["read"]),
        )
        assert resp.status_code == 403


class TestDeleteUser:
    """PostgreSQL enforces foreign keys; in-memory SQLite only does with the
    pragma on, so these tests turn it on to catch a missed reference."""

    @staticmethod
    def _fk_on(on: bool):
        from tests.conftest import _test_engine

        with _test_engine.connect() as conn:
            conn.exec_driver_sql(f"PRAGMA foreign_keys={'ON' if on else 'OFF'}")

    def test_delete_user_with_every_reference(self, client, db_session, admin_user, test_user):
        from datetime import datetime, timezone

        from sqlmodel import select

        from app.models.schema import AnomalyEvent, AppOwner, UsageLog, User

        now = datetime.now(timezone.utc)
        legacy_app = User(username="app_legacy", owner_id=test_user.id)
        db_session.add(legacy_app)
        db_session.commit()
        db_session.add(AppOwner(app_id=legacy_app.id, owner_id=test_user.id))
        db_session.add(UsageLog(user_id=test_user.id, model="m", endpoint="/v1/chat/completions"))
        db_session.add(AnomalyEvent(
            scope=f"user:{test_user.id}", rule="cost_spike", user_id=test_user.id,
            window_start=now, window_end=now,
        ))
        db_session.commit()
        uid, app_id = test_user.id, legacy_app.id

        self._fk_on(True)
        try:
            resp = client.post(
                f"/admin/users/{uid}/delete",
                headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
                follow_redirects=False,
            )
        finally:
            self._fk_on(False)

        assert resp.status_code == 303
        db_session.expire_all()
        assert db_session.get(User, uid) is None
        assert db_session.get(User, app_id).owner_id is None
        assert db_session.exec(select(AnomalyEvent).where(AnomalyEvent.user_id == uid)).all() == []
        assert db_session.exec(select(AppOwner)).all() == []
