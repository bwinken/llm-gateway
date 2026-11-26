"""People whose sign-in was disabled at the identity provider (leavers):
``app/services/offboarding.py``, the job that asks Keycloak who is disabled,
and what the admin page shows of it."""

from __future__ import annotations

import inspect
import json
from datetime import datetime
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
import pytest
from sqlmodel import select

from app.models.schema import AuditLog, ScheduledJob, User
from app.services import offboarding, scheduler
from tests.conftest import auth_header, web_auth_header

REALM = "https://keycloak.test/realms/company"
ENV = {
    "OFFBOARDING_MODE": "monitor",
    "OFFBOARDING_KEYCLOAK_URL": REALM,
    "OFFBOARDING_KEYCLOAK_CLIENT_ID": "gateway-offboarding",
    "OFFBOARDING_KEYCLOAK_CLIENT_SECRET": "s3cret-value",
}


def _admin():
    return web_auth_header(sub="admin", scopes=["admin"])


@pytest.fixture()
def env(monkeypatch):
    for name in (
        "OFFBOARDING_MODE", "OFFBOARDING_KEYCLOAK_URL", "OFFBOARDING_KEYCLOAK_CLIENT_ID",
        "OFFBOARDING_KEYCLOAK_CLIENT_SECRET", "OFFBOARDING_KEYCLOAK_CA_FILE", "OFFBOARDING_MAX_PER_RUN",
        "OFFBOARDING_INTERVAL_S", "KEYCLOAK_ISSUER_URL", "KEYCLOAK_CA_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def _add(db_session, username: str, **fields) -> User:
    user = User(username=username, api_key=f"sk-{username}", **fields)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _reload(db_session, user: User) -> User:
    db_session.expire_all()
    return db_session.get(User, user.id)


class Keycloak:
    """A realm's token endpoint and Admin API, as an httpx transport."""

    def __init__(self, users: list[dict], *, knows_the_filter: bool = True):
        self.users = users
        self.knows_the_filter = knows_the_filter
        self.token_status = 200
        self.users_status = 200
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/realms/company/protocol/openid-connect/token":
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "unauthorized_client"})
            return httpx.Response(200, json={"access_token": "the-token"})
        assert request.url.path == "/admin/realms/company/users"
        assert request.headers["authorization"] == "Bearer the-token"
        if self.users_status != 200:
            return httpx.Response(self.users_status, json={"error": "forbidden"})
        q = request.url.params
        rows = self.users
        if q.get("enabled") == "false" and self.knows_the_filter:
            rows = [u for u in rows if u.get("enabled") is False]
        first, size = int(q.get("first", 0)), int(q.get("max", 100))
        return httpx.Response(200, json=rows[first:first + size])

    def client(self, _settings=None) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def _kc_user(name: str, enabled: bool = True) -> dict:
    return {"id": f"id-{name}", "username": name, "enabled": enabled}


@pytest.fixture()
def keycloak(env):
    realm = Keycloak([_kc_user("alice"), _kc_user("bob", enabled=False), _kc_user("carol")])
    with patch.object(offboarding, "_http", realm.client):
        yield realm


# ── Settings ──

