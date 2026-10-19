"""Combined cloud cap: one daily budget for Azure + Bedrock together.

Semantics under test:
  - ``[app].cloud_daily_limit_usd`` is the default for every user; a user's
    own ``users.cloud_daily_limit_usd`` (0 included) replaces it, and NULL
    follows the default. No value anywhere = no combined cap.
  - Spend on both backends counts against it, so users split it however
    they like; once it is gone, both backends 429 (or fall back on-prem on
    ``/v1/*`` with ``cloud_budget_fallback``) while on-prem keeps working.
  - It is checked before the per-backend sub-limit, so its 429 names it.
  - ``/v1/me`` reports it (never as null) and bounds Azure / AWS
    ``remaining_usd`` by it; the dashboard shows the combined row.
  - Admin: the default via ``POST /admin/cloud-limit``, a user's own value
    via ``POST /admin/users/{id}/cloud-limit`` and the JSON user API.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlmodel import Session

from app.core import config as cfg
from app.core.config import get_cloud_daily_limit
from app.core.deps import effective_cloud_limit
from app.models.schema import User
from tests.conftest import web_auth_header
from tests.test_cloud_budget_fallback import _burn, _chat, _messages, _mock_downstream


def _default(value):
    """Patch the gateway-wide default ([app].cloud_daily_limit_usd)."""
    if value is None:
        return patch.dict("app.core.config.APP_CONFIG", {}, clear=False)
    return patch.dict("app.core.config.APP_CONFIG", {"cloud_daily_limit_usd": value})


def _set_own(db_session: Session, user: User, value: float | None) -> None:
    user.cloud_daily_limit_usd = value
    db_session.add(user)
    db_session.commit()


class TestConfigAndResolution:
    @pytest.mark.parametrize("raw, expected", [
        (30, 30.0), ("12.5", 12.5), (0, 0.0), (-5, 0.0),
        ("", None), ("abc", None), (float("inf"), None), (None, None),
    ])
    def test_default_parsing(self, raw, expected):
        with patch.dict("app.core.config.APP_CONFIG", {"cloud_daily_limit_usd": raw}):
            assert get_cloud_daily_limit() == expected

    def test_absent_means_no_cap(self):
        with patch.dict("app.core.config.APP_CONFIG", {}, clear=True):
            assert get_cloud_daily_limit() is None

    def test_own_value_wins_and_null_follows_default(self, test_user):
        with _default(30):
            assert effective_cloud_limit(test_user) == 30.0
            test_user.cloud_daily_limit_usd = 50.0
            assert effective_cloud_limit(test_user) == 50.0
            test_user.cloud_daily_limit_usd = 0.0
            assert effective_cloud_limit(test_user) == 0.0
        with patch.dict("app.core.config.APP_CONFIG", {}, clear=True):
            test_user.cloud_daily_limit_usd = None
            assert effective_cloud_limit(test_user) is None

    def test_set_and_clear_the_default(self, tmp_path):
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
    def test_split_freely_until_the_combined_cap(self, client, db_session, test_user):
        _mock_downstream(client)
        _burn(db_session, test_user, Decimal("20.00"), "azure")
        with _default(30):
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
        with _default(30):
            assert _chat(client, path="/azure/v1/chat/completions").status_code == 429

    def test_own_value_overrides_the_default(self, client, db_session, test_user):
        _mock_downstream(client)
        _burn(db_session, test_user, Decimal("30.00"), "azure")
        _set_own(db_session, test_user, 50.0)
        with _default(30):
            assert _chat(client).status_code == 200

    def test_own_zero_refuses_cloud_without_a_default(self, client, db_session, test_user):
        _mock_downstream(client)
        _set_own(db_session, test_user, 0.0)
        with patch.dict("app.core.config.APP_CONFIG", {}, clear=True):
            resp = _chat(client)
        assert resp.status_code == 429
        assert "Cloud (Azure + Bedrock) daily spending limit is $0" in resp.json()["detail"]

    def test_no_cap_anywhere_is_unchanged(self, client, db_session, test_user):
        _mock_downstream(client)
        _burn(db_session, test_user, Decimal("60.00"), "azure")
        with patch.dict("app.core.config.APP_CONFIG", {}, clear=True):
            assert _chat(client).status_code == 200

    def test_sub_limit_still_applies_inside_the_cap(self, client, db_session, test_user):
        _mock_downstream(client)
        test_user.azure_daily_limit_usd = 5.0
        db_session.add(test_user)
        db_session.commit()
        _burn(db_session, test_user, Decimal("5.00"), "azure")
        with _default(30):
            resp = _chat(client)
            assert resp.status_code == 429
            assert resp.json()["detail"].startswith("Azure daily spending limit")
            assert _messages(client, model="bedrock-claude").status_code == 200

    def test_fallback_serves_on_prem_when_the_cap_is_spent(self, client, db_session, test_user):
        captured = _mock_downstream(client)
        _burn(db_session, test_user, Decimal("30.00"), "bedrock")
        with _default(30), patch.dict("app.core.config.APP_CONFIG", {"cloud_budget_fallback": True}):
            resp = _chat(client)
        assert resp.status_code == 200
        assert "X-Budget-Fallback" in resp.headers
        assert "/openai/v1/responses" not in captured["url"]


class TestAccountInfo:
    def _me(self, client):
        from tests.conftest import auth_header
        return client.get("/v1/me", headers=auth_header()).json()

    def test_cloud_block_and_bounded_remaining(self, client, db_session, test_user):
        _burn(db_session, test_user, Decimal("20.00"), "azure")
        with _default(30):
            me = self._me(client)
        assert me["cloud"] == {"limit_usd": 30.0, "used_usd": 20.0, "remaining_usd": 10.0}
        assert me["backends"]["azure"]["remaining_usd"] == 10.0
        assert me["backends"]["aws"]["remaining_usd"] == 10.0
        assert me["backends"]["onprem"]["remaining_usd"] == 80.0

    def test_own_value_is_reported(self, client, db_session, test_user):
        _set_own(db_session, test_user, 50.0)
        with _default(30):
            assert self._me(client)["cloud"]["limit_usd"] == 50.0

    def test_no_cloud_key_without_cloud_access(self, client, db_session, test_user):
        """A default cap means nothing to a user who can't reach the cloud."""
        test_user.can_use_azure = test_user.can_use_bedrock = False
        db_session.add(test_user)
        db_session.commit()
        with _default(30):
            assert "cloud" not in self._me(client)

    def test_no_cap_means_no_cloud_key(self, client, db_session, test_user):
        with patch.dict("app.core.config.APP_CONFIG", {}, clear=True):
            me = self._me(client)
        assert "cloud" not in me


