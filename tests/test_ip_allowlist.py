"""API IP allowlist (app/services/ip_allowlist.py): the check on the API
routers, rule windows and the admin card (admins only: users have no self-service)."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlmodel import Session, select

from app.models.schema import IpAllowRule, User
from app.services import ip_allowlist
from tests.conftest import auth_header, web_auth_header

# app.routers.* are imported inside the tests, never at module level:
# conftest's _build_test_app imports the routers under its config patches.

OUTSIDE = "198.51.100.7"
PARTNER = "203.0.113.5"


def _settings(mode: str = "enforce"):
    return patch("app.services.ip_allowlist.get_ip_allowlist_settings", return_value={"mode": mode})


def _rule(session: Session, cidr: str, **kw) -> IpAllowRule:
    rule = ip_allowlist.add_rule(session, cidr, created_by="admin", **kw)
    ip_allowlist.refresh()
    return rule


@pytest.fixture()
def client_at(db_session, test_user):
    """``client_at("203.0.113.5")``: a TestClient whose requests come from
    that address (``request.client.host``, as uvicorn's proxy-header
    middleware leaves it behind nginx)."""
    from fastapi.testclient import TestClient

    from app.core.database import get_session
    from tests.conftest import _build_test_app

    app = _build_test_app()

    def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    with ExitStack() as stack:
        def make(ip: str):
            return stack.enter_context(TestClient(app, raise_server_exceptions=False, client=(ip, 50000)))
        yield make


# ── Addresses and rules ──

class TestNormalize:
    @pytest.mark.parametrize("raw, expected", [
        ("203.0.113.5", "203.0.113.5/32"),
        (" 10.1.2.3/8 ", "10.0.0.0/8"),
        ("2001:db8::1", "2001:db8::1/128"),
        ("2001:db8::/32", "2001:db8::/32"),
        ("::ffff:203.0.113.5", "203.0.113.5/32"),
        ("::ffff:10.0.0.0/104", "10.0.0.0/8"),
    ])
    def test_normalizes(self, raw, expected):
        assert ip_allowlist.normalize_cidr(raw) == expected

    @pytest.mark.parametrize("raw", ["", "partner.example.com", "10.0.0.0/33", "0.0.0.0/0", "::/0"])
    def test_rejects(self, raw):
        with pytest.raises(ValueError):
            ip_allowlist.normalize_cidr(raw)

    def test_mapped_client_address_matches_ipv4_rules(self):
        assert str(ip_allowlist.parse_address("::ffff:203.0.113.5")) == PARTNER
        assert ip_allowlist.parse_address("testclient") is None


class TestWindows:
    def test_permanent_scheduled_and_expired(self, db_session):
        now = ip_allowlist.utcnow()
        _rule(db_session, "10.0.0.0/8")
        _rule(db_session, PARTNER, starts_at=now + timedelta(hours=1), expires_at=now + timedelta(hours=3))
        addr = ip_allowlist.parse_address
        assert ip_allowlist.is_allowed(addr("10.20.30.40"), now)
        assert ip_allowlist.is_allowed(addr("10.20.30.40"), now + timedelta(days=3650))
        assert not ip_allowlist.is_allowed(addr(PARTNER), now)
        assert ip_allowlist.is_allowed(addr(PARTNER), now + timedelta(hours=2))
        # The end is exact, whatever the snapshot's age.
        assert not ip_allowlist.is_allowed(addr(PARTNER), now + timedelta(hours=3))
        assert not ip_allowlist.is_allowed(addr(OUTSIDE), now)
        assert not ip_allowlist.is_allowed(None, now)

    def test_bad_windows_are_refused(self, db_session):
        now = ip_allowlist.utcnow()
        with pytest.raises(ValueError, match="past"):
            ip_allowlist.add_rule(db_session, PARTNER, created_by="a", expires_at=now - timedelta(minutes=1), now=now)
        with pytest.raises(ValueError, match="after the start"):
            ip_allowlist.add_rule(
                db_session, PARTNER, created_by="a",
                starts_at=now + timedelta(hours=2), expires_at=now + timedelta(hours=1), now=now,
            )

    def test_failed_refresh_keeps_the_previous_snapshot(self, db_session):
        _rule(db_session, PARTNER)
        with patch("app.services.ip_allowlist.Session", side_effect=RuntimeError("db down")):
            assert ip_allowlist.refresh() is False
        assert ip_allowlist.is_allowed(ip_allowlist.parse_address(PARTNER))

    def test_expired_rules_are_not_loaded(self, db_session):
        now = ip_allowlist.utcnow()
        db_session.add(IpAllowRule(cidr="192.0.2.0/24", created_by="a", created_at=now - timedelta(days=2),
                                   expires_at=now - timedelta(days=1)))
        db_session.commit()
        ip_allowlist.refresh()
        assert ip_allowlist._snapshot == ()


# ── The check on the API routers ──

class TestApiCheck:
    def test_off_by_default(self, client_at):
        assert client_at(OUTSIDE).get("/v1/models", headers=auth_header()).status_code == 200

    def test_enforce_refuses_unlisted_addresses_before_auth(self, client_at, db_session):
        with _settings("enforce"):
            r = client_at(OUTSIDE).get("/v1/models")  # no API key at all: 403, not 401
        assert r.status_code == 403
        assert OUTSIDE in r.json()["detail"]

    @pytest.mark.parametrize("path", ["/v1/models", "/models", "/azure/v1/models", "/aws/v1/models", "/v1/me"])
    def test_every_api_router_is_gated(self, client_at, path):
        with _settings("enforce"):
            assert client_at(OUTSIDE).get(path, headers=auth_header()).status_code == 403

    def test_enforce_admits_listed_addresses(self, client_at, db_session):
        _rule(db_session, "203.0.113.0/24")
        with _settings("enforce"):
            assert client_at(PARTNER).get("/v1/models", headers=auth_header()).status_code == 200
            assert client_at("::ffff:203.0.113.9").get("/v1/models", headers=auth_header()).status_code == 200
            assert client_at(OUTSIDE).get("/v1/models", headers=auth_header()).status_code == 403

    def test_end_time_is_exact_without_a_refresh(self, client_at, db_session):
        now = ip_allowlist.utcnow()
        _rule(db_session, PARTNER, expires_at=now + timedelta(hours=1))
        api = client_at(PARTNER)
        with _settings("enforce"):
            assert api.get("/v1/models", headers=auth_header()).status_code == 200
            with patch("app.services.ip_allowlist.utcnow", return_value=now + timedelta(hours=1)):
                assert api.get("/v1/models", headers=auth_header()).status_code == 403

    def test_ending_a_rule_early_applies_after_the_refresh(self, client_at, db_session):
        rule = _rule(db_session, PARTNER)
        api = client_at(PARTNER)
        with _settings("enforce"):
            ip_allowlist.end_rule(db_session, rule)
            # The snapshot still holds it as permanent until this worker reloads
            # (the admin / dashboard endpoints reload at once; others within REFRESH_S).
            assert api.get("/v1/models", headers=auth_header()).status_code == 200
            ip_allowlist.refresh()
            assert api.get("/v1/models", headers=auth_header()).status_code == 403

    def test_monitor_serves_and_logs(self, client_at):
        with _settings("monitor"), patch("app.services.ip_allowlist.logger") as log:
            assert client_at(OUTSIDE).get("/v1/models", headers=auth_header()).status_code == 200
            assert client_at(OUTSIDE).get("/v1/models", headers=auth_header()).status_code == 200
        lines = [c.args[0] for c in log.warning.call_args_list]
        assert lines == ["IP not on allowlist ({}) | ip={} path={}{}"]  # once per address per minute

    @pytest.mark.parametrize("path", ["/healthz", "/dashboard", "/setup"])
    def test_web_ui_and_health_are_never_gated(self, client_at, path):
        with _settings("enforce"):
            r = client_at(OUTSIDE).get(path, headers=web_auth_header())
        assert r.status_code == 200


# ── Admin card ──

class TestAdmin:
    def _admin(self, client_at, admin_user, ip=PARTNER):
        api = client_at(ip)
        headers = {**web_auth_header(sub=admin_user.username, scopes=["admin"]), "Accept": "application/json"}
        return api, headers

    def test_add_rule_converts_local_time_and_takes_effect(self, client_at, admin_user, db_session):
        api, headers = self._admin(client_at, admin_user)
        r = api.post("/admin/ip-allowlist/rules", headers=headers, data={
            "cidr": "203.0.113.0/24", "label": "Partner office",
            "starts_at": "", "expires_at": "2099-01-01T09:00",
        })
        assert r.status_code == 200, r.text
        rule = db_session.exec(select(IpAllowRule)).one()
        assert (rule.cidr, rule.label, rule.created_by) == ("203.0.113.0/24", "Partner office", "admin")
        assert rule.expires_at.isoformat() == "2099-01-01T01:00:00"  # 09:00 Asia/Taipei
        assert ip_allowlist.is_allowed(ip_allowlist.parse_address(PARTNER))

    def test_add_rule_rejects_bad_input(self, client_at, admin_user):
        api, headers = self._admin(client_at, admin_user)
        for data in (
            {"cidr": "partner.example.com"},
            {"cidr": PARTNER, "expires_at": "2000-01-01T00:00"},
            {"cidr": PARTNER, "expires_at": "tomorrow"},
        ):
            r = api.post("/admin/ip-allowlist/rules", headers=headers, data=data)
            assert r.status_code == 400, data

    def test_end_keeps_a_started_rule_and_deletes_a_scheduled_one(self, client_at, admin_user, db_session):
        now = ip_allowlist.utcnow()
        started = _rule(db_session, PARTNER)
        scheduled = _rule(db_session, OUTSIDE, starts_at=now + timedelta(days=1))
        started_id, scheduled_id = started.id, scheduled.id
        api, headers = self._admin(client_at, admin_user)
        assert api.post(f"/admin/ip-allowlist/rules/{started_id}/end", headers=headers).status_code == 200
        assert api.post(f"/admin/ip-allowlist/rules/{scheduled_id}/end", headers=headers).status_code == 200
        db_session.expire_all()
        assert db_session.get(IpAllowRule, started_id).expires_at <= ip_allowlist.utcnow()
        assert db_session.get(IpAllowRule, scheduled_id) is None
        assert not ip_allowlist.is_allowed(ip_allowlist.parse_address(PARTNER))

    def test_settings_save_the_mode(self, client_at, admin_user):
        api, headers = self._admin(client_at, admin_user)
        with patch("app.routers.admin.set_ip_allowlist_settings") as save:
            assert api.post("/admin/ip-allowlist", headers=headers, data={"mode": "monitor"}).status_code == 200
            assert api.post("/admin/ip-allowlist", headers=headers, data={"mode": "block"}).status_code == 400
        save.assert_called_once_with("monitor")

    def test_admin_page_renders_the_card_while_enforcing(self, client_at, admin_user, db_session):
        _rule(db_session, "10.0.0.0/8", label="HQ")
        api, headers = self._admin(client_at, admin_user, ip=OUTSIDE)
        with _settings("enforce"):
            r = api.get("/admin", headers={"Authorization": headers["Authorization"]})
        assert r.status_code == 200
        assert "IP Allowlist" in r.text and "10.0.0.0/8" in r.text and "not on the allowlist" in r.text


# ── Users have no way to open access themselves ──

class TestNoUserSelfService:
    def test_dashboard_offers_nothing(self, client_at):
        with _settings("enforce"):
            page = client_at(OUTSIDE).get("/dashboard", headers=web_auth_header())
        assert page.status_code == 200
        assert OUTSIDE not in page.text  # the dashboard does not even show the address

    def test_there_is_no_endpoint_for_it(self, client_at, test_user, admin_user):
        api = client_at(OUTSIDE)
        with _settings("enforce"):
            r = api.post("/dashboard/ip-access", headers=web_auth_header(), data={"hours": "1"})
            assert r.status_code == 404
            r = api.post(f"/admin/users/{test_user.id}/toggle-ip-self-service",
                         headers=web_auth_header(sub=admin_user.username, scopes=["admin"]))
            assert r.status_code == 404


# ── Special access overview and filters (admin Users tab) ──

class TestSpecialAccess:
    def _seed(self, db_session):
        rows = [
            User(username="alice", api_key="k1", can_use_bedrock=True),
            User(username="bob", api_key="k2", can_use_azure=True, cloud_daily_limit_usd=5.0),
            User(username="carol", api_key="k3", concurrency_waived=True),
            User(username="dave", api_key="k4", is_disabled=True),
            User(username="plain", api_key="k5"),
            User(username="app_batch", api_key="k6", concurrency_waived=True),   # the app default
            User(username="app_limited", api_key="k7", concurrency_waived=False, can_use_bedrock=True),
        ]
        for u in rows:
            db_session.add(u)
        db_session.commit()

    def test_overview_counts_users_and_apps_separately(self, client, db_session, admin_user):
        from app.routers.admin import _special_access_summary

        self._seed(db_session)
        summary = {s["key"]: (s["users"], s["apps"]) for s in _special_access_summary(db_session)}
        # admin_user (is_admin) and the conftest test_user (Azure + AWS) are users too.
        assert summary["admin"] == (1, 0)
        assert summary["azure"] == (2, 0)
        assert summary["bedrock"] == (2, 1)
        assert summary["cloud_cap"] == (1, 0)
        assert summary["waiver_changed"] == (1, 1)  # carol waived; app_limited un-waived
        assert summary["disabled"] == (1, 0)
        # Any special access: admin, testuser, alice, bob, carol / app_limited. Not dave, plain, app_batch.
        assert summary["special"] == (5, 1)

    def test_filters_list_the_accounts(self, client, db_session, admin_user):
        self._seed(db_session)
        headers = web_auth_header(sub=admin_user.username, scopes=["admin"])
        everyone = ("admin", "testuser", "alice", "bob", "carol", "dave", "plain", "app_batch", "app_limited")

        def names(kind, status):
            body = client.get("/admin/user-table", params={"kind": kind, "status": status}, headers=headers).text
            return sorted(n for n in everyone if f'data-username="{n}"' in body)

        assert names("users", "bedrock") == ["alice", "testuser"]
        assert names("users", "special") == ["admin", "alice", "bob", "carol", "testuser"]
        assert names("apps", "special") == ["app_limited"]
        assert names("apps", "waiver_changed") == ["app_limited"]
        assert names("users", "cloud_cap") == ["bob"]

    def test_admin_page_shows_the_overview(self, client, db_session, admin_user):
        self._seed(db_session)
        body = client.get("/admin", params={"status": "cloud_cap"},
                          headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        assert "Special access" in body
        assert 'href="/admin?status=cloud_cap#users" class="sa-chip  is-active"' in body
        assert 'data-username="bob"' in body and 'data-username="alice"' not in body
