"""Tests for the /setup install-guide page (tabs for each Claude tool)."""

from __future__ import annotations

from tests.conftest import web_auth_header


class TestSetupPage:
    def test_renders_all_tool_tabs(self, client, db_session, test_user):
        resp = client.get("/setup", headers=web_auth_header(sub="testuser"))
        assert resp.status_code == 200
        assert 'id="tab-panel-cert"' in resp.text
        assert 'id="tab-panel-claude-code"' in resp.text
        assert 'id="tab-panel-office"' in resp.text
        assert "Claude in Office" in resp.text

    def test_office_tab_shows_gateway_base_url(self, client, db_session, test_user):
        resp = client.get("/setup", headers=web_auth_header(sub="testuser"))
        assert resp.status_code == 200
        # gateway_base is built from the Host header and shown in the
        # "Connect it to the gateway" step
        assert "/v1" in resp.text

    def test_requires_login(self, client, db_session):
        resp = client.get("/setup")
        assert resp.status_code in (401, 403)
