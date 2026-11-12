"""Tests for the /setup page (Claude Code installer, external guide card)."""

from __future__ import annotations

import pytest

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


class TestSetupPageSwitch:
    """``SETUP_PAGE_ENABLED=false``: a site whose users are not on Windows
    takes the page, its installer and every link to it out."""

    PAGES = ("/", "/dashboard")

    def test_on_by_default(self, client, db_session, test_user):
        headers = web_auth_header(sub="testuser")
        assert client.get("/setup", headers=headers).status_code == 200
        for page in self.PAGES:
            assert 'href="/setup"' in client.get(page, headers=headers).text, page

    @pytest.mark.parametrize("value", ["false", "0", "off", " False "])
    def test_off(self, client, db_session, test_user, monkeypatch, value):
        monkeypatch.setenv("SETUP_PAGE_ENABLED", value)
        headers = web_auth_header(sub="testuser")
        assert client.get("/setup", headers=headers).status_code == 404
        assert client.get("/dashboard/install-claude-code.bat", headers=headers).status_code == 404
        for page in self.PAGES:
            resp = client.get(page, headers=headers)
            assert resp.status_code == 200, page
            assert "/setup" not in resp.text, page
        # The key patch is for every platform and stays.
        assert client.get("/dashboard/api-key/patch.sh", headers=headers).status_code == 200

    def test_anything_else_leaves_it_on(self, client, db_session, test_user, monkeypatch):
        monkeypatch.setenv("SETUP_PAGE_ENABLED", "true")
        assert client.get("/setup", headers=web_auth_header(sub="testuser")).status_code == 200

    def test_every_page_environment_knows_the_switch(self, client, db_session, test_user, admin_user, monkeypatch):
        # base.html asks for it in the navbar. A template environment
        # without the global renders every page of its router as a 500.
        from app import main
        from app.routers import admin, web_ui

        for env in (web_ui.templates.env, admin.templates.env, main._templates.env):
            assert callable(env.globals["setup_page_enabled"])
            assert callable(env.globals["get_site_links"])
        admin_headers = web_auth_header(sub="admin", scopes=["admin"])
        assert 'href="/setup"' in client.get("/admin", headers=admin_headers).text
        monkeypatch.setenv("SETUP_PAGE_ENABLED", "false")
        page = client.get("/admin", headers=admin_headers)
        assert page.status_code == 200
        assert 'href="/setup"' not in page.text

    def test_the_install_guide_moves_to_the_top_bar(self, client, db_session, test_user, monkeypatch):
        # The admin's guide link is a card on the Setup page. With that page
        # off it would be nowhere: a Linux site points it at its own guide.
        from unittest.mock import patch

        guide = "https://wiki.example.com/terminal-tool"
        headers = web_auth_header(sub="testuser")
        with patch.dict("app.core.config.APP_CONFIG", {"install_guide_url": guide}):
            assert guide not in client.get("/dashboard", headers=headers).text
            assert guide in client.get("/setup", headers=headers).text
            monkeypatch.setenv("SETUP_PAGE_ENABLED", "false")
            page = client.get("/dashboard", headers=headers).text
            assert f'href="{guide}"' in page and "Install Guide" in page
        assert "Install Guide" not in client.get("/dashboard", headers=headers).text