class TestSettings:
    def test_off_unless_asked_for(self, env):
        env.delenv("OFFBOARDING_MODE")
        assert offboarding.settings()["mode"] == "off"
        assert offboarding.JOB.interval() == 0.0

    @pytest.mark.parametrize("raw", ["on", "true", "enforced", "yes"])
    def test_a_typo_never_turns_it_on(self, env, raw):
        env.setenv("OFFBOARDING_MODE", raw)
        assert offboarding.settings()["mode"] == "off"
        assert offboarding.JOB.interval() == 0.0

    def test_the_modes_and_the_pace(self, env):
        assert offboarding.JOB.interval() == offboarding.DEFAULT_INTERVAL_S
        env.setenv("OFFBOARDING_MODE", "ENFORCE")
        env.setenv("OFFBOARDING_INTERVAL_S", "600")
        assert offboarding.settings()["mode"] == "enforce"
        assert offboarding.JOB.interval() == 600.0

    def test_it_reads_the_realm_keycloak_sign_in_uses_when_it_has_none_of_its_own(self, env):
        env.delenv("OFFBOARDING_KEYCLOAK_URL")
        assert offboarding.missing_settings() == ["OFFBOARDING_KEYCLOAK_URL"]
        env.setenv("KEYCLOAK_ISSUER_URL", REALM + "/")
        assert offboarding.settings()["realm_url"] == REALM
        assert offboarding.missing_settings() == []

    def test_what_it_cannot_work_without(self, env):
        env.delenv("OFFBOARDING_KEYCLOAK_CLIENT_SECRET")
        env.delenv("OFFBOARDING_KEYCLOAK_CLIENT_ID")
        assert offboarding.missing_settings() == [
            "OFFBOARDING_KEYCLOAK_CLIENT_ID", "OFFBOARDING_KEYCLOAK_CLIENT_SECRET",
        ]

    @pytest.mark.parametrize("realm_url, expected", [
        ("https://k.test/realms/company", "https://k.test/admin/realms/company"),
        ("https://k.test/auth/realms/company", "https://k.test/auth/admin/realms/company"),
    ])
    def test_the_admin_api_is_beside_the_realm(self, realm_url, expected):
        assert offboarding.admin_url(realm_url) == expected

    @pytest.mark.parametrize("realm_url", ["https://k.test", "https://k.test/realms/", "https://k.test/realms/a/b"])
    def test_anything_but_a_realm_url_is_refused(self, realm_url):
        with pytest.raises(offboarding.OffboardingError):
            offboarding.admin_url(realm_url)

    def test_a_cap_that_is_no_number_is_the_default(self, env):
        env.setenv("OFFBOARDING_MAX_PER_RUN", "many")
        assert offboarding.settings()["max_per_run"] == offboarding.DEFAULT_MAX_PER_RUN


# ── Reading Keycloak ──

class TestReadDisabled:
    def test_the_disabled_users_by_name(self, keycloak):
        assert offboarding.read_disabled() == {"bob"}
        token = keycloak.requests[0]
        assert token.method == "POST"
        form = parse_qs(token.content.decode())
        assert form == {
            "grant_type": ["client_credentials"], "client_id": ["gateway-offboarding"],
            "client_secret": ["s3cret-value"],
        }

    def test_names_are_compared_without_case(self, keycloak):
        keycloak.users.append(_kc_user(" Dave ", enabled=False))
        assert offboarding.read_disabled() == {"bob", "dave"}

    def test_every_page_is_read(self, keycloak):
        keycloak.users = [_kc_user(f"u{i}", enabled=False) for i in range(offboarding._PAGE * 2 + 5)]
        assert len(offboarding.read_disabled()) == offboarding._PAGE * 2 + 5

    def test_a_server_that_ignores_the_filter_disables_nobody_extra(self, keycloak):
        # An older Keycloak answers ``enabled=false`` with everyone.
        keycloak.knows_the_filter = False
        assert offboarding.read_disabled() == {"bob"}

    def test_nobody_disabled_is_an_answer(self, keycloak):
        keycloak.users = [_kc_user("alice")]
        assert offboarding.read_disabled() == set()

    def test_a_client_that_sees_no_users_is_not_believed(self, keycloak):
        # ``query-users`` without ``view-users``: an empty list, not a 403.
        keycloak.users = []
        with pytest.raises(offboarding.OffboardingError, match="view-users"):
            offboarding.read_disabled()

    def test_an_answer_without_the_field_is_not_believed(self, keycloak):
        keycloak.users = [{"id": "x", "username": "bob"}]
        keycloak.knows_the_filter = False
        with pytest.raises(offboarding.OffboardingError, match="enabled"):
            offboarding.read_disabled()

    def test_a_refusal_names_the_status_and_never_the_secret(self, keycloak):
        keycloak.token_status = 401
        with pytest.raises(offboarding.OffboardingError) as caught:
            offboarding.read_disabled()
        assert "HTTP 401" in str(caught.value) and "s3cret-value" not in str(caught.value)
        keycloak.token_status, keycloak.users_status = 200, 403
        with pytest.raises(offboarding.OffboardingError, match="HTTP 403"):
            offboarding.read_disabled()

    def test_keycloak_not_reached(self, env):
        def down(request):
            raise httpx.ConnectTimeout("")
        with patch.object(offboarding, "_http", lambda s: httpx.Client(transport=httpx.MockTransport(down))):
            with pytest.raises(offboarding.OffboardingError, match="ConnectTimeout"):
                offboarding.read_disabled()

    def test_missing_settings_ask_nobody(self, keycloak, env):
        env.delenv("OFFBOARDING_KEYCLOAK_CLIENT_SECRET")
        with pytest.raises(offboarding.OffboardingError, match="OFFBOARDING_KEYCLOAK_CLIENT_SECRET"):
            offboarding.read_disabled()
        assert keycloak.requests == []


