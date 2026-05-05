"""Tests for user disable / enable flow."""

from __future__ import annotations

from tests.conftest import auth_header, web_auth_header


class TestApiKeyDisabledRejection:
    """Disabled users hit 403 on /v1/* endpoints regardless of valid API key."""

    def test_disabled_user_blocked_on_v1(self, client, db_session, test_user):
        test_user.is_disabled = True
        db_session.add(test_user); db_session.commit()
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test-llm", "messages": []},
            headers=auth_header(),
        )
        assert resp.status_code == 403

    def test_enabled_again_works(self, client, db_session, test_user):
        # Disable then re-enable: should still be 200/4xx (not 403)
        test_user.is_disabled = True
        db_session.add(test_user); db_session.commit()
        resp = client.get("/v1/models", headers=auth_header())
        assert resp.status_code == 403

        test_user.is_disabled = False
        db_session.add(test_user); db_session.commit()
        resp = client.get("/v1/models", headers=auth_header())
        assert resp.status_code == 200


class TestDisabledHTMLRendering:
    """Browser-style requests get a styled HTML page; API clients get JSON."""

    def test_browser_gets_html(self, client, db_session, test_user):
        test_user.is_disabled = True
        db_session.add(test_user); db_session.commit()
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test-llm", "messages": []},
            headers={
                "Authorization": "Bearer sk-testkey123",
                "Accept": "text/html,application/xhtml+xml,*/*",
            },
        )
        assert resp.status_code == 403
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Account Disabled" in resp.text
        assert "Sign Out" in resp.text

    def test_api_client_gets_json(self, client, db_session, test_user):
        test_user.is_disabled = True
        db_session.add(test_user); db_session.commit()
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test-llm", "messages": []},
            headers={
                "Authorization": "Bearer sk-testkey123",
                "Accept": "application/json",
            },
        )
        assert resp.status_code == 403
        assert "application/json" in resp.headers.get("content-type", "")
        assert resp.json() == {"detail": "Account disabled. Contact your administrator."}


class TestAdminToggle:
    """POST /admin/users/{id}/toggle-disable flips the flag."""

    def test_toggle_disable_then_enable(self, client, db_session, admin_user, test_user):
        # Initial state — not disabled
        assert test_user.is_disabled is False

        # Toggle once → disabled
        resp = client.post(
            f"/admin/users/{test_user.id}/toggle-disable",
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db_session.refresh(test_user)
        assert test_user.is_disabled is True

        # Toggle again → enabled
        resp = client.post(
            f"/admin/users/{test_user.id}/toggle-disable",
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(test_user)
        assert test_user.is_disabled is False

    def test_cannot_disable_self(self, client, db_session, admin_user):
        admin_id = admin_user.id
        resp = client.post(
            f"/admin/users/{admin_id}/toggle-disable",
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_non_admin_cannot_toggle(self, client, db_session, test_user):
        another_user_id = test_user.id
        resp = client.post(
            f"/admin/users/{another_user_id}/toggle-disable",
            headers=web_auth_header(sub="testuser", scopes=["read"]),
            follow_redirects=False,
        )
        assert resp.status_code == 403


class TestAzureToggle:
    """POST /admin/users/{id}/toggle-azure flips the flag."""

    def test_toggle_azure(self, client, db_session, admin_user, test_user):
        # test_user fixture defaults to can_use_azure=True
        resp = client.post(
            f"/admin/users/{test_user.id}/toggle-azure",
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(test_user)
        assert test_user.can_use_azure is False

        resp = client.post(
            f"/admin/users/{test_user.id}/toggle-azure",
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
            follow_redirects=False,
        )
        db_session.refresh(test_user)
        assert test_user.can_use_azure is True
