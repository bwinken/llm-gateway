"""API IP allowlist (app/services/ip_allowlist.py): the check on the API
routers, rule windows, the admin card and the dashboard's self-service."""

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


def _settings(mode: str = "enforce", max_hours: int = 8):
    return patch(
        "app.services.ip_allowlist.get_ip_allowlist_settings",
        return_value={"mode": mode, "self_service_max_hours": max_hours},
    )


def _allow_self_service(session: Session, user: User, allowed: bool = True) -> None:
    user.can_self_allow_ip = allowed
    session.add(user)
    session.commit()


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
        assert (rule.cidr, rule.label, rule.source, rule.created_by) == ("203.0.113.0/24", "Partner office", "admin", "admin")
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

    def test_settings_save_mode_and_hours(self, client_at, admin_user):
        api, headers = self._admin(client_at, admin_user)
        with patch("app.routers.admin.set_ip_allowlist_settings") as save:
            r = api.post("/admin/ip-allowlist", headers=headers, data={
                "mode": "monitor", "self_service_max_hours": "12",
            })
            assert r.status_code == 200
        save.assert_called_once_with("monitor", 12)

    def test_settings_reject_bad_mode_and_hours(self, client_at, admin_user):
        api, headers = self._admin(client_at, admin_user)
        with patch("app.routers.admin.set_ip_allowlist_settings") as save:
            for data in ({"mode": "block", "self_service_max_hours": "8"},
                         {"mode": "enforce", "self_service_max_hours": "0"},
                         {"mode": "enforce", "self_service_max_hours": "1000"}):
                assert api.post("/admin/ip-allowlist", headers=headers, data=data).status_code == 400
        save.assert_not_called()

    def test_admin_page_renders_the_card_while_enforcing(self, client_at, admin_user, db_session, test_user):
        _rule(db_session, "10.0.0.0/8", label="HQ")
        _allow_self_service(db_session, test_user)
        api, headers = self._admin(client_at, admin_user, ip=OUTSIDE)
        with _settings("enforce"):
            r = api.get("/admin", headers={"Authorization": headers["Authorization"]})
        assert r.status_code == 200
        assert "IP Allowlist" in r.text and "10.0.0.0/8" in r.text and "not on the allowlist" in r.text
        # Self-service users are listed, with a link to that filter of the Users table.
        assert '<span class="font-mono text-t2">testuser</span>' in r.text
        assert "/admin?status=ip_self#users" in r.text

    def test_toggle_grants_and_revoke_ends_open_grants(self, client_at, admin_user, db_session, test_user):
        api, headers = self._admin(client_at, admin_user)
        r = api.post(f"/admin/users/{test_user.id}/toggle-ip-self-service", headers=headers)
        assert r.json() == {"ok": True, "can_self_allow_ip": True, "ended_grants": 0}
        ip_allowlist.grant_self(db_session, "testuser", ip_allowlist.parse_address(PARTNER), 4)
        ip_allowlist.refresh()
        assert ip_allowlist.is_allowed(ip_allowlist.parse_address(PARTNER))

        r = api.post(f"/admin/users/{test_user.id}/toggle-ip-self-service", headers=headers)
        assert r.json() == {"ok": True, "can_self_allow_ip": False, "ended_grants": 1}
        # Ended now, and this worker's snapshot already knows.
        assert not ip_allowlist.is_allowed(ip_allowlist.parse_address(PARTNER))
        assert ip_allowlist.self_grants(db_session, "testuser") == []

    def test_patch_api_sets_the_flag_strictly(self, client_at, admin_user, db_session, test_user):
        api, headers = self._admin(client_at, admin_user)
        url = f"/admin/users/{test_user.id}"
        assert api.patch(url, headers=headers, json={"can_self_allow_ip": "true"}).status_code == 400
        assert api.patch(url, headers=headers, json={"can_self_allow_ip": True}).status_code == 200
        ip_allowlist.grant_self(db_session, "testuser", ip_allowlist.parse_address(PARTNER), 4)
        assert api.patch(url, headers=headers, json={"can_self_allow_ip": False}).status_code == 200
        db_session.expire_all()
        assert db_session.get(User, test_user.id).can_self_allow_ip is False
        assert ip_allowlist.self_grants(db_session, "testuser") == []
        listed = {u["username"]: u for u in api.get("/admin/users", headers=headers).json()}
        assert listed["testuser"]["can_self_allow_ip"] is False


# ── Dashboard self-service ──