# ── Monitor: the admin is told, nothing is disabled ──

class TestMonitor:
    def test_a_person_reported_disabled_is_marked_and_keeps_working(self, client, db_session, keycloak):
        bob = _add(db_session, "bob")
        alice = _add(db_session, "alice")
        line = offboarding.run(db_session)
        assert "newly reported disabled: bob" in line and line.startswith("monitor")
        bob, alice = _reload(db_session, bob), _reload(db_session, alice)
        assert bob.sso_disabled_at is not None and bob.is_disabled is False
        assert alice.sso_disabled_at is None
        # The key works until an admin disables the account.
        assert client.get("/v1/me", headers=auth_header("sk-bob")).status_code == 200
        assert db_session.exec(select(AuditLog)).all() == []

    def test_the_first_report_is_kept(self, db_session, keycloak):
        bob = _add(db_session, "bob")
        first = datetime(2026, 11, 20, 1, 0)
        offboarding.reconcile(db_session, {"bob"}, mode="monitor", now=first)
        result = offboarding.reconcile(db_session, {"bob"}, mode="monitor", now=datetime(2026, 11, 26, 1, 0))
        assert result["new"] == [] and result["pending"] == 1
        assert _reload(db_session, bob).sso_disabled_at == first

    def test_the_gateways_name_may_differ_in_case(self, db_session):
        bob = _add(db_session, "Bob")
        offboarding.reconcile(db_session, {"bob"}, mode="monitor")
        assert _reload(db_session, bob).sso_disabled_at is not None

    def test_a_service_account_is_never_marked(self, db_session):
        app = _add(db_session, "bob", is_service_account=True)
        result = offboarding.reconcile(db_session, {"bob"}, mode="enforce")
        app = _reload(db_session, app)
        assert app.sso_disabled_at is None and app.is_disabled is False
        assert result == {"new": [], "back": [], "disabled": [], "held": False, "pending": 0}

    def test_a_person_keycloak_does_not_know_is_left_alone(self, db_session, keycloak):
        stranger = _add(db_session, "not-in-keycloak")
        offboarding.run(db_session)
        assert _reload(db_session, stranger).sso_disabled_at is None

    def test_the_mark_goes_when_keycloak_enables_them_again(self, db_session, keycloak):
        bob = _add(db_session, "bob")
        offboarding.run(db_session)
        keycloak.users[1]["enabled"] = True
        assert "enabled again at Keycloak: bob" in offboarding.run(db_session)
        assert _reload(db_session, bob).sso_disabled_at is None
        assert offboarding.to_review(db_session) == []

    def test_a_failed_read_changes_nothing(self, db_session, keycloak):
        bob = _add(db_session, "bob")
        offboarding.run(db_session)
        keycloak.users_status = 503
        with pytest.raises(offboarding.OffboardingError):
            offboarding.run(db_session)
        # Not "nobody is disabled": the mark of the last good read stays.
        assert _reload(db_session, bob).sso_disabled_at is not None

    def test_off_asks_nobody(self, db_session, keycloak, env):
        env.setenv("OFFBOARDING_MODE", "off")
        _add(db_session, "bob")
        assert offboarding.run(db_session) == "off"
        assert keycloak.requests == []

    def test_keycloak_is_read_before_the_database(self):
        # No pooled connection is held while Keycloak answers.
        src = inspect.getsource(offboarding.run)
        assert src.index("read_disabled(") < src.index("reconcile(")


# ── Enforce: the gateway disables the account ──

