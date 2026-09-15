"""Tests for the /setup page (Claude Code installer, external guide card)."""

from __future__ import annotations

from tests.conftest import web_auth_header


class TestSetupPage:
    def test_renders_claude_code_section(self, client, db_session, test_user):
        resp = client.get("/setup", headers=web_auth_header(sub="testuser"))
        assert resp.status_code == 200
        assert 'id="setup-claude-code"' in resp.text
        # The CA-certificate tab was removed; nothing on the page should
        # point users at a cert download any more.
        assert "tab-panel-cert" not in resp.text
        assert "/setup/files/" not in resp.text

    def test_requires_login(self, client, db_session):
        resp = client.get("/setup")
        assert resp.status_code in (401, 403)

    def test_setup_files_route_is_gone(self, client, db_session, test_user):
        resp = client.get(
            "/setup/files/install-cert.bat", headers=web_auth_header(sub="testuser")
        )
        assert resp.status_code == 404
