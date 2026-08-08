"""Tests for admin-editable site links (support bot / install guide).

Covers the POST /admin/site-links endpoint and the base.html rendering of
the floating support-bot button + navbar "Install Guide" link.
"""

from __future__ import annotations

from unittest.mock import patch

from tests.conftest import web_auth_header

_LINKS = {
    "support_bot_url": "https://chat.example.com/bot",
    "install_guide_url": "https://wiki.example.com/claude-guide",
}


class TestUpdateSiteLinks:
    """POST /admin/site-links (JWT admin auth)."""

    def test_set_both_urls(self, client, db_session, admin_user):
        with patch("app.routers.admin.set_site_links") as mock_set:
            resp = client.post(
                "/admin/site-links",
                data={
                    "support_bot_url": " https://chat.example.com/bot ",
                    "install_guide_url": "https://wiki.example.com/claude-guide",
                },
                headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
                follow_redirects=False,
            )
        assert resp.status_code == 303
        mock_set.assert_called_once_with(
            support_bot_url="https://chat.example.com/bot",
            install_guide_url="https://wiki.example.com/claude-guide",
        )

    def test_empty_values_allowed(self, client, db_session, admin_user):
        """Empty fields clear the links (hides the UI elements)."""
        with patch("app.routers.admin.set_site_links") as mock_set:
            resp = client.post(
                "/admin/site-links",
                data={"support_bot_url": "", "install_guide_url": ""},
                headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
                follow_redirects=False,
            )
        assert resp.status_code == 303
        mock_set.assert_called_once_with(support_bot_url="", install_guide_url="")

    def test_relative_url_allowed(self, client, db_session, admin_user):
        with patch("app.routers.admin.set_site_links") as mock_set:
            resp = client.post(
                "/admin/site-links",
                data={"support_bot_url": "/setup", "install_guide_url": ""},
                headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
                follow_redirects=False,
            )
        assert resp.status_code == 303
        mock_set.assert_called_once_with(support_bot_url="/setup", install_guide_url="")

    def test_javascript_url_rejected(self, client, db_session, admin_user):
        with patch("app.routers.admin.set_site_links") as mock_set:
            resp = client.post(
                "/admin/site-links",
                data={
                    "support_bot_url": "javascript:alert(1)",
                    "install_guide_url": "",
                },
                headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
                follow_redirects=False,
            )
        assert resp.status_code == 400
        mock_set.assert_not_called()

    def test_non_admin_forbidden(self, client, db_session):
        with patch("app.routers.admin.set_site_links") as mock_set:
            resp = client.post(
                "/admin/site-links",
                data=_LINKS,
                headers=web_auth_header(sub="testuser", scopes=["read"]),
                follow_redirects=False,
            )
        assert resp.status_code == 403
        mock_set.assert_not_called()


class TestSiteLinksRendering:
    """base.html renders the support-bot FAB / Install Guide link only when set."""

    def test_dashboard_shows_links_when_configured(self, client, db_session, test_user):
        with patch.dict("app.core.config.APP_CONFIG", _LINKS):
            resp = client.get("/dashboard", headers=web_auth_header(sub="testuser"))
        assert resp.status_code == 200
        assert 'href="https://chat.example.com/bot"' in resp.text
        assert 'href="https://wiki.example.com/claude-guide"' in resp.text
        assert "Install Guide" in resp.text
        # Both open in a new tab
        assert 'target="_blank"' in resp.text

    def test_dashboard_hides_links_when_unset(self, client, db_session, test_user):
        with patch.dict(
            "app.core.config.APP_CONFIG",
            {"support_bot_url": "", "install_guide_url": ""},
        ):
            resp = client.get("/dashboard", headers=web_auth_header(sub="testuser"))
        assert resp.status_code == 200
        # The FAB anchor is not rendered (".gw-fab" still exists in the CSS)
        assert 'class="gw-fab' not in resp.text
        assert "Install Guide" not in resp.text

    def test_admin_panel_shows_current_values(self, client, db_session, admin_user):
        with patch.dict("app.core.config.APP_CONFIG", _LINKS):
            resp = client.get(
                "/admin",
                headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
            )
        assert resp.status_code == 200
        assert 'value="https://chat.example.com/bot"' in resp.text
        assert 'value="https://wiki.example.com/claude-guide"' in resp.text