class TestSelfService:
    def _post(self, api, path, **data):
        return api.post(path, headers={**web_auth_header(), "Accept": "application/json"}, data=data)

    def test_authorized_user_opens_and_extends_their_address(self, client_at, db_session, test_user):
        _allow_self_service(db_session, test_user)
        api = client_at(PARTNER)
        with _settings("enforce"):
            page = api.get("/dashboard", headers=web_auth_header())
            assert "Open this address" in page.text
            assert api.get("/v1/models", headers=auth_header()).status_code == 403

            assert self._post(api, "/dashboard/ip-access", hours="2").status_code == 200
            assert api.get("/v1/models", headers=auth_header()).status_code == 200

            assert self._post(api, "/dashboard/ip-access", hours="8").status_code == 200
        rules = db_session.exec(select(IpAllowRule)).all()
        assert len(rules) == 1  # extended, not duplicated
        rule = rules[0]
        assert (rule.cidr, rule.source, rule.created_by) == (f"{PARTNER}/32", "self", "testuser")
        assert timedelta(hours=7, minutes=59) < rule.expires_at - ip_allowlist.utcnow() <= timedelta(hours=8)

    def test_unauthorized_user_is_refused(self, client_at):
        with _settings("enforce"):
            assert self._post(client_at(PARTNER), "/dashboard/ip-access", hours="1").status_code == 403
            page = client_at(PARTNER).get("/dashboard", headers=web_auth_header()).text
        assert "Open this address" not in page and "Ask an administrator to add it" in page

    def test_hours_are_capped(self, client_at, db_session, test_user):
        _allow_self_service(db_session, test_user)
        with _settings("enforce", max_hours=4):
            assert self._post(client_at(PARTNER), "/dashboard/ip-access", hours="5").status_code == 400
            assert self._post(client_at(PARTNER), "/dashboard/ip-access", hours="0").status_code == 400

    def test_at_most_three_open_addresses(self, db_session):
        for i in range(ip_allowlist.MAX_SELF_GRANTS):
            ip_allowlist.grant_self(db_session, "alice", ip_allowlist.parse_address(f"192.0.2.{i}"), 1)
        with pytest.raises(ValueError, match="End one"):
            ip_allowlist.grant_self(db_session, "alice", ip_allowlist.parse_address("192.0.2.99"), 1)
        # Another user is unaffected.
        ip_allowlist.grant_self(db_session, "bob", ip_allowlist.parse_address("192.0.2.99"), 1)

    def test_user_ends_only_their_own_grants(self, client_at, db_session, test_user):
        _allow_self_service(db_session, test_user)
        db_session.add(User(username="other", api_key="sk-other"))
        db_session.commit()
        mine = ip_allowlist.grant_self(db_session, "testuser", ip_allowlist.parse_address(PARTNER), 1)
        theirs = ip_allowlist.grant_self(db_session, "other", ip_allowlist.parse_address(OUTSIDE), 1)
        admin_rule = _rule(db_session, "10.0.0.0/8")
        mine_id, theirs_id, admin_id = mine.id, theirs.id, admin_rule.id
        api = client_at(PARTNER)
        with _settings("enforce"):
            assert self._post(api, f"/dashboard/ip-access/{theirs_id}/end").status_code == 404
            assert self._post(api, f"/dashboard/ip-access/{admin_id}/end").status_code == 404
            assert self._post(api, f"/dashboard/ip-access/{mine_id}/end").status_code == 200
        db_session.expire_all()
        assert db_session.get(IpAllowRule, mine_id).expires_at <= ip_allowlist.utcnow()
        assert db_session.get(IpAllowRule, theirs_id).expires_at > ip_allowlist.utcnow()

    def test_no_card_while_off_or_when_nothing_to_do(self, client_at, db_session):
        api = client_at(PARTNER)
        assert "API access from this address" not in api.get("/dashboard", headers=web_auth_header()).text
        _rule(db_session, PARTNER)
        with _settings("enforce"):
            assert "API access from this address" not in api.get("/dashboard", headers=web_auth_header()).text
        with _settings("enforce"):
            text = client_at(OUTSIDE).get("/dashboard", headers=web_auth_header()).text
        assert "Ask an administrator to add it" in text


# ── Special access overview and filters (admin Users tab) ──

class TestSpecialAccess:
    def _seed(self, db_session):
        rows = [
            User(username="alice", api_key="k1", can_self_allow_ip=True),
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
        assert summary["bedrock"] == (1, 1)
        assert summary["cloud_cap"] == (1, 0)
        assert summary["waiver_changed"] == (1, 1)  # carol waived; app_limited un-waived
        assert summary["ip_self"] == (1, 0)
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

        assert names("users", "ip_self") == ["alice"]
        assert names("users", "special") == ["admin", "alice", "bob", "carol", "testuser"]
        assert names("apps", "special") == ["app_limited"]
        assert names("apps", "waiver_changed") == ["app_limited"]
        assert names("users", "cloud_cap") == ["bob"]

    def test_admin_page_shows_the_overview_and_the_ip_toggle(self, client, db_session, admin_user):
        self._seed(db_session)
        body = client.get("/admin", params={"status": "ip_self"},
                          headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        assert "Special access" in body
        assert 'href="/admin?status=ip_self#users" class="sa-chip  is-active"' in body
        assert "toggle-ip-self-service" in body
        # App accounts never sign in to the dashboard: no IP toggle on their rows.
        apps_panel = body[body.index('data-um-panel="apps"'):]
        assert "toggle-ip-self-service" not in apps_panel
