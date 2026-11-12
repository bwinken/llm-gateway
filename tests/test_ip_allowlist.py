"""API IP allowlist (app/services/ip_allowlist.py): rules of address ×
account × window, the check inside API-key auth, and the admin card.
Admins only: users have no way to open access themselves."""

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

OFFICE = "10.20.30.40"      # network A: everyone
PARTNER = "203.0.113.5"     # network B: some people, for a while
LAB = "198.51.100.23"       # network C: another group
OUTSIDE = "192.0.2.99"      # no rule at all

addr = ip_allowlist.parse_address


def _settings(mode: str = "enforce"):
    return patch("app.services.ip_allowlist.get_ip_allowlist_settings", return_value={"mode": mode})


def _rule(session: Session, cidr: str, **kw) -> IpAllowRule:
    rule = ip_allowlist.add_rule(session, cidr, created_by="admin", **kw)
    ip_allowlist.refresh()
    return rule


def _account(session: Session, username: str, key: str, org_code: str = "") -> User:
    user = User(username=username, api_key=key, org_code=org_code, daily_limit_usd=100.0)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


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


def _status(client_at, ip: str, key: str = "sk-testkey123", path: str = "/v1/models") -> int:
    return client_at(ip).get(path, headers=auth_header(key)).status_code


# ── Addresses ──

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
        assert str(addr("::ffff:203.0.113.5")) == PARTNER
        assert addr("testclient") is None


# ── Rules: address × account × window ──

