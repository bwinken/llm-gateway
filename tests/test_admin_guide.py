"""The admin Guide page (``/admin/guide``): every setting with its reason and
its trade-offs, and the links into it from the cards."""

from __future__ import annotations

import re

from tests.conftest import web_auth_header


def _admin():
    return web_auth_header(sub="admin", scopes=["admin"])


class TestContent:
    def test_every_anchor_is_unique_and_every_entry_says_why(self):
        from app.services import admin_guide

        ids = [s.id for s in admin_guide.SECTIONS] + [e.id for s in admin_guide.SECTIONS for e in s.entries]
        assert len(ids) == len(set(ids))
        for section in admin_guide.SECTIONS:
            assert section.entries and section.href.startswith("/admin")
            for entry in section.entries:
                assert entry.what and entry.why, entry.id
                assert re.fullmatch(r"[a-z0-9-]+", entry.id), entry.id

    def test_every_gateway_wide_limit_has_an_entry(self):
        # A setting added to the Limits tab without its reasons fails here.
        from app.core import config
        from app.services import admin_guide

        keys = {
            config._CONCURRENCY_MODE_KEY, config._CONCURRENCY_LIMIT_KEY, config._CONCURRENCY_CLOUD_LIMIT_KEY,
            config._CONCURRENCY_OFFPEAK_START_KEY, config._CONCURRENCY_OFFPEAK_END_KEY,
            config._CONCURRENCY_OFFPEAK_WEEKENDS_KEY, config._CLOUD_BUDGET_FALLBACK_KEY,
            "default_daily_limit_usd", "cloud_daily_limit_usd",
            config._OVERFLOW_MODE_KEY, config._OVERFLOW_BUDGET_KEY, *config._OVERFLOW_INT_KEYS,
            config._IP_ALLOWLIST_MODE_KEY, config._CLIENT_MONTHLY_MAIL_KEY,
        }
        documented = admin_guide.documented_keys()
        missing = sorted(k for k in keys if f"[app].{k}" not in documented)
        assert missing == []

    def test_the_overflow_defaults_it_states_are_the_real_ones(self):
        from app.core import config
        from app.services import admin_guide

        entries = {e.id: e for s in admin_guide.SECTIONS for e in s.entries}
        d = {k.removeprefix("overflow_"): v[0] for k, v in config._OVERFLOW_INT_KEYS.items()}
        assert entries["overflow-thresholds"].default == f"{d['waiting_high']} / {d['waiting_low']}"
        assert entries["overflow-consecutive"].default == str(d["consecutive"])
        assert entries["overflow-pace"].default == f"{d['grab_max']} / {d['release_per_round']}"
        assert entries["overflow-stay"].default == f"{d['min_stay_min']} minutes"
        assert entries["overflow-idle"].default == f"{d['idle_min']} minutes"
        assert entries["overflow-stop"].default == str(d["budget_stop_pct"])
        assert entries["overflow-budget"].default == f"${config.DEFAULT_OVERFLOW_DAILY_BUDGET_USD:g}"
        assert entries["concurrency-local"].default == str(config.DEFAULT_CONCURRENCY_LIMIT)
        assert entries["concurrency-cloud"].default == str(config.DEFAULT_CONCURRENCY_CLOUD_LIMIT)


class TestPage:
    def test_it_lists_every_entry_with_the_value_in_force(self, client, admin_user):
        from app.services import admin_guide

        resp = client.get("/admin/guide", headers=_admin())
        assert resp.status_code == 200
        body = resp.text
        for anchor in admin_guide.entry_ids():
            assert f'id="{anchor}"' in body, anchor
        assert "now: off" in body  # overflow / concurrency are off in the tests
        assert "Trade-offs" in body and "Decided" in body

    def test_admins_only(self, client, test_user):
        assert client.get("/admin/guide", headers=web_auth_header(sub="testuser")).status_code == 403

    def test_it_opens_with_a_setting_that_cannot_be_read(self, client, admin_user):
        from unittest.mock import patch

        with patch("app.services.overflow.get_overflow_settings", side_effect=RuntimeError("broken config")):
            resp = client.get("/admin/guide", headers=_admin())
        assert resp.status_code == 200 and 'id="overflow-budget"' in resp.text

    def test_it_is_a_tab_of_the_admin_bar_on_all_three_pages(self, client, admin_user):
        for path in ("/admin", "/admin/models", "/admin/guide"):
            body = client.get(path, headers=_admin()).text
            assert 'href="/admin/guide"' in body, path
        guide = client.get("/admin/guide", headers=_admin()).text
        bar = guide[guide.index('id="adminTabs"'):]
        bar = bar[:bar.index("</div>")]
        assert bar.count("gw-tab-on") == 1 and "Guide" in bar[bar.index("gw-tab-on"):]


class TestLinksFromTheCards:
    def test_every_why_link_lands_on_an_entry(self, client, admin_user):
        from app.services import admin_guide

        body = client.get("/admin", headers=_admin()).text
        anchors = set(re.findall(r'href="/admin/guide#([a-z0-9-]+)"', body))
        assert {"concurrency", "concurrency-offpeak", "default-daily-limit", "cloud-daily-limit",
                "cloud-budget-fallback", "overflow"} <= anchors
        assert anchors <= admin_guide.entry_ids()

    def test_the_limits_tab_opens_with_what_is_in_force(self, client, admin_user):
        body = client.get("/admin", headers=_admin()).text
        limits = body[body.index('data-panel="limits"'):body.index('data-panel="settings"')]
        strip = limits[limits.index('id="limits-summary"'):limits.index('id="concurrency-limit"')]
        for label in ("Concurrency", "Off-peak hours", "Daily limit", "Cloud limit", "Budget fallback", "Cloud overflow"):
            assert label in strip, label
        # each figure jumps to a card that exists
        for card in set(re.findall(r"getElementById\('([a-z-]+)'\)", strip)):
            assert f'id="{card}"' in limits, card

    def test_the_overflow_settings_are_grouped_by_question(self, client, admin_user):
        body = client.get("/admin", headers=_admin()).text
        card = body[body.index('id="cloud-overflow"'):body.index('data-panel="settings"')]
        assert card.count("<fieldset") == 3
        for name in ("mode", "daily_budget_usd", "budget_stop_pct", "waiting_high", "waiting_low", "consecutive",
                     "grab_max", "release_per_round", "min_stay_min", "idle_min"):
            assert f'name="{name}"' in card, name
