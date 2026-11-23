"""The dashboard as data: ``/dashboard/api/*`` (``app/routers/dashboard``).

What the dashboard page shows, as JSON for a frontend of its own, signed in
by SSO. ``me`` / ``status`` / ``usage`` / ``service-accounts`` / ``notices``
answer what the account endpoints of the same name answer by API key, from
the same function (``app/services/account_view.py``), plus what only a signed-in person may see. A key is in
none of them: the two routes that show one do nothing else and are in the
audit log."""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from app.models.schema import AppOwner, AuditLog, ClientViolation, UsageLog, User
from tests.conftest import auth_header, earlier_today, web_auth_header

# app.routers.* are imported inside the tests, never at module level.

READS = ["me", "status", "usage", "service-accounts", "notices"]
OWN_KEY = "sk-testkey123"
SERVICE_KEY = "sk-hr-chat-secret"


def _no_nulls(value) -> bool:
    if isinstance(value, dict):
        return all(_no_nulls(v) for v in value.values())
    if isinstance(value, list):
        return all(_no_nulls(v) for v in value)
    return value is not None


def _sso(sub: str = "testuser", **kw) -> dict[str, str]:
    return {**web_auth_header(sub=sub, **kw), "Accept": "application/json"}


def _get(client, name: str, sub: str = "testuser", **params) -> dict:
    resp = client.get(f"/dashboard/api/{name}", headers=_sso(sub), params=params)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert _no_nulls(body), body
    return body


