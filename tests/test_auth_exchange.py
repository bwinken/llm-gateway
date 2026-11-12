"""``POST /auth/api-key`` (app/routers/auth_api.py): a client that is not a
browser trades an AuthCenter access token for its account's API key, once,
and uses the key from then on."""

from __future__ import annotations

import json
from contextlib import ExitStack
from unittest.mock import patch

import pytest
from sqlmodel import select

from app.models.schema import AuditLog, User
from app.services import auth_throttle, ip_allowlist
from tests.conftest import auth_header, web_auth_header

URL = "/auth/api-key"
OFFICE = "10.20.30.40"
OUTSIDE = "192.0.2.99"


def _exchange(client, **token):
    return client.post(URL, headers=web_auth_header(**token))


def _user(db_session, username: str) -> User | None:
    db_session.expire_all()
    return db_session.exec(select(User).where(User.username == username)).first()


def _audit(db_session) -> list[AuditLog]:
    db_session.expire_all()
    return list(db_session.exec(select(AuditLog).where(AuditLog.path == URL).order_by(AuditLog.id)).all())


class TestExchange:
    def test_a_token_gets_the_accounts_key(self, client, test_user):
        resp = _exchange(client, sub="testuser")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "object": "api_key", "username": "testuser", "api_key": "sk-testkey123", "legacy_key": True,
        }
        # A credential: never cached anywhere on the way.
        assert resp.headers["cache-control"] == "no-store"

    def test_the_key_then_works_like_any_other(self, client, test_user):
        key = _exchange(client, sub="testuser").json()["api_key"]
        me = client.get("/v1/me", headers=auth_header(key))
        assert me.status_code == 200 and me.json()["username"] == "testuser"

    def test_the_existing_key_is_returned_never_a_new_one(self, client, db_session, test_user):
        # A new key would log the user's other machines out.
        issued = _user(db_session, "testuser").api_key_created_at
        first = _exchange(client, sub="testuser").json()["api_key"]
        second = _exchange(client, sub="testuser").json()["api_key"]
        assert first == second == "sk-testkey123"
        assert _user(db_session, "testuser").api_key_created_at == issued

    def test_a_first_sign_in_creates_the_account_like_the_dashboard_does(self, client, db_session, test_user):
        from app.core.config import get_default_daily_limit
        from app.models.schema import is_legacy_api_key

        assert _user(db_session, "newcomer") is None
        resp = _exchange(client, sub="newcomer", display_name="New Comer", org_code="RD-7")
        assert resp.status_code == 200, resp.text
        user = _user(db_session, "newcomer")
        assert (user.display_name, user.org_code) == ("New Comer", "RD-7")
        assert user.daily_limit_usd == get_default_daily_limit()
        assert resp.json()["api_key"] == user.api_key
        assert resp.json()["legacy_key"] is False and not is_legacy_api_key(user.api_key)
        # ... and the same account the web UI then finds.
        assert client.get("/dashboard", headers=web_auth_header(sub="newcomer")).status_code == 200

    def test_names_from_the_token_are_synced(self, client, db_session, test_user):
        _exchange(client, sub="testuser", display_name="Tess", org_code="QA-1")
        user = _user(db_session, "testuser")
        assert (user.display_name, user.org_code) == ("Tess", "QA-1")

    def test_only_post(self, client, test_user):
        assert client.get(URL, headers=web_auth_header()).status_code == 405


