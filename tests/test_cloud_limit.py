"""Cloud cap: one daily budget for Azure + Bedrock together, the same for
everyone.

Semantics under test:
  - ``[app].cloud_daily_limit_usd`` caps each user's Azure + Bedrock spend
    per day. Per user there are only the access flags: no amounts. Absent =
    cloud bounded by the overall daily limit only; 0 refuses cloud.
  - Spend on both backends counts against it, so users split it however
    they like; once it is gone, both backends 429 (or fall back on-prem on
    ``/v1/*`` with ``cloud_budget_fallback``) while on-prem keeps working.
  - The retired per-user sub-limit columns are ignored, and the admin JSON
    API rejects the retired fields rather than silently dropping them.
  - ``/v1/me`` reports the cap (never as null, never to a user without cloud
    access) and bounds Azure / AWS ``remaining_usd`` by it; the dashboard
    shows the combined row.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

from app.core import config as cfg
from app.core.config import get_cloud_daily_limit
from tests.conftest import auth_header, web_auth_header
from tests.test_cloud_budget_fallback import _burn, _chat, _messages, _mock_downstream


def _cap(value):
    """Patch the cloud cap ([app].cloud_daily_limit_usd)."""
    return patch.dict("app.core.config.APP_CONFIG", {"cloud_daily_limit_usd": value})


def _no_cap():
    return patch.dict("app.core.config.APP_CONFIG", {}, clear=True)


class TestConfig:
    @pytest.mark.parametrize("raw, expected", [
        (30, 30.0), ("12.5", 12.5), (0, 0.0), (-5, 0.0),
        ("", None), ("abc", None), (float("inf"), None), (None, None),
    ])
    def test_parsing(self, raw, expected):
        with _cap(raw):
            assert get_cloud_daily_limit() == expected

    def test_absent_means_no_cap(self):
        with _no_cap():
            assert get_cloud_daily_limit() is None

    def test_set_and_clear(self, tmp_path):
        """set_cloud_daily_limit writes [app] and None removes the key."""
        import toml

        path = tmp_path / "config.toml"
        path.write_text('[app]\ndefault_daily_limit_usd = 10.0\n')
        with patch.object(cfg, "_CONFIG_PATH", path), patch.object(cfg, "reload_config"):
            cfg.set_cloud_daily_limit(30.0)
            assert toml.load(path)["app"]["cloud_daily_limit_usd"] == 30.0
            cfg.set_cloud_daily_limit(None)
            assert "cloud_daily_limit_usd" not in toml.load(path)["app"]
            assert toml.load(path)["app"]["default_daily_limit_usd"] == 10.0


class TestEnforcement:
    def test_split_freely_until_the_cap(self, client, db_session, test_user):
        _mock_downstream(client)
        _burn(db_session, test_user, Decimal("20.00"), "azure")
        with _cap(30):
            # $20 spent on Azure: Bedrock may still take the remaining $10.
            assert _messages(client, model="bedrock-claude").status_code == 200
            _burn(db_session, test_user, Decimal("10.00"), "bedrock")
            for resp in (_chat(client), _messages(client, model="bedrock-claude")):
                assert resp.status_code == 429
                assert "Cloud (Azure + Bedrock) daily spending limit ($30.0)" in resp.json()["detail"]
                assert "Retry-After" in resp.headers
            # On-prem is untouched by the cloud cap.
            assert _chat(client, model="test-llm").status_code == 200

    def test_dedicated_surfaces_are_capped_too(self, client, db_session, test_user):
        _mock_downstream(client)
        _burn(db_session, test_user, Decimal("30.00"), "bedrock")
        with _cap(30):
            assert _chat(client, path="/azure/v1/chat/completions").status_code == 429
            assert _messages(client, model="bedrock-claude", path="/aws/v1/messages").status_code == 429

    def test_zero_refuses_cloud(self, client, db_session, test_user):
        _mock_downstream(client)
        with _cap(0):
            resp = _chat(client)
        assert resp.status_code == 429
        assert resp.json()["detail"] == "Cloud (Azure + Bedrock) daily spending limit is $0."

    def test_no_cap_leaves_cloud_to_the_daily_limit(self, client, db_session, test_user):
        _mock_downstream(client)
        _burn(db_session, test_user, Decimal("60.00"), "azure")
        with _no_cap():
            assert _chat(client).status_code == 200

    def test_retired_sub_limits_are_ignored(self, client, db_session, test_user):
        """A value left in the retired columns no longer caps anything."""
        _mock_downstream(client)
        test_user.azure_daily_limit_usd = 1.0
        test_user.bedrock_daily_limit_usd = 0.0
        db_session.add(test_user)
        db_session.commit()
        _burn(db_session, test_user, Decimal("5.00"), "azure")
        with _cap(30):
            assert _chat(client).status_code == 200
            assert _messages(client, model="bedrock-claude").status_code == 200

    def test_fallback_serves_on_prem_when_the_cap_is_spent(self, client, db_session, test_user):
        captured = _mock_downstream(client)
        _burn(db_session, test_user, Decimal("30.00"), "bedrock")
        with patch.dict("app.core.config.APP_CONFIG",
                        {"cloud_daily_limit_usd": 30, "cloud_budget_fallback": True}):
            resp = _chat(client)
        assert resp.status_code == 200
        assert "X-Budget-Fallback" in resp.headers
        assert "/openai/v1/responses" not in captured["url"]


class TestAccountInfo:
    def _me(self, client):
        return client.get("/v1/me", headers=auth_header()).json()

    def test_cloud_block_and_bounded_remaining(self, client, db_session, test_user):
        _burn(db_session, test_user, Decimal("20.00"), "azure")
        with _cap(30):
            me = self._me(client)
        assert me["cloud"] == {"limit_usd": 30.0, "used_usd": 20.0, "remaining_usd": 10.0}
        assert me["backends"]["azure"] == {"access": True, "used_usd": 20.0, "remaining_usd": 10.0}
        assert me["backends"]["aws"]["remaining_usd"] == 10.0
        assert me["backends"]["onprem"]["remaining_usd"] == 80.0

    def test_no_cloud_block_without_cloud_access(self, client, db_session, test_user):
        """The cap means nothing to a user who can't reach the cloud."""
        test_user.can_use_azure = test_user.can_use_bedrock = False
        db_session.add(test_user)
        db_session.commit()
        with _cap(30):
            assert "cloud" not in self._me(client)

    def test_no_cap_means_no_cloud_block(self, client, db_session, test_user):
        with _no_cap():
            me = self._me(client)
        assert "cloud" not in me
        assert me["backends"]["azure"]["remaining_usd"] == me["daily"]["remaining_usd"]