class TestRules:
    def test_the_three_networks(self, db_session):
        """A for everyone, B for two people until a time, C for a department."""
        now = ip_allowlist.utcnow()
        _account(db_session, "alice", "k-a")
        _account(db_session, "bob", "k-b")
        _account(db_session, "carol", "k-c", org_code="RD-7")
        _account(db_session, "dave", "k-d", org_code="QA-2")
        _rule(db_session, "10.0.0.0/8", label="A")
        _rule(db_session, "203.0.113.0/24", label="B", usernames=["alice", "bob"], expires_at=now + timedelta(hours=8))
        _rule(db_session, "198.51.100.0/24", label="C", org_codes=["rd-7"])

        allowed = ip_allowlist.is_allowed
        for user, org in (("alice", ""), ("bob", ""), ("carol", "RD-7"), ("dave", "QA-2")):
            assert allowed(addr(OFFICE), user, org, now)            # A: everyone
        assert allowed(addr(PARTNER), "alice", "", now)
        assert allowed(addr(PARTNER), "bob", "", now)
        assert not allowed(addr(PARTNER), "carol", "RD-7", now)    # B: not carol
        assert not allowed(addr(PARTNER), "alice", "", now + timedelta(hours=8))  # B has ended
        assert allowed(addr(LAB), "carol", "RD-7", now)            # C: her department
        assert allowed(addr(LAB), "carol", "rd-7", now)            # org codes compare case-insensitively
        assert not allowed(addr(LAB), "dave", "QA-2", now)
        assert not allowed(addr(LAB), "alice", "", now)
        assert ip_allowlist.verdict(addr(LAB), "alice", "", now) == "not_for_this_account"
        assert ip_allowlist.verdict(addr(OUTSIDE), "alice", "", now) == "no_rule_for_address"

    def test_one_range_different_people_different_windows(self, db_session):
        """Rules on the same range add up, each with its own people and window."""
        now = ip_allowlist.utcnow()
        for name in ("alice", "bob", "dave"):
            _account(db_session, name, f"k-{name}")
        _rule(db_session, "203.0.113.0/24", usernames=["alice", "bob"], expires_at=now + timedelta(days=4))
        _rule(db_session, "203.0.113.0/24", usernames=["dave"],
              starts_at=now + timedelta(days=7), expires_at=now + timedelta(days=7, hours=9))
        _rule(db_session, "203.0.113.0/24", usernames=["bob"],
              starts_at=now + timedelta(days=7), expires_at=now + timedelta(days=14))

        def ok(user, when):
            return ip_allowlist.is_allowed(addr(PARTNER), user, "", now + when)

        assert ok("alice", timedelta(0)) and ok("bob", timedelta(0)) and not ok("dave", timedelta(0))
        assert not ok("alice", timedelta(days=5)) and not ok("bob", timedelta(days=5))   # first window over
        assert ok("dave", timedelta(days=7, hours=1)) and ok("bob", timedelta(days=7, hours=1))
        assert not ok("alice", timedelta(days=7, hours=1))
        assert not ok("dave", timedelta(days=7, hours=10)) and ok("bob", timedelta(days=7, hours=10))

    def test_rules_of_one_range_are_listed_together(self, db_session):
        now = ip_allowlist.utcnow()
        _rule(db_session, "203.0.113.0/24")
        _rule(db_session, "10.0.0.0/8")
        _rule(db_session, "203.0.113.0/24", starts_at=now - timedelta(hours=1))
        _rule(db_session, "9.0.0.0/8")
        assert [r.cidr for r in ip_allowlist.list_rules(db_session, now)["active"]] == [
            "9.0.0.0/8", "10.0.0.0/8", "203.0.113.0/24", "203.0.113.0/24",
        ]

    def test_accounts_and_org_codes_combine(self, db_session):
        _account(db_session, "alice", "k-a")
        _account(db_session, "carol", "k-c", org_code="RD-7")
        _rule(db_session, "198.51.100.0/24", usernames=["alice"], org_codes=["RD-7"])
        assert ip_allowlist.is_allowed(addr(LAB), "alice", "")
        assert ip_allowlist.is_allowed(addr(LAB), "carol", "RD-7")
        assert not ip_allowlist.is_allowed(addr(LAB), "someone", "")

    def test_scheduled_window(self, db_session):
        now = ip_allowlist.utcnow()
        _rule(db_session, PARTNER, starts_at=now + timedelta(hours=1), expires_at=now + timedelta(hours=3))
        assert not ip_allowlist.is_allowed(addr(PARTNER), "x", "", now)
        assert ip_allowlist.is_allowed(addr(PARTNER), "x", "", now + timedelta(hours=2))
        assert not ip_allowlist.is_allowed(addr(PARTNER), "x", "", now + timedelta(hours=3))

    def test_unknown_accounts_and_org_codes_are_refused(self, db_session):
        _account(db_session, "alice", "k-a", org_code="RD-7")
        with pytest.raises(ValueError, match="nobody"):
            ip_allowlist.add_rule(db_session, PARTNER, created_by="a", usernames=["alice", "nobody"])
        with pytest.raises(ValueError, match="XX-9"):
            ip_allowlist.add_rule(db_session, PARTNER, created_by="a", org_codes=["XX-9"])
        rule = ip_allowlist.add_rule(db_session, PARTNER, created_by="a", org_codes=["rd-7"])
        assert rule.org_codes == ["RD-7"]  # spelled as the accounts carry it

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
        assert ip_allowlist.is_allowed(addr(PARTNER), "anyone")

    def test_expired_rules_are_not_loaded(self, db_session):
        now = ip_allowlist.utcnow()
        db_session.add(IpAllowRule(cidr="192.0.2.0/24", created_by="a", created_at=now - timedelta(days=2),
                                   expires_at=now - timedelta(days=1)))
        db_session.commit()
        ip_allowlist.refresh()
        assert ip_allowlist._snapshot == ()


# ── The check inside API-key auth ──

class TestEveryApiRouteIsChecked:
    def test_every_api_route_authenticates_through_the_checked_dependencies(self):
        """The allowlist runs inside deps._authenticate. An API route that
        authenticated some other way would silently skip it."""
        from app.core import deps
        from app.routers import aws_api, azure_api, v1_api

        checked = {deps.get_current_user, deps.get_current_user_unmetered}

        def walk(dependant) -> bool:
            return dependant.call in checked or any(walk(d) for d in dependant.dependencies)

        routes = [
            route
            for router in (v1_api.router, azure_api.router, aws_api.router)
            for route in router.routes
        ]
        missing = [(route.path, sorted(route.methods)) for route in routes if not walk(route.dependant)]
        assert missing == []
        # Guard against a vacuous pass if FastAPI ever stops listing them here.
        assert {"/v1/chat/completions", "/azure/v1/messages", "/aws/v1/messages"} <= {r.path for r in routes}