class TestRefusals:
    def test_no_token(self, client, test_user):
        resp = client.post(URL)
        assert resp.status_code == 401
        assert "api_key" not in resp.text

    @pytest.mark.parametrize("token", ["not-a-token", "sk-testkey123", "eyJhbGciOiJub25lIn0.e30."])
    def test_what_is_not_an_authcenter_token(self, client, test_user, token):
        # An API key is not a way to read an API key.
        resp = client.post(URL, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert "sk-testkey123" not in resp.text

    def test_the_x_api_key_header_is_not_a_way_in(self, client, test_user):
        assert client.post(URL, headers={"x-api-key": "sk-testkey123"}).status_code == 401

    def test_a_token_without_an_account_name(self, client, test_user):
        with patch("app.core.auth._decode_jwt", return_value={"scopes": ["read"]}):
            assert client.post(URL, headers={"Authorization": "Bearer x"}).status_code == 401

    def test_a_token_without_the_read_scope(self, client, db_session, test_user):
        resp = _exchange(client, sub="outsider", scopes=["billing"])
        assert resp.status_code == 403
        # Refused before anything is created for it.
        assert _user(db_session, "outsider") is None

    def test_admin_scope_is_enough(self, client, test_user):
        assert _exchange(client, sub="testuser", scopes=["admin"]).status_code == 200

    def test_a_disabled_account(self, client, db_session, test_user):
        test_user.is_disabled = True
        db_session.add(test_user)
        db_session.commit()
        resp = _exchange(client, sub="testuser")
        assert resp.status_code == 403
        assert "sk-testkey123" not in resp.text

    def test_the_token_opens_nothing_else(self, client, test_user):
        # On the API a bearer value is an API key, and this is not one.
        jwt_header = web_auth_header(sub="testuser", scopes=["admin"])
        for path in ("/v1/me", "/v1/models", "/v1/usage", "/azure/v1/models", "/aws/v1/models"):
            assert client.get(path, headers=jwt_header).status_code == 401, path


class TestIpAllowlist:
    """The address rules that would refuse the key refuse handing it out."""

    @pytest.fixture()
    def client_at(self, db_session, test_user):
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

    def _enforce(self):
        return patch("app.services.ip_allowlist.get_ip_allowlist_settings", return_value={"mode": "enforce"})

    def test_an_unlisted_address_is_told_at_sign_in(self, client_at, db_session):
        ip_allowlist.add_rule(db_session, "10.0.0.0/8", created_by="admin")
        ip_allowlist.refresh()
        with self._enforce():
            assert _exchange(client_at(OFFICE), sub="testuser").status_code == 200
            refused = _exchange(client_at(OUTSIDE), sub="testuser")
        assert refused.status_code == 403
        assert OUTSIDE in refused.json()["detail"]
        assert "sk-testkey123" not in refused.text

    def test_off_by_default(self, client_at):
        assert _exchange(client_at(OUTSIDE), sub="testuser").status_code == 200


class TestThrottle:
    """Invalid tokens count like invalid keys, in the same per-address tally."""

    @pytest.fixture(autouse=True)
    def _small_limits(self):
        with (
            patch.object(auth_throttle, "DISTINCT_KEYS", 3),
            patch.object(auth_throttle, "WINDOW_S", 600),
            patch.object(auth_throttle, "LOCKOUT_S", 900),
        ):
            yield

    def _bad(self, client, n: int):
        return client.post(URL, headers={"Authorization": f"Bearer bad-token-{n}"})

    def test_guessing_tokens_locks_the_address(self, client, test_user):
        for n in range(3):
            assert self._bad(client, n).status_code == 401
        locked = self._bad(client, 99)
        assert locked.status_code == 429
        assert 0 < int(locked.headers["Retry-After"]) <= 900
        # Locked means locked: a good token is not looked at either.
        assert _exchange(client, sub="testuser").status_code == 429
        # One tally with the API keys.
        assert client.get("/v1/me", headers=auth_header()).status_code == 429

    def test_retrying_one_expired_token_never_locks(self, client, test_user):
        for _ in range(20):
            assert self._bad(client, 1).status_code == 401
        assert _exchange(client, sub="testuser").status_code == 200


class TestAudit:
    def test_a_key_handed_out_is_recorded_without_the_key(self, client, db_session, test_user):
        assert _exchange(client, sub="testuser").status_code == 200
        (entry,) = _audit(db_session)
        assert (entry.actor, entry.method, entry.status) == ("testuser", "POST", 200)
        assert "sk-testkey123" not in entry.detail
        assert json.loads(entry.detail)["response"]["api_key"] == "<redacted>"

    def test_a_refused_account_is_recorded_too(self, client, db_session, test_user):
        test_user.is_disabled = True
        db_session.add(test_user)
        db_session.commit()
        _exchange(client, sub="testuser")
        (entry,) = _audit(db_session)
        assert (entry.actor, entry.status) == ("testuser", 403)

    def test_garbage_cannot_fill_the_log(self, client, db_session, test_user):
        # No oauth2-proxy in front of this route: anyone can post to it,
        # and audit_log is append-only.
        client.post(URL)
        client.post(URL, headers={"Authorization": "Bearer nonsense"})
        client.post(URL, headers={"Authorization": "Bearer sk-testkey123"})
        assert _audit(db_session) == []

    def test_other_routes_are_audited_as_before(self):
        from app.services import audit

        assert audit.is_audited("POST", "/auth/api-key")
        assert not audit.is_audited("GET", "/auth/api-key")
        assert not audit.is_audited("POST", "/auth/anything-else")
        assert not audit.is_audited("POST", "/v1/chat/completions")
        assert audit.is_audited("POST", "/dashboard/refresh-key")


class TestWiring:
    def test_it_is_the_only_route_under_auth_and_reads_no_api_key(self, client):
        from fastapi.routing import iter_route_contexts

        from app.core import deps

        found = [
            ctx for ctx in iter_route_contexts(client.app.routes)
            if ctx.path == "/auth" or ctx.path.startswith("/auth/")
        ]
        assert [(ctx.path, sorted(ctx.methods)) for ctx in found] == [("/auth/api-key", ["POST"])]

        def calls(dependant) -> set:
            return {dependant.call} | {c for d in dependant.dependencies for c in calls(d)}

        used = calls(found[0].dependant)
        assert deps.get_current_user not in used and deps.get_current_user_unmetered not in used

    def test_runs_off_the_event_loop(self):
        import inspect

        from app.routers import auth_api

        assert not inspect.iscoroutinefunction(auth_api.api_key_for_token)

    def test_the_production_app_mounts_it(self):
        from fastapi.routing import iter_route_contexts

        from app import main

        assert "/auth/api-key" in {ctx.path for ctx in iter_route_contexts(main.app.routes)}
