"""The account endpoints take two sign-ins (``deps.get_account_user``).

``/v1/me``, ``/v1/status``, ``/v1/usage``, ``/v1/service-accounts``,
``/v1/notices``, ``/v1/settings`` and the two that write are asked by a
program with the account's API key and by a browser with the person's SSO
sign-in (the proxy in front trades the session cookie for the access token,
so both arrive as ``Authorization: Bearer``). One route serves both.

What is pinned here is how the sign-in is judged: the key keeps every rule
it had, the SSO sign-in gets the web UI's rules, and the model API takes
neither a token nor anything new. What a signed-in person is shown is in
``tests/test_dashboard_api.py``."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlmodel import Session, select

from app.models.schema import AuditLog, ClientViolation, User
from app.services import auth_throttle, ip_allowlist
from tests.conftest import auth_header, web_auth_header

# app.routers.* are imported inside the tests, never at module level.

READS = ["me", "status", "usage", "service-accounts", "notices", "settings"]
HOLDER_ONLY = ["usage", "service-accounts", "notices", "settings"]
OWN_KEY = "sk-testkey123"
OUTSIDE = "192.0.2.99"
EVENT = {"event": "installed", "tool": "setup-tool", "tool_version": "1.0", "component": "claude-code", "os": "linux"}
NOT_A_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0dXNlciJ9.not-the-signature"


def _sso(sub: str = "testuser", **kw) -> dict[str, str]:
    return web_auth_header(sub=sub, **kw)


def _add(db_session: Session, **fields) -> User:
    user = User(**fields)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _violation(db_session: Session, rule_id: str = "CC001", username: str = "testuser") -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.add(ClientViolation(rule_id=rule_id, username=username, first_seen_at=now, last_seen_at=now))
    db_session.commit()


def _audit(db_session: Session) -> list[AuditLog]:
    db_session.expire_all()
    return list(db_session.exec(select(AuditLog).order_by(AuditLog.id)).all())


class TestEitherSignIn:
    @pytest.mark.parametrize("name", READS)
    def test_every_read_answers_a_key_and_a_sign_in(self, client, test_user, name):
        assert client.get(f"/v1/{name}", headers=auth_header()).status_code == 200
        assert client.get(f"/v1/{name}", headers=_sso()).status_code == 200
        assert client.get(f"/v1/{name}").status_code == 401

    def test_the_two_writes_too(self, client, db_session, test_user):
        _violation(db_session)
        resp = client.post("/v1/notices/resolved", headers=_sso())
        assert resp.status_code == 200 and resp.json()["resolved"] == ["CC001"]
        assert client.post("/v1/events", headers=_sso(), json=EVENT).status_code == 200
        assert client.post("/v1/events", headers=auth_header(), json=EVENT).status_code == 200

    def test_me_without_the_prefix(self, client, test_user):
        assert client.get("/me", headers=_sso()).json()["username"] == "testuser"

    def test_it_is_the_persons_own_account(self, client, db_session, test_user):
        _add(db_session, username="alice", api_key="sk-alice")
        assert client.get("/v1/me", headers=_sso("alice")).json()["username"] == "alice"
        assert client.get("/v1/me", headers=_sso()).json()["username"] == "testuser"

    def test_a_first_sign_in_makes_the_account_as_the_dashboard_does(self, client, db_session):
        body = client.get("/v1/me", headers=_sso("newcomer", display_name="New Comer")).json()
        assert body["username"] == "newcomer" and body["display_name"] == "New Comer"
        assert db_session.exec(select(User).where(User.username == "newcomer")).first() is not None

    def test_answers_once_the_budget_is_spent(self, client, db_session, test_user):
        test_user.daily_limit_usd = 0.0
        db_session.add(test_user)
        db_session.commit()
        assert client.get("/v1/me", headers=_sso()).status_code == 200
        assert client.get("/v1/usage", headers=_sso()).status_code == 200


class TestWhatAKeyIsNotShown:
    """The holder of the account is told the same either way, with two
    exceptions: only a sign-in can make an admin, and only a sign-in is
    told who an application was used for."""

    def test_me_is_the_same_but_for_the_admin_right(self, client, db_session, test_user):
        test_user.org_code = "RD1"
        db_session.add(test_user)
        db_session.commit()
        by_key = client.get("/v1/me", headers=auth_header()).json()
        signed_in = client.get("/v1/me", headers=_sso(scopes=["admin"])).json()
        assert by_key["is_admin"] is False and signed_in["is_admin"] is True
        assert set(by_key) == set(signed_in) and {"api_key", "site", "org_code"} <= set(by_key)
        for body in (by_key, signed_in):
            body.pop("is_admin"), body["daily"].pop("resets_in_seconds")
        # An admin sees every cloud backend; nothing else differs.
        by_key.pop("backends"), signed_in.pop("backends")
        assert by_key == signed_in

    def test_the_key_is_masked_and_the_department_is_left_out_without_one(self, client, test_user):
        body = client.get("/v1/me", headers=auth_header()).json()
        assert "org_code" not in body
        assert body["api_key"]["masked"] not in ("", OWN_KEY) and body["api_key"]["masked"].endswith(OWN_KEY[-4:])
        assert body["site"]["api_base"].endswith("/v1") and all(body["site"]["links"].values())

    def test_who_an_application_was_used_for_is_told_to_a_sign_in_only(self, client, db_session, test_user):
        from decimal import Decimal

        from app.models.schema import AppOwner, UsageLog
        from tests.conftest import earlier_today

        app = _add(
            db_session, username="hr-chat", api_key="sk-hr-chat-secret", is_service_account=True,
            acts_for_users=True, concurrency_waived=True,
        )
        alice = _add(db_session, username="alice", api_key="sk-alice")
        db_session.add(AppOwner(app_id=app.id, owner_id=test_user.id))
        db_session.add(UsageLog(
            user_id=alice.id, model="test-llm", model_type="llm", input_tokens=1000, output_tokens=500,
            cost_usd=Decimal("1.5"), endpoint="/v1/chat/completions", backend="vllm", via_user_id=app.id,
            created_at=earlier_today(),
        ))
        db_session.commit()
        by_key = client.get("/v1/service-accounts", headers=auth_header())
        signed_in = client.get("/v1/service-accounts", headers=_sso()).json()
        assert set(by_key.json()) == {"object", "month", "service_accounts", "withheld"}
        assert set(signed_in) == set(by_key.json()) | {"top_users"}
        [row], [key_row] = signed_in["service_accounts"], by_key.json()["service_accounts"]
        assert row["sent_for"]["rows"][0]["user"] == "alice"
        # The same account otherwise, and by key nobody is named.
        assert {k: v for k, v in row.items() if k != "sent_for"} == key_row
        assert "sent_for" not in by_key.text and "alice" not in by_key.text
        assert "sk-hr-chat-secret" not in by_key.text and key_row["api_key_masked"].endswith("cret")

    def test_an_application_acting_for_a_user_is_not_told_the_holders_part(self, client, db_session, test_user):
        """It gets what it needs to serve the user (the budget) and no
        more: not the user's key, masked or not, nor their department."""
        _add(
            db_session, username="hr-chat", api_key="sk-hr-chat-secret", is_service_account=True,
            acts_for_users=True, concurrency_waived=True,
        )
        test_user.org_code = "RD1"
        db_session.add(test_user)
        db_session.commit()
        body = client.get("/v1/me", headers={**auth_header("sk-hr-chat-secret"), "X-On-Behalf-Of": "testuser"}).json()
        assert body["username"] == "testuser" and "daily" in body
        assert not {"api_key", "site", "org_code"} & set(body)

    @pytest.mark.parametrize("name", READS)
    def test_no_read_holds_the_key_in_full_either_way(self, client, test_user, name):
        for headers in (auth_header(), _sso()):
            assert OWN_KEY not in client.get(f"/v1/{name}", headers=headers).text


