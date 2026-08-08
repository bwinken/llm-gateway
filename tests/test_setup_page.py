"""Tests for the /setup page (cert + Claude Code tabs, external guide card)."""

from __future__ import annotations

from tests.conftest import web_auth_header


class TestSetupPage:
    def test_renders_tool_tabs(self, client, db_session, test_user):
        resp = client.get("/setup", headers=web_auth_header(sub="testuser"))
        assert resp.status_code == 200
        assert 'id="tab-panel-cert"' in resp.text
        assert 'id="tab-panel-claude-code"' in resp.text

    def test_requires_login(self, client, db_session):
        resp = client.get("/setup")
        assert resp.status_code in (401, 403)