class TestEnforce:
    @pytest.fixture(autouse=True)
    def _enforce(self, env):
        env.setenv("OFFBOARDING_MODE", "enforce")

    def test_the_account_is_disabled_and_its_key_refused(self, client, db_session, keycloak):
        bob = _add(db_session, "bob")
        alice = _add(db_session, "alice")
        assert client.get("/v1/me", headers=auth_header("sk-bob")).status_code == 200
        line = offboarding.run(db_session)
        assert "disabled here: bob" in line
        assert _reload(db_session, bob).is_disabled is True
        assert _reload(db_session, alice).is_disabled is False
        assert client.get("/v1/me", headers=auth_header("sk-bob")).status_code == 403
        assert client.get("/v1/me", headers=auth_header("sk-alice")).status_code == 200

    def test_it_is_in_the_audit_log_as_the_job(self, client, db_session, keycloak, admin_user):
        bob = _add(db_session, "bob")
        offboarding.run(db_session)
        [row] = db_session.exec(select(AuditLog)).all()
        assert (row.actor, row.method, row.status) == ("job:offboarding", "JOB", 200)
        assert row.path == f"/admin/users/{bob.id}"
        detail = json.loads(row.detail)
        assert detail["target"] == "bob" and detail["is_disabled"] is True
        # ... and in the account's own history in the drawer.
        drawer = client.get(f"/admin/users/{bob.id}/detail", headers=_admin()).text
        assert "job:offboarding" in drawer
        # Once: the next run finds nothing left to do.
        offboarding.run(db_session)
        assert len(db_session.exec(select(AuditLog)).all()) == 1

    def test_those_marked_while_monitoring_are_disabled_once_it_is_switched_on(self, db_session, keycloak, env):
        env.setenv("OFFBOARDING_MODE", "monitor")
        bob = _add(db_session, "bob")
        offboarding.run(db_session)
        assert _reload(db_session, bob).is_disabled is False
        env.setenv("OFFBOARDING_MODE", "enforce")
        offboarding.run(db_session)
        assert _reload(db_session, bob).is_disabled is True

    def test_too_many_at_once_disables_nobody(self, db_session, keycloak, env):
        env.setenv("OFFBOARDING_MAX_PER_RUN", "2")
        names = ["bob", "dan", "eve"]
        keycloak.users = [_kc_user("alice")] + [_kc_user(n, enabled=False) for n in names]
        people = [_add(db_session, n) for n in names]
        line = offboarding.run(db_session)
        assert "none disabled" in line
        for person in people:
            person = _reload(db_session, person)
            assert person.is_disabled is False and person.sso_disabled_at is not None
        assert len(offboarding.to_review(db_session)) == 3
        assert db_session.exec(select(AuditLog)).all() == []
        # Within the cap they are disabled.
        env.setenv("OFFBOARDING_MAX_PER_RUN", "3")
        offboarding.run(db_session)
        assert all(_reload(db_session, p).is_disabled for p in people)

    def test_a_person_who_comes_back_is_enabled_by_an_admin_not_by_the_job(self, db_session, keycloak):
        bob = _add(db_session, "bob")
        offboarding.run(db_session)
        keycloak.users[1]["enabled"] = True
        offboarding.run(db_session)
        bob = _reload(db_session, bob)
        assert bob.sso_disabled_at is None and bob.is_disabled is True

    def test_an_account_an_admin_disabled_is_not_acted_on_again(self, db_session, keycloak):
        bob = _add(db_session, "bob", is_disabled=True)
        result = offboarding.reconcile(db_session, {"bob"}, mode="enforce")
        assert result["disabled"] == [] and result["pending"] == 0
        assert _reload(db_session, bob).sso_disabled_at is not None
        assert db_session.exec(select(AuditLog)).all() == []


# ── The job ──

class TestJob:
    def test_the_lifespan_starts_it(self):
        import app.main

        assert "offboarding.JOB" in inspect.getsource(app.main.lifespan)

    def test_a_run_is_recorded(self, db_session, keycloak):
        _add(db_session, "bob")
        assert scheduler.run_if_due(offboarding.JOB) == "ok"
        db_session.expire_all()
        row = db_session.get(ScheduledJob, "offboarding")
        assert row.last_status == "ok" and "bob" in row.last_detail

    def test_a_failed_read_is_recorded_not_raised(self, db_session, keycloak):
        keycloak.users_status = 403
        assert scheduler.run_if_due(offboarding.JOB) == "error"
        db_session.expire_all()
        row = db_session.get(ScheduledJob, "offboarding")
        assert row.last_status == "error" and "HTTP 403" in row.last_detail
        assert "s3cret-value" not in row.last_detail


