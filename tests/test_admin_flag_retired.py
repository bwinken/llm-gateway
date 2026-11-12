"""``users.is_admin`` is retired as a stored permission.

Who is an admin is AuthCenter's to say: the ``admin`` scope of the access
token, which the web UI reads on every request. The column used to grant an
API key three exemptions (cloud backends without the flags, the concurrency
limit, a disabled account). Nothing grants anything from it any more, the
admin API refuses the field and no listing shows it."""

from __future__ import annotations

import pytest
from sqlmodel import select

from app.models.schema import User
from tests.conftest import auth_header, web_auth_header

ADMIN_KEY = "sk-adminkey456"  # conftest's admin_user: is_admin stored true, no cloud flags


def _admin_headers(admin: User) -> dict:
    return {**web_auth_header(sub=admin.username, scopes=["admin"]), "Accept": "application/json"}


class TestApiKeyHasNoAdminRight:
    def test_no_cloud_backend_without_the_flag(self, client, admin_user):
        headers = auth_header(ADMIN_KEY)
        assert client.get("/azure/v1/models", headers=headers).status_code == 403
        assert client.get("/aws/v1/models", headers=headers).status_code == 403
        listed = {m["id"] for m in client.get("/v1/models", headers=headers).json()["data"]}
        assert "azure-gpt-4" not in listed and "bedrock-claude" not in listed

    def test_a_cloud_alias_is_not_dispatched_to_the_cloud(self, client, admin_user):
        from app.routers import v1_api

        assert v1_api._route_to_azure("azure-gpt-4", admin_user) is False
        assert v1_api._route_to_bedrock("bedrock-claude", admin_user) is False

    def test_the_flags_still_grant_it(self, client, db_session, admin_user):
        admin_user.can_use_azure = True
        db_session.add(admin_user)
        db_session.commit()
        assert client.get("/azure/v1/models", headers=auth_header(ADMIN_KEY)).status_code == 200

    def test_a_disabled_account_is_refused(self, client, db_session, admin_user):
        admin_user.is_disabled = True
        db_session.add(admin_user)
        db_session.commit()
        assert client.get("/v1/me", headers=auth_header(ADMIN_KEY)).status_code == 403

    def test_auth_hands_on_an_account_that_is_no_admin(self, client, db_session, admin_user):
        # One place clears it, so code shared with the web UI (model lists,
        # patches) cannot grant something from a stale row either.
        from app.core import deps

        user = deps._authenticate(None, ADMIN_KEY, check_daily_limit=False)
        assert user.username == "admin" and user.is_admin is False
        # ... without writing anything: the row is as it was.
        db_session.expire_all()
        assert db_session.exec(select(User).where(User.username == "admin")).one().is_admin is True

    def test_the_notice_patch_is_the_accounts_own(self, client, db_session, admin_user):
        from app.models.schema import ClientViolation
        from app.services import client_rules
        from datetime import datetime, timezone

        rule = client_rules.get_rule(db_session, "CC001")
        client_rules.save_settings(db_session, rule.check, dict(
            enabled=True, action=rule.action, title=rule.title, message=rule.message,
            patch='{"A": "base"}', patch_azure='{"A": "azure"}', patch_aws=rule.patch_aws,
            email_subject=rule.email_subject, auto_email=rule.auto_email, email_repeat_days=rule.email_repeat_days,
        ), updated_by="test")
        client_rules.refresh()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        db_session.add(ClientViolation(rule_id="CC001", username="admin", first_seen_at=now, last_seen_at=now))
        db_session.commit()
        body = client.get("/v1/notices", headers=auth_header(ADMIN_KEY)).json()
        assert body["patch"]["set"]["A"] == "base"