class TestDashboard:
    def _body(self, client, user):
        return client.get(
            "/dashboard", headers=web_auth_header(sub=user.username, scopes=["read"]),
        ).text

    def test_cloud_row_shows_the_cap(self, client, db_session, test_user):
        with _cap(30):
            body = self._body(client, test_user)
        assert "Cloud (Azure + AWS)" in body
        assert "/ $30" in body

    def test_no_cloud_row_without_cloud_access(self, client, db_session, test_user):
        test_user.can_use_azure = test_user.can_use_bedrock = False
        db_session.add(test_user)
        db_session.commit()
        with _cap(30):
            assert "Cloud (Azure + AWS)" not in self._body(client, test_user)


class TestAdmin:
    @staticmethod
    def _hdr(admin_user):
        return web_auth_header(sub=admin_user.username, scopes=["admin"])

    def test_set_and_clear_the_cap(self, client, admin_user):
        with patch("app.routers.admin.set_cloud_daily_limit") as mock_set:
            for form, expected in (({"new_cloud_limit": "30"}, 30.0), ({"new_cloud_limit": ""}, None)):
                resp = client.post("/admin/cloud-limit", data=form,
                                   headers=self._hdr(admin_user), follow_redirects=False)
                assert resp.status_code == 303
                assert mock_set.call_args.args == (expected,)

    def test_invalid_cap_rejected(self, client, admin_user):
        with patch("app.routers.admin.set_cloud_daily_limit") as mock_set:
            resp = client.post("/admin/cloud-limit", data={"new_cloud_limit": "-1"},
                               headers=self._hdr(admin_user), follow_redirects=False)
        assert resp.status_code == 400
        mock_set.assert_not_called()

    @pytest.mark.parametrize("field", [
        "azure_daily_limit_usd", "bedrock_daily_limit_usd", "cloud_daily_limit_usd",
    ])
    def test_retired_per_user_fields_rejected(self, client, admin_user, test_user, field):
        resp = client.patch(f"/admin/users/{test_user.id}", json={field: 5.0},
                            headers=self._hdr(admin_user))
        assert resp.status_code == 400
        assert "no longer exist" in resp.json()["detail"]
        resp = client.post("/admin/users", json={"username": "app_new", field: 5.0},
                           headers=self._hdr(admin_user))
        assert resp.status_code == 400

    def test_per_user_amount_endpoints_are_gone(self, client, admin_user, test_user):
        for kind in ("azure", "bedrock", "cloud"):
            resp = client.post(f"/admin/users/{test_user.id}/{kind}-limit",
                               data={f"new_{kind}_limit": "5"},
                               headers=self._hdr(admin_user), follow_redirects=False)
            assert resp.status_code in (404, 405), kind

    def test_admin_page_has_only_the_global_cap(self, client, admin_user, test_user):
        with _cap(30):
            body = client.get("/admin", headers=self._hdr(admin_user)).text
        assert 'action="/admin/cloud-limit"' in body
        for kind in ("azure", "bedrock", "cloud"):
            assert f'/admin/users/{test_user.id}/{kind}-limit"' not in body
        assert f'/admin/users/{test_user.id}/toggle-azure"' in body
        assert f'/admin/users/{test_user.id}/toggle-bedrock"' in body
