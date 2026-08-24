"""Admin surfaces must not hand out usable credentials in bulk.

A single admin page load used to embed every user's and every app account's
API key in the HTML (`data-apikey="..."` per row), and `GET /admin/users`
returned them all as JSON. Both now carry masked keys; the real key is
fetched one account at a time.
"""

from __future__ import annotations

from app.models.schema import User, mask_api_key
from tests.conftest import web_auth_header


class TestMaskApiKey:
    def test_keeps_prefix_and_last_four(self):
        masked = mask_api_key("sk-internal-1700000000-abcd1234")
        assert masked.startswith("sk-internal")
        assert masked.endswith("1234")
        assert "1700000000" not in masked

    def test_short_and_empty_keys_reveal_nothing(self):
        assert mask_api_key("sk-short") == "…"
        assert mask_api_key("") == ""


def _make_admin(db_session) -> User:
    admin = User(username="adminuser", api_key="sk-internal-1-adminkey", is_admin=True)
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    return admin


class TestBulkSurfacesAreMasked:
    def test_admin_page_html_has_no_raw_keys(self, client, db_session, test_user):
        _make_admin(db_session)
        resp = client.get("/admin", headers=web_auth_header(sub="adminuser", scopes=["read", "admin"]))
        assert resp.status_code == 200
        assert test_user.api_key not in resp.text
        assert "data-apikey" not in resp.text

    def test_users_api_returns_masked_keys(self, client, db_session, test_user):
        _make_admin(db_session)
        resp = client.get("/admin/users", headers=web_auth_header(sub="adminuser", scopes=["read", "admin"]))
        assert resp.status_code == 200
        keys = [row["api_key"] for row in resp.json()]
        assert test_user.api_key not in keys
        assert mask_api_key(test_user.api_key) in keys


class TestRevealEndpoint:
    def test_returns_the_full_key_for_one_account(self, client, db_session, test_user):
        _make_admin(db_session)
        resp = client.get(
            f"/admin/users/{test_user.id}/api-key",
            headers=web_auth_header(sub="adminuser", scopes=["read", "admin"]),
        )
        assert resp.status_code == 200
        assert resp.json()["api_key"] == test_user.api_key

    def test_404_for_unknown_account(self, client, db_session):
        _make_admin(db_session)
        resp = client.get(
            "/admin/users/999999/api-key", headers=web_auth_header(sub="adminuser", scopes=["read", "admin"])
        )
        assert resp.status_code == 404

    def test_requires_admin_scope(self, client, db_session, test_user):
        """A read-scope session must not reach it — same gate as the rest of /admin."""
        resp = client.get(
            f"/admin/users/{test_user.id}/api-key",
            headers=web_auth_header(sub="testuser", scopes=["read"]),
        )
        assert resp.status_code in (401, 403)