# ── What the admin sees ──

class TestAdminPage:
    def test_nothing_is_shown_while_it_is_off(self, client, db_session, admin_user, env):
        env.delenv("OFFBOARDING_MODE")
        page = client.get("/admin", headers=_admin()).text
        assert 'id="leaversAttention"' not in page

    def test_the_people_to_disable_are_listed(self, client, db_session, admin_user, keycloak):
        bob = _add(db_session, "bob", display_name="Bob Lin", org_code="RD1")
        assert 'id="leaversAttention"' not in client.get("/admin", headers=_admin()).text
        scheduler.run_if_due(offboarding.JOB)
        page = client.get("/admin", headers=_admin()).text
        assert "1 sign-in disabled, account still active" in page
        block = page[page.index('id="leaversAttention"'):]
        block = block[:block.index("</details>")]
        assert f'data-um-detail="{bob.id}"' in block  # it opens the account
        assert "Bob Lin" in block and "(RD1)" in block
        assert "Monitoring only" in block and "last checked" in block
        assert 'href="/admin?status=sso_disabled#users"' in block
        assert "data-leavers-problem" not in block

    def test_it_clears_itself_once_the_account_is_disabled(self, client, db_session, admin_user, keycloak):
        bob = _add(db_session, "bob")
        offboarding.run(db_session)
        assert [e["username"] for e in offboarding.to_review(db_session)] == ["bob"]
        resp = client.post(
            "/admin/users/bulk-update", data={"ids": str(bob.id), "action": "disable", "enabled": "on"},
            headers={**_admin(), "Accept": "application/json"},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        assert offboarding.to_review(db_session) == []
        assert 'id="leaversAttention"' not in client.get("/admin", headers=_admin()).text

    def test_a_check_that_is_not_working_is_said(self, client, db_session, admin_user, keycloak):
        keycloak.token_status = 401
        scheduler.run_if_due(offboarding.JOB)
        page = client.get("/admin", headers=_admin()).text
        assert "leaver check not working" in page
        block = page[page.index("data-leavers-problem"):]
        assert "HTTP 401" in block[:400]

    def test_missing_settings_are_said_before_the_first_run(self, client, db_session, admin_user, env):
        env.delenv("OFFBOARDING_KEYCLOAK_CLIENT_SECRET")
        page = client.get("/admin", headers=_admin()).text
        block = page[page.index("data-leavers-problem"):]
        assert "OFFBOARDING_KEYCLOAK_CLIENT_SECRET" in block[:400]

    def test_the_table_filter_the_row_and_the_drawer(self, client, db_session, admin_user, keycloak):
        bob = _add(db_session, "bob")
        _add(db_session, "alice")
        table = lambda kind="users": client.get(  # noqa: E731
            "/admin/user-table", params={"kind": kind, "status": "sso_disabled"}, headers=_admin(),
        ).text
        assert 'data-username="bob"' not in table()
        offboarding.run(db_session)
        body = table()
        assert 'data-username="bob"' in body and 'data-username="alice"' not in body
        assert "data-sso-disabled" in body
        assert 'data-username="bob"' not in table("apps")  # never a filter for service accounts
        drawer = client.get(f"/admin/users/{bob.id}/detail", headers=_admin()).text
        note = drawer[drawer.index("data-ud-sso-disabled"):]
        assert "still works until you disable the account" in note[:400]
        # Disabled: out of the filter, still marked in the drawer.
        bob.is_disabled = True
        db_session.add(bob)
        db_session.commit()
        assert 'data-username="bob"' not in table()
        drawer = client.get(f"/admin/users/{bob.id}/detail", headers=_admin()).text
        assert "enable it again once Keycloak has" in drawer

    def test_the_filter_is_not_a_special_access_chip(self):
        from app.routers.admin import users

        assert "sso_disabled" in users.USER_TABLE_STATUSES
        assert "sso_disabled" not in users.SPECIAL_ACCESS_KEYS

    def test_the_guide_says_the_mode_in_force(self, client, admin_user, env):
        body = client.get("/admin/guide", headers=_admin()).text
        entry = body[body.index('id="account-sso-disabled"'):]
        assert "now: monitor" in entry[:3000]