class TestApiCheck:
    def test_off_by_default(self, client_at):
        assert _status(client_at, OUTSIDE) == 200

    def test_enforce_refuses_and_says_why(self, client_at):
        with _settings("enforce"):
            r = client_at(OUTSIDE).get("/v1/models", headers=auth_header())
        assert r.status_code == 403
        assert OUTSIDE in r.json()["detail"]

    def test_a_bad_key_is_still_a_401(self, client_at):
        with _settings("enforce"):
            assert client_at(OUTSIDE).get("/v1/models").status_code == 401
            assert _status(client_at, OUTSIDE, key="sk-wrong") == 401

    @pytest.mark.parametrize("path", [
        "/v1/models", "/models", "/azure/v1/models", "/aws/v1/models",
        "/v1/me", "/v1/status", "/v1/usage",
    ])
    def test_every_api_router_is_gated(self, client_at, path):
        with _settings("enforce"):
            assert _status(client_at, OUTSIDE, path=path) == 403

    def test_rules_for_some_accounts(self, client_at, db_session):
        _account(db_session, "alice", "sk-alice")
        _rule(db_session, "10.0.0.0/8")
        _rule(db_session, "203.0.113.0/24", usernames=["alice"])
        with _settings("enforce"):
            assert _status(client_at, OFFICE) == 200                    # A: testuser too
            assert _status(client_at, PARTNER, key="sk-alice") == 200   # B: alice
            assert _status(client_at, PARTNER) == 403                   # B: not testuser
            assert _status(client_at, "::ffff:203.0.113.9", key="sk-alice") == 200

    def test_rules_by_org_code(self, client_at, db_session, test_user):
        test_user.org_code = "RD-7"
        db_session.add(test_user)
        db_session.commit()
        _rule(db_session, "198.51.100.0/24", org_codes=["RD-7"])
        with _settings("enforce"):
            assert _status(client_at, LAB) == 200

    def test_end_time_is_exact_without_a_refresh(self, client_at, db_session):
        now = ip_allowlist.utcnow()
        _rule(db_session, PARTNER, expires_at=now + timedelta(hours=1))
        with _settings("enforce"):
            assert _status(client_at, PARTNER) == 200
            with patch("app.services.ip_allowlist.utcnow", return_value=now + timedelta(hours=1)):
                assert _status(client_at, PARTNER) == 403

    def test_ending_a_rule_early_applies_after_the_refresh(self, client_at, db_session):
        rule = _rule(db_session, PARTNER)
        with _settings("enforce"):
            ip_allowlist.end_rule(db_session, rule)
            # The snapshot still holds it as permanent until this worker reloads
            # (the admin endpoints reload at once; other workers within REFRESH_S).
            assert _status(client_at, PARTNER) == 200
            ip_allowlist.refresh()
            assert _status(client_at, PARTNER) == 403

    def test_monitor_serves_and_logs_account_address_and_reason(self, client_at, db_session):
        _account(db_session, "alice", "sk-alice")
        _rule(db_session, "203.0.113.0/24", usernames=["alice"])
        with _settings("monitor"), patch("app.services.ip_allowlist.logger") as log:
            assert _status(client_at, OUTSIDE) == 200
            assert _status(client_at, OUTSIDE) == 200   # same account + address: logged once a minute
            assert _status(client_at, PARTNER) == 200
        lines = [c.args for c in log.warning.call_args_list]
        assert [(a[2], a[3], a[4]) for a in lines] == [
            ("testuser", OUTSIDE, "no_rule_for_address"),
            ("testuser", PARTNER, "not_for_this_account"),
        ]

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

    def test_add_rule_for_accounts_and_org_codes(self, client_at, admin_user, db_session, test_user):
        _account(db_session, "carol", "k-c", org_code="RD-7")
        api, headers = self._admin(client_at, admin_user)
        r = api.post("/admin/ip-allowlist/rules", headers=headers, data={
            "cidr": "203.0.113.0/24", "label": "Partner office", "usernames": "testuser, testuser",
            "org_codes": "rd-7", "starts_at": "", "expires_at": "2099-01-01T09:00",
        })
        assert r.status_code == 200, r.text
        rule = db_session.exec(select(IpAllowRule)).one()
        assert (rule.cidr, rule.label, rule.usernames, rule.org_codes, rule.created_by) == (
            "203.0.113.0/24", "Partner office", ["testuser"], ["RD-7"], "admin",
        )
        assert rule.expires_at.isoformat() == "2099-01-01T01:00:00"  # 09:00 Asia/Taipei
        assert ip_allowlist.is_allowed(addr(PARTNER), "testuser")
        assert not ip_allowlist.is_allowed(addr(PARTNER), "admin")

    def test_add_rule_for_everyone(self, client_at, admin_user, db_session):
        api, headers = self._admin(client_at, admin_user)
        assert api.post("/admin/ip-allowlist/rules", headers=headers, data={"cidr": "10.0.0.0/8"}).status_code == 200
        rule = db_session.exec(select(IpAllowRule)).one()
        assert (rule.usernames, rule.org_codes, rule.expires_at) == ([], [], None)

    def test_add_rule_rejects_bad_input(self, client_at, admin_user):
        api, headers = self._admin(client_at, admin_user)
        for data in (
            {"cidr": "partner.example.com"},
            {"cidr": PARTNER, "expires_at": "2000-01-01T00:00"},
            {"cidr": PARTNER, "expires_at": "tomorrow"},
            {"cidr": PARTNER, "usernames": "nobody"},
            {"cidr": PARTNER, "org_codes": "XX-9"},
        ):
            r = api.post("/admin/ip-allowlist/rules", headers=headers, data=data)
            assert r.status_code == 400, data

    def test_edit_who_a_rule_is_for(self, client_at, admin_user, db_session, test_user):
        rule = _rule(db_session, "203.0.113.0/24", usernames=["testuser"])
        rule_id = rule.id
        api, headers = self._admin(client_at, admin_user)
        r = api.post(f"/admin/ip-allowlist/rules/{rule_id}/who", headers=headers,
                     data={"usernames": "admin", "org_codes": ""})
        assert r.status_code == 200 and r.json()["rule"]["usernames"] == ["admin"]
        assert ip_allowlist.is_allowed(addr(PARTNER), "admin")       # this worker reloaded at once
        assert not ip_allowlist.is_allowed(addr(PARTNER), "testuser")
        r = api.post(f"/admin/ip-allowlist/rules/{rule_id}/who", headers=headers, data={"usernames": "ghost"})
        assert r.status_code == 400
        r = api.post(f"/admin/ip-allowlist/rules/{rule_id}/who", headers=headers, data={})
        assert r.json()["rule"]["everyone"] is True

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
        assert not ip_allowlist.is_allowed(addr(PARTNER), "anyone")

    def test_settings_save_the_mode(self, client_at, admin_user):
        api, headers = self._admin(client_at, admin_user)
        with patch("app.routers.admin.set_ip_allowlist_settings") as save:
            assert api.post("/admin/ip-allowlist", headers=headers, data={"mode": "monitor"}).status_code == 200
            assert api.post("/admin/ip-allowlist", headers=headers, data={"mode": "block"}).status_code == 400
        save.assert_called_once_with("monitor")

    def test_admin_page_renders_the_card(self, client_at, admin_user, db_session, test_user):
        _rule(db_session, "10.0.0.0/8", label="HQ")
        _rule(db_session, "198.51.100.0/24", label="Lab", usernames=["testuser"])
        api, headers = self._admin(client_at, admin_user, ip=LAB)
        with _settings("monitor"):
            r = api.get("/admin", headers={"Authorization": headers["Authorization"]})
        assert r.status_code == 200
        assert "IP Allowlist" in r.text and "10.0.0.0/8" in r.text and "Everyone" in r.text
        assert "rules cover this address, but none is for your account" in r.text
        assert '/admin/ip-allowlist/rules/2/who' in r.text


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