def _by_key(client, name: str, **params) -> dict:
    resp = client.get(f"/v1/{name}", headers=auth_header(), params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _add(db_session: Session, **fields) -> User:
    user = User(**fields)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _spend(db_session: Session, user: User, amount: str, backend: str = "vllm", model: str = "test-llm", **kw) -> None:
    kw.setdefault("created_at", earlier_today())
    db_session.add(UsageLog(
        user_id=user.id, model=model, model_type="llm", input_tokens=1000, output_tokens=500,
        cost_usd=Decimal(amount), endpoint="/v1/chat/completions", backend=backend, **kw,
    ))
    db_session.commit()


def _violation(db_session: Session, rule_id: str = "CC001", username: str = "testuser") -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.add(ClientViolation(rule_id=rule_id, username=username, first_seen_at=now, last_seen_at=now))
    db_session.commit()


def _audit(db_session: Session) -> list[AuditLog]:
    db_session.expire_all()
    return list(db_session.exec(select(AuditLog).order_by(AuditLog.id)).all())


@pytest.fixture()
def owner(db_session, test_user) -> User:
    """``testuser``, in department RD1."""
    test_user.org_code = "RD1"
    db_session.add(test_user)
    db_session.commit()
    return test_user


@pytest.fixture()
def hr_chat(db_session, owner) -> User:
    """A service account for RD1 that acts for its users, owned by ``owner``."""
    account = _add(
        db_session, username="hr-chat", api_key=SERVICE_KEY, is_service_account=True,
        acts_for_users=True, concurrency_waived=True, org_code="RD1", description="HR chat page",
    )
    db_session.add(AppOwner(app_id=account.id, owner_id=owner.id))
    db_session.commit()
    return account


class TestSignIn:
    @pytest.mark.parametrize("name", READS + ["api-key", "model-breakdown"])
    def test_needs_the_sso_sign_in_and_an_api_key_is_not_one(self, client, test_user, name):
        assert client.get(f"/dashboard/api/{name}").status_code in (401, 403)
        assert client.get(f"/dashboard/api/{name}", headers=auth_header()).status_code in (401, 403)

    def test_every_dashboard_route_signs_in_by_sso(self):
        from fastapi.routing import iter_route_contexts

        from app.core.auth import get_web_user
        from app.routers import dashboard

        def walk(dependant) -> bool:
            return dependant.call is get_web_user or any(walk(d) for d in dependant.dependencies)

        contexts = list(iter_route_contexts(dashboard.router.routes))
        paths = {context.path for context in contexts}
        # The page, its data and its actions are all here, and nowhere else.
        assert {"/dashboard", "/dashboard/api/me", "/dashboard/refresh-key", "/dashboard/api/api-key"} <= paths
        assert all(path == "/dashboard" or path.startswith("/dashboard/") for path in paths)
        assert [context.path for context in contexts if not walk(context.dependant)] == []

    def test_no_dashboard_route_is_left_in_the_other_pages_module(self):
        from fastapi.routing import iter_route_contexts

        from app.routers import web_ui

        assert sorted(context.path for context in iter_route_contexts(web_ui.router.routes)) == ["/", "/setup"]


class TestTheSameAsByKey:
    """One shape for a client, whichever way it signed in."""

    def test_status(self, client, test_user):
        assert _get(client, "status") == _by_key(client, "status")

    def test_usage(self, client, db_session, test_user):
        _spend(db_session, test_user, "1.25")
        _spend(db_session, test_user, "0.50", backend="azure", model="test-azure-llm")
        assert _get(client, "usage", days=3) == _by_key(client, "usage", days=3)
        assert len(_get(client, "usage")["days"]) == 7

    @pytest.mark.parametrize("days", [0, 91])
    def test_usage_takes_the_same_days(self, client, test_user, days):
        assert client.get("/dashboard/api/usage", headers=_sso(), params={"days": days}).status_code == 422

    def test_notices(self, client, db_session, test_user):
        _violation(db_session)
        body = _get(client, "notices")
        from app.services import client_rules

        assert body.pop("quiet_days") == client_rules.RESOLVED_QUIET_DAYS
        assert body == _by_key(client, "notices")
        assert [n["id"] for n in body["notices"]] == ["CC001"]
        assert body["model_changes"] == {"changes": [], "retiring": [], "patch": {"models": {}}}

    def test_me_is_v1_me_and_more(self, client, db_session, test_user):
        _spend(db_session, test_user, "2.00")
        body = _get(client, "me")
        by_key = _by_key(client, "me")
        extra = {"api_key", "site"}
        assert set(body) == set(by_key) | extra
        # The clock moves between the two calls; everything else is the same.
        body["daily"].pop("resets_in_seconds"), by_key["daily"].pop("resets_in_seconds")
        assert {k: v for k, v in body.items() if k not in extra} == by_key
        assert body["daily"]["used_usd"] == 2.0

    def test_service_accounts_are_the_same_rows_and_more(self, client, db_session, hr_chat):
        _spend(db_session, hr_chat, "3.00")
        [row] = _get(client, "service-accounts")["service_accounts"]
        [by_key] = _by_key(client, "service-accounts")["service_accounts"]
        assert {k: v for k, v in row.items() if k not in ("id", "api_key_masked")} == by_key
        assert row["month"]["cost_usd"] == 3.0 and row["description"] == "HR chat page"


class TestWhatOnlyTheSignedInPersonSees:
    def test_an_admin_is_one_by_the_token(self, client, test_user):
        assert _get(client, "me")["is_admin"] is False
        body = client.get("/dashboard/api/me", headers=_sso(scopes=["admin"])).json()
        assert body["is_admin"] is True
        # By API key nobody is: the scope exists on the web UI only.
        assert _by_key(client, "me")["is_admin"] is False

    def test_the_department_and_the_masked_key(self, client, owner):
        body = _get(client, "me")
        assert body["org_code"] == "RD1"
        assert body["api_key"]["masked"] not in ("", OWN_KEY) and body["api_key"]["masked"].endswith(OWN_KEY[-4:])
        assert body["api_key"]["created_at"].endswith("+08:00")

    def test_no_department_is_left_out(self, client, test_user):
        assert "org_code" not in _get(client, "me")

    def test_the_site(self, client, test_user):
        site = _get(client, "me")["site"]
        assert site["title"] and site["api_base"].endswith("/v1")
        assert isinstance(site["setup_page"], bool) and isinstance(site["installer"], bool)
        # Only the links an admin set: an empty one is left out.
        assert all(site["links"].values())

    def test_the_installer_follows_the_setup_page_switch(self, client, test_user, monkeypatch):
        monkeypatch.setattr("app.routers.dashboard.api.setup_page_enabled", lambda: False)
        site = _get(client, "me")["site"]
        assert site["setup_page"] is False and site["installer"] is False

    def test_a_service_account_has_its_id_and_who_it_was_used_for(self, client, db_session, hr_chat):
        alice = _add(db_session, username="alice", display_name="Alice", api_key="sk-alice")
        db_session.add(UsageLog(
            user_id=alice.id, model="test-llm", model_type="llm", input_tokens=1000, output_tokens=500,
            cost_usd=Decimal("1.5"), endpoint="/v1/chat/completions", backend="vllm", via_user_id=hr_chat.id,
            created_at=earlier_today(),
        ))
        db_session.commit()
        body = _get(client, "service-accounts")
        [row] = body["service_accounts"]
        assert row["id"] == hr_chat.id
        assert row["sent_for"] == {"users": 1, "rows": [{
            "user": "alice", "display_name": "Alice", "backend": "onprem",
            "requests": 1, "input_tokens": 1000, "output_tokens": 500, "cost_usd": 1.5,
        }]}
        assert body["top_users"] >= 1 and body["withheld"] == []
        # By key the owner is not told who uses the application.
        assert "sent_for" not in _by_key(client, "service-accounts")["service_accounts"][0]

    def test_nothing_sent_for_anyone_has_no_flow(self, client, hr_chat):
        assert "sent_for" not in _get(client, "service-accounts")["service_accounts"][0]

    def test_the_users_beyond_the_top_are_one_row_without_a_name(self, client, db_session, hr_chat):
        from app.routers.dashboard.api import _sent_for

        flow = {"users": 12, "rows": [
            {"user": "alice", "display_name": "", "backend": "bedrock", "requests": 2,
             "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.2},
            {"user": "", "display_name": "", "backend": "vllm", "others": 2, "requests": 3,
             "input_tokens": 30, "output_tokens": 15, "cost_usd": 0.1},
        ]}
        named, rest = _sent_for(flow)["rows"]
        assert named["user"] == "alice" and "display_name" not in named and named["backend"] == "aws"
        assert "user" not in rest and rest["others"] == 2 and rest["backend"] == "onprem"

    def test_an_account_for_another_department_is_named_and_not_shown(self, client, db_session, owner, hr_chat):
        owner.org_code = "QA2"
        db_session.add(owner)
        db_session.commit()
        body = _get(client, "service-accounts")
        assert body["service_accounts"] == []
        assert body["withheld"] == [{"username": "hr-chat", "department": "RD1"}]


class TestKeys:
    @pytest.mark.parametrize("name", READS)
    def test_no_data_route_holds_a_key(self, client, db_session, hr_chat, name):
        _violation(db_session)
        text = client.get(f"/dashboard/api/{name}", headers=_sso()).text
        assert OWN_KEY not in text and SERVICE_KEY not in text

    def test_the_own_key_is_one_call_never_cached_and_audited(self, client, db_session, test_user):
        resp = client.get("/dashboard/api/api-key", headers=_sso())
        assert resp.status_code == 200
        assert resp.json() == {"object": "api_key", "username": "testuser", "api_key": OWN_KEY}
        assert resp.headers["cache-control"] == "no-store"
        [entry] = _audit(db_session)
        assert (entry.actor, entry.method, entry.route) == ("testuser", "GET", "/dashboard/api/api-key")
        assert OWN_KEY not in entry.detail

    def test_the_other_reads_are_not_in_the_audit_log(self, client, db_session, test_user):
        for name in READS:
            _get(client, name)
        assert _audit(db_session) == []

    def test_an_owner_reads_a_service_accounts_key(self, client, db_session, hr_chat):
        resp = client.get(f"/dashboard/api/service-accounts/{hr_chat.id}/api-key", headers=_sso())
        assert resp.status_code == 200
        assert resp.json() == {"object": "api_key", "username": "hr-chat", "api_key": SERVICE_KEY}
        assert resp.headers["cache-control"] == "no-store"
        [entry] = _audit(db_session)
        assert entry.route == "/dashboard/api/service-accounts/{app_id}/api-key"
        assert json.loads(entry.detail)["target"] == "hr-chat"
        assert SERVICE_KEY not in entry.detail

    def test_only_an_owner_in_its_department(self, client, db_session, owner, hr_chat):
        _add(db_session, username="mallory", api_key="sk-mallory")
        url = f"/dashboard/api/service-accounts/{hr_chat.id}/api-key"
        refused = client.get(url, headers=_sso("mallory"))
        assert refused.status_code == 403 and SERVICE_KEY not in refused.text
        assert client.get("/dashboard/api/service-accounts/999999/api-key", headers=_sso()).status_code == 404
        # A person's account is nobody's service account.
        assert client.get(f"/dashboard/api/service-accounts/{owner.id}/api-key", headers=_sso("mallory")).status_code == 403
        # The owner moved to another department: the same refusal as regenerating it.
        owner.org_code = "QA2"
        db_session.add(owner)
        db_session.commit()
        moved = client.get(url, headers=_sso())
        assert moved.status_code == 403 and SERVICE_KEY not in moved.text
        assert client.post(f"/dashboard/app/{hr_chat.id}/refresh-key", headers=_sso()).status_code == 403

    def test_regenerating_shows_in_the_next_read(self, client, test_user):
        new_key = client.post("/dashboard/refresh-key", headers=_sso()).json()["api_key"]
        assert new_key != OWN_KEY
        assert client.get("/dashboard/api/api-key", headers=_sso()).json()["api_key"] == new_key


class TestTheActionsTakeWhatTheApiGives:
    def test_a_notice_is_resolved_by_its_id(self, client, db_session, test_user):
        _violation(db_session)
        [notice] = _get(client, "notices")["notices"]
        resp = client.post(f"/dashboard/client-rules/{notice['id']}/resolved", headers=_sso())
        assert resp.status_code == 200 and resp.json() == {"ok": True, "rule_id": "CC001"}
        assert _get(client, "notices")["notices"] == []

    def test_a_model_change_is_applied_by_the_id_it_is_shown_under(self, client, db_session, test_user):
        from app.services import model_changes

        change = model_changes.save(db_session, model_changes.clean_fields({
            "title": "Old model retires", "message": "It is switched off next month.", "renames": "old-llm -> test-llm",
        }), by="admin", shown=True)
        [shown] = _get(client, "notices")["model_changes"]["changes"]
        assert shown["id"] == f"MC{change.id}" and shown["applied"] is False
        resp = client.post(f"/dashboard/model-changes/{shown['id']}/applied", headers=_sso())
        assert resp.status_code == 200 and resp.json() == {"ok": True, "id": change.id}
        assert _get(client, "notices")["model_changes"]["changes"][0]["applied"] is True
        # The page's own form still posts the number.
        assert client.post(f"/dashboard/model-changes/{change.id}/applied", headers=_sso()).status_code == 200

    @pytest.mark.parametrize("ref", ["MC999999", "nope", "0x1"])
    def test_an_unknown_change_is_a_404(self, client, test_user, ref):
        assert client.post(f"/dashboard/model-changes/{ref}/applied", headers=_sso()).status_code == 404


class TestThePageAndTheApiAgree:
    def test_the_page_is_built_from_the_same_view(self, client, db_session, test_user):
        _spend(db_session, test_user, "4.00")
        page = client.get("/dashboard", headers=web_auth_header(sub="testuser"))
        assert page.status_code == 200
        me = _get(client, "me")
        assert f"{me['daily']['used_usd']:.2f}" in page.text
        for model in _get(client, "status")["models"]:
            assert model["id"] in page.text

    def test_the_view_knows_no_request_and_no_template(self):
        from app.services import dashboard_view

        source = inspect.getsource(dashboard_view)
        for name in ("fastapi", "Request", "TemplateResponse", "app.routers"):
            assert name not in source, name

    def test_the_handlers_that_read_the_database_are_plain_def(self):
        from app.routers.dashboard import actions, api, page

        for fn in (
            page.dashboard,
            api.usage, api.service_accounts, api.notices, api.model_breakdown_api, api.service_account_api_key,
            actions.refresh_own_key, actions.refresh_owned_app_key, actions.client_rules_resolved_all,
        ):
            assert not inspect.iscoroutinefunction(fn), fn.__name__