class TestDashboard:
    def _body(self, client, user):
        return client.get(
            "/dashboard", headers=web_auth_header(sub=user.username, scopes=["read"]),
        ).text

    def test_combined_row_uses_the_effective_limit(self, client, db_session, test_user):
        _set_own(db_session, test_user, 45.0)
        with _default(30):
            body = self._body(client, test_user)
        assert "Cloud (Azure + AWS)" in body
        assert "/ $45" in body

    def test_no_row_without_cloud_access(self, client, db_session, test_user):
        test_user.can_use_azure = test_user.can_use_bedrock = False
        db_session.add(test_user)
        db_session.commit()
        with _default(30):
            assert "Cloud (Azure + AWS)" not in self._body(client, test_user)


class TestAdmin:
    @staticmethod
    def _hdr(admin_user):
        return web_auth_header(sub=admin_user.username, scopes=["admin"])

    def test_set_and_clear_the_default(self, client, admin_user):
        with patch("app.routers.admin.set_cloud_daily_limit") as mock_set:
            for form, expected in (({"new_cloud_limit": "30"}, 30.0), ({"new_cloud_limit": ""}, None)):
                resp = client.post("/admin/cloud-limit", data=form,
                                   headers=self._hdr(admin_user), follow_redirects=False)
                assert resp.status_code == 303
                assert mock_set.call_args.args == (expected,)

    def test_invalid_default_rejected(self, client, admin_user):
        with patch("app.routers.admin.set_cloud_daily_limit") as mock_set:
            resp = client.post("/admin/cloud-limit", data={"new_cloud_limit": "-1"},
                               headers=self._hdr(admin_user), follow_redirects=False)
        assert resp.status_code == 400
        mock_set.assert_not_called()

    def test_per_user_form_sets_and_clears(self, client, db_session, admin_user, test_user):
        for raw, expected in (("45", 45.0), ("0", 0.0), ("", None)):
            resp = client.post(f"/admin/users/{test_user.id}/cloud-limit",
                               data={"new_cloud_limit": raw},
                               headers=self._hdr(admin_user), follow_redirects=False)
            assert resp.status_code == 303
            db_session.refresh(test_user)
            assert test_user.cloud_daily_limit_usd == expected, raw

    def test_json_patch_and_listing(self, client, db_session, admin_user, test_user):
        resp = client.patch(f"/admin/users/{test_user.id}",
                            json={"cloud_daily_limit_usd": 12.5},
                            headers=self._hdr(admin_user))
        assert resp.status_code == 200, resp.text
        db_session.refresh(test_user)
        assert test_user.cloud_daily_limit_usd == 12.5
        listing = client.get("/admin/users", headers=self._hdr(admin_user)).json()
        row = next(u for u in listing if u["id"] == test_user.id)
        assert row["cloud_daily_limit_usd"] == 12.5

    def test_admin_page_renders_both_fields(self, client, admin_user, test_user):
        with _default(30):
            body = client.get("/admin", headers=self._hdr(admin_user)).text
        assert 'action="/admin/cloud-limit"' in body
        assert f'action="/admin/users/{test_user.id}/cloud-limit"' in body