class TestTheModelApiIsByKeyOnly:
    def test_a_sign_in_is_not_a_key_there(self, client, test_user):
        body = {"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]}
        assert client.get("/v1/models", headers=_sso(scopes=["admin"])).status_code == 401
        assert client.post("/v1/chat/completions", headers=_sso(), json=body).status_code == 401
        assert client.post("/v1/messages", headers=_sso(), json=body).status_code == 401

    def test_only_the_account_endpoints_take_it(self):
        from fastapi.routing import iter_route_contexts

        from app.core import deps
        from tests.conftest import _build_test_app

        def calls(dependant) -> set:
            return {dependant.call} | {c for d in dependant.dependencies for c in calls(d)}

        taking = {
            context.path for context in iter_route_contexts(_build_test_app().routes)
            if getattr(context, "dependant", None) is not None and deps.get_account_user in calls(context.dependant)
        }
        assert taking == {
            "/v1/me", "/me", "/v1/status", "/v1/usage", "/v1/service-accounts", "/v1/notices",
            "/v1/notices/resolved", "/v1/events", "/v1/settings",
        }


class TestTheSignInIsTheWebUis:
    def test_a_token_that_does_not_verify_is_a_401(self, client, test_user):
        resp = client.get("/v1/me", headers={"Authorization": f"Bearer {NOT_A_TOKEN}"})
        assert resp.status_code == 401 and "access token" in resp.json()["detail"]

    def test_the_read_scope_is_needed(self, client, test_user):
        resp = client.get("/v1/me", headers=_sso(scopes=["other"]))
        assert resp.status_code == 403 and "scope" in resp.json()["detail"]

    def test_a_disabled_account_is_refused_unless_the_token_says_admin(self, client, db_session, test_user):
        test_user.is_disabled = True
        db_session.add(test_user)
        db_session.commit()
        assert client.get("/v1/me", headers=_sso()).status_code == 403
        assert client.get("/v1/me", headers=_sso(scopes=["admin"])).status_code == 200
        # By key nobody is an admin: the key of a disabled account is refused.
        assert client.get("/v1/me", headers=auth_header()).status_code == 403

    def test_the_stored_admin_flag_grants_nothing(self, client, db_session, test_user):
        test_user.is_admin = True
        db_session.add(test_user)
        db_session.commit()
        assert client.get("/v1/me", headers=_sso(scopes=["read"])).json()["is_admin"] is False

    def test_nobody_signs_in_as_a_service_account(self, client, db_session):
        _add(db_session, username="hr-chat", api_key="sk-hr-chat-secret", is_service_account=True)
        resp = client.get("/v1/me", headers=_sso("hr-chat"))
        assert resp.status_code == 403 and "sk-hr-chat-secret" not in resp.text

    def test_the_on_behalf_header_means_nothing_to_a_sign_in(self, client, db_session, test_user):
        _add(db_session, username="alice", api_key="sk-alice")
        resp = client.get("/v1/me", headers={**_sso(), "X-On-Behalf-Of": "alice"})
        assert resp.json()["username"] == "testuser"


class TestAKeyKeepsItsRules:
    def test_a_key_shaped_like_a_token_is_still_a_key(self, client, db_session, test_user):
        """A key set by hand may have any shape: auth is an exact lookup,
        and the token check is only tried when that lookup misses."""
        _add(db_session, username="oldtimer", api_key="legacy.key.value")
        resp = client.get("/v1/me", headers=auth_header("legacy.key.value"))
        assert resp.status_code == 200 and resp.json()["username"] == "oldtimer"
        assert resp.json()["is_admin"] is False
        assert client.get("/v1/service-accounts", headers=auth_header("legacy.key.value")).json().keys() \
            == {"object", "month", "service_accounts", "withheld"}  # a key's answer, not a sign-in's

    def test_a_key_beside_a_foreign_token_still_gets_in(self, client, test_user):
        """A corporate proxy may put its own token in ``Authorization``
        while the client sends its key in ``x-api-key``."""
        headers = {"Authorization": f"Bearer {NOT_A_TOKEN}", "x-api-key": OWN_KEY}
        assert client.get("/v1/me", headers=headers).json()["username"] == "testuser"
        # Judged as the key it is: the answer a key gets, not a sign-in's.
        assert "top_users" not in client.get("/v1/service-accounts", headers=headers).json()

    @pytest.mark.parametrize("name", HOLDER_ONLY)
    def test_an_application_acting_for_a_user_still_reads_only_what_it_needs(self, client, db_session, test_user, name):
        _add(
            db_session, username="hr-chat", api_key="sk-hr-chat-secret", is_service_account=True,
            acts_for_users=True, concurrency_waived=True,
        )
        headers = {**auth_header("sk-hr-chat-secret"), "X-On-Behalf-Of": "testuser"}
        assert client.get("/v1/me", headers=headers).json()["username"] == "testuser"
        assert client.get(f"/v1/{name}", headers=headers).status_code == 403


class TestIpAllowlist:
    """The list is about API keys. The dashboard is reachable from any
    address, and so is its data by sign-in: these routes return no key."""

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

    def test_a_key_is_refused_from_an_unlisted_address_and_a_sign_in_is_not(self, client_at, db_session):
        ip_allowlist.add_rule(db_session, "10.0.0.0/8", created_by="admin")
        ip_allowlist.refresh()
        outside = client_at(OUTSIDE)
        with patch("app.services.ip_allowlist.get_ip_allowlist_settings", return_value={"mode": "enforce"}):
            for name in READS:
                assert outside.get(f"/v1/{name}", headers=auth_header()).status_code == 403, name
                assert outside.get(f"/v1/{name}", headers=_sso()).status_code == 200, name


class TestThrottle:
    """The failed-auth throttle is about keys, and stays about keys."""

    @pytest.fixture(autouse=True)
    def _small_limits(self):
        with (
            patch.object(auth_throttle, "DISTINCT_KEYS", 3),
            patch.object(auth_throttle, "WINDOW_S", 600),
            patch.object(auth_throttle, "LOCKOUT_S", 900),
        ):
            yield

    def _bad_token(self, n: int) -> dict[str, str]:
        return {"Authorization": f"Bearer {NOT_A_TOKEN}{n}"}

    def test_expired_tokens_never_lock_an_address(self, client, test_user):
        """Several people behind one office address whose sign-ins ran out
        together each send a different dead token. Counting those would
        lock every API client in the office out."""
        for n in range(10):
            assert client.get("/v1/me", headers=self._bad_token(n)).status_code == 401
        assert client.get("/v1/me", headers=auth_header()).status_code == 200

    def test_a_key_beside_a_bad_token_is_counted_as_a_key(self, client, test_user):
        """A token-shaped header is no way round the throttle."""
        for n in range(3):
            headers = {**self._bad_token(n), "x-api-key": f"sk-guess-{n}"}
            assert client.get("/v1/me", headers=headers).status_code == 401
        assert client.get("/v1/me", headers=auth_header()).status_code == 429

    def test_a_locked_address_still_reads_by_sign_in_and_tests_no_key(self, client, db_session, test_user):
        _add(db_session, username="oldtimer", api_key="legacy.key.value")
        for n in range(3):
            client.get("/v1/me", headers=auth_header(f"sk-guess-{n}"))
        assert client.get("/v1/me", headers=auth_header()).status_code == 429
        # Token-shaped or not, no key is looked up for a locked address.
        assert client.get("/v1/me", headers=auth_header("legacy.key.value")).status_code == 401
        assert client.get("/v1/me", headers={**self._bad_token(0), "x-api-key": OWN_KEY}).status_code == 429
        # A person's own sign-in is not a guess.
        assert client.get("/v1/me", headers=_sso()).status_code == 200


class TestCrossSite:
    """A browser attaches the session by itself, so a write by sign-in is
    refused when another site's page sent it. By key there is nothing
    ambient to abuse."""

    PATH = "/v1/notices/resolved"

    @pytest.mark.parametrize("extra", [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
    ])
    def test_a_write_by_sign_in_from_another_site_is_refused(self, client, db_session, test_user, extra):
        _violation(db_session)
        resp = client.post(self.PATH, headers={**_sso(), **extra})
        assert resp.status_code == 403 and "Cross-site" in resp.json()["detail"]
        # Nothing was resolved, and nothing is in the audit log.
        assert client.get("/v1/notices", headers=_sso()).json()["notices"] != []
        assert _audit(db_session) == []
        assert client.post("/v1/events", headers={**_sso(), **extra}, json=EVENT).status_code == 403

    @pytest.mark.parametrize("extra", [
        {},
        {"Sec-Fetch-Site": "same-origin"},
        {"Origin": "http://testserver"},
    ])
    def test_the_sites_own_page_and_a_script_get_through(self, client, test_user, extra):
        assert client.post(self.PATH, headers={**_sso(), **extra}).status_code == 200

    def test_a_read_is_never_refused_for_it(self, client, test_user):
        assert client.get("/v1/me", headers={**_sso(), "Sec-Fetch-Site": "cross-site"}).status_code == 200

    def test_a_key_is_not_a_cookie(self, client, test_user):
        """An application in a browser on another origin calls the API with
        its key (CORS allows that, without credentials)."""
        headers = {**auth_header(), "Sec-Fetch-Site": "cross-site", "Origin": "https://app.example"}
        assert client.post(self.PATH, headers=headers).status_code == 200


class TestAudit:
    PATH = "/v1/notices/resolved"

    def test_the_row_says_how_the_caller_signed_in(self, client, db_session, test_user):
        import json

        _violation(db_session)
        client.post(self.PATH, headers=_sso())
        _violation(db_session, "CC002")
        client.post(self.PATH, headers=auth_header())
        by_sso, by_key = _audit(db_session)
        assert (by_sso.actor, by_sso.route) == ("testuser", self.PATH)
        assert json.loads(by_sso.detail)["via"] == "sso"
        assert json.loads(by_key.detail)["via"] == "api_key"

    def test_a_refused_sign_in_cannot_fill_the_audit_log(self, client, db_session, test_user):
        client.post(self.PATH, headers={"Authorization": f"Bearer {NOT_A_TOKEN}"})
        client.post(self.PATH, headers=_sso(scopes=["other"]))
        assert _audit(db_session) == []

    def test_the_reads_are_not_in_it(self, client, db_session, test_user):
        for name in READS:
            client.get(f"/v1/{name}", headers=_sso())
        assert _audit(db_session) == []