class TestAdminApiRefusesTheField:
    def test_create(self, client, db_session, admin_user):
        resp = client.post("/admin/users", json={"username": "newbie", "is_admin": True},
                           headers=_admin_headers(admin_user))
        assert resp.status_code == 400
        assert "AuthCenter" in resp.json()["detail"]
        assert db_session.exec(select(User).where(User.username == "newbie")).first() is None

    @pytest.mark.parametrize("value", [True, False])
    def test_update(self, client, db_session, admin_user, test_user, value):
        resp = client.patch(f"/admin/users/{test_user.id}", json={"is_admin": value, "daily_limit_usd": 5},
                          headers=_admin_headers(admin_user))
        assert resp.status_code == 400
        db_session.expire_all()
        fresh = db_session.get(User, test_user.id)
        # Refused as a whole: nothing else in the body was applied.
        assert fresh.is_admin is False and fresh.daily_limit_usd == 100.0

    def test_without_the_field_nothing_changes(self, client, db_session, admin_user, test_user):
        resp = client.patch(f"/admin/users/{test_user.id}", json={"daily_limit_usd": 5},
                          headers=_admin_headers(admin_user))
        assert resp.status_code == 200, resp.text

    def test_listings_do_not_show_it(self, client, admin_user, test_user):
        headers = _admin_headers(admin_user)
        users = client.get("/admin/users", headers=headers).json()
        assert users and all("is_admin" not in u for u in users)
        table = client.get("/admin/user-table", params={"kind": "users"}, headers=headers).text
        assert "gw-badge-admin" not in table and "data-admin" not in table

    def test_every_account_but_your_own_can_be_disabled(self, client, db_session, admin_user):
        # The stored flag used to hide the Disable button of "admin" rows.
        other = User(username="oldadmin", api_key="sk-old", is_admin=True)
        db_session.add(other)
        db_session.commit()
        db_session.refresh(other)
        table = client.get("/admin/user-table", params={"kind": "users"}, headers=_admin_headers(admin_user)).text
        assert f'action="/admin/users/{other.id}/toggle-disable"' in table
        assert f'action="/admin/users/{other.id}/toggle-concurrency-waive"' in table
        assert f'action="/admin/users/{admin_user.id}/toggle-disable"' not in table  # yourself


class TestAuthCenterDecides:
    """The web UI is unchanged: the token's scope is the admin right."""

    def test_the_scope_opens_the_admin_pages_whatever_the_row_says(self, client, db_session, test_user):
        assert test_user.is_admin is False
        assert client.get("/admin", headers=web_auth_header(sub="testuser", scopes=["admin"])).status_code == 200

    def test_the_stored_flag_opens_nothing(self, client, admin_user):
        assert admin_user.is_admin is True
        assert client.get("/admin", headers=web_auth_header(sub="admin", scopes=["read"])).status_code == 403

    def test_an_admin_by_scope_still_sees_every_cloud_model_on_the_dashboard(self, client, db_session, test_user):
        test_user.can_use_azure = test_user.can_use_bedrock = False
        db_session.add(test_user)
        db_session.commit()
        plain = client.get("/dashboard", headers=web_auth_header(sub="testuser", scopes=["read"])).text
        admin = client.get("/dashboard", headers=web_auth_header(sub="testuser", scopes=["admin"])).text
        assert "azure-gpt-4" not in plain
        assert "azure-gpt-4" in admin

    def test_a_disabled_admin_by_scope_keeps_the_web_ui(self, client, db_session, test_user):
        test_user.is_disabled = True
        db_session.add(test_user)
        db_session.commit()
        assert client.get("/dashboard", headers=web_auth_header(sub="testuser", scopes=["admin"])).status_code == 200
        assert client.get("/dashboard", headers=web_auth_header(sub="testuser", scopes=["read"])).status_code == 403


def test_nothing_on_the_api_path_reads_the_column():
    """A guard against the exemption coming back: the modules an API-key
    request runs through do not mention ``is_admin`` except where
    ``deps._authenticate`` clears it and ``/v1/me`` reports false."""
    from pathlib import Path

    app = Path(__file__).resolve().parent.parent / "app"
    allowed = {
        "core/deps.py": ["    user.is_admin = False"],
        "routers/v1_api.py": ['        "is_admin": False,'],
        "routers/azure_api.py": [],
        "routers/aws_api.py": [],
    }
    for name, expected in allowed.items():
        lines = [
            line for line in (app / name).read_text().splitlines()
            if "is_admin" in line and not line.lstrip().startswith("#")
        ]
        assert lines == expected, name
