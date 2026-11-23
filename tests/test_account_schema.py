"""The shapes in ``app/services/account_schema.py`` are the account
endpoints' answers.

``openapi.json`` shows those shapes to whoever builds a tool or a frontend,
and they are documentation: no route filters its answer through one. So
this is what keeps them true. Every real answer, for an API key and for an
SSO sign-in, with and without the optional parts, must validate against its
shape and come back **unchanged**: a key the answer has and the shape does
not is dropped by the validation and fails the comparison, a key the shape
requires and the answer lacks fails the validation.

It is also the guard on the contract: a key cannot leave an answer without
this failing, since the shape still requires it."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlmodel import Session

from app.core import api_docs
from app.models.schema import AppOwner, ClientViolation, UsageLog, User
from app.services import account_schema as shapes
from tests.conftest import auth_header, earlier_today, web_auth_header

# app.routers.* are imported inside the tests, never at module level.

READS = {
    "/v1/me": shapes.Account,
    "/me": shapes.Account,
    "/v1/status": shapes.Status,
    "/v1/usage": shapes.Usage,
    "/v1/service-accounts": shapes.ServiceAccounts,
    "/v1/notices": shapes.Notices,
    "/v1/settings": shapes.Settings,
}
EVENT = {"event": "installed", "tool": "setup-tool", "tool_version": "1.0", "component": "claude-code", "os": "linux"}


def _fits(shape, body) -> None:
    """``body`` is exactly a ``shape``: it validates, and nothing in it is
    beyond what the shape declares."""
    assert TypeAdapter(shape).validate_python(body) == body


def _no_nulls(value) -> bool:
    if isinstance(value, dict):
        return all(_no_nulls(v) for v in value.values())
    if isinstance(value, list):
        return all(_no_nulls(v) for v in value)
    return value is not None


def _add(db_session: Session, **fields) -> User:
    user = User(password_hash="x", **fields)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def busy_account(db_session, test_user) -> User:
    """``testuser`` with everything an answer can carry: a department, cloud
    access, usage on three backends (one row overflowed, one sent by an
    application), an owned service account that acts for its users, one
    withheld for another department, an open notice and a model change."""
    from app.services import model_changes

    test_user.org_code, test_user.can_use_azure, test_user.can_use_bedrock = "RD1", True, True
    test_user.api_key_created_at = datetime.now(timezone.utc).replace(tzinfo=None)
    test_user.cloud_daily_limit_usd = 5.0
    db_session.add(test_user)
    hr_chat = _add(
        db_session, username="hr-chat", api_key="sk-hr-chat-secret", is_service_account=True, acts_for_users=True,
        concurrency_waived=True, org_code="RD1", description="HR chat page", daily_limit_usd=50.0,
    )
    etl = _add(
        db_session, username="fin-etl", api_key="sk-fin-etl-secret", is_service_account=True,
        concurrency_waived=True, org_code="FIN2", daily_limit_usd=20.0,
    )
    alice = _add(db_session, username="alice", display_name="Alice", api_key="sk-alice", daily_limit_usd=10.0)
    db_session.add_all([AppOwner(app_id=hr_chat.id, owner_id=test_user.id), AppOwner(app_id=etl.id, owner_id=test_user.id)])

    def spend(user, amount, backend="vllm", model="test-llm", **kw):
        db_session.add(UsageLog(
            user_id=user.id, model=model, model_type="llm", input_tokens=1000, output_tokens=500,
            cost_usd=Decimal(amount), endpoint="/v1/chat/completions", backend=backend,
            created_at=earlier_today(), **kw,
        ))

    spend(test_user, "1.25")
    spend(test_user, "0.5", backend="azure", model="azure-gpt-4")
    spend(test_user, "0.4", backend="bedrock", model="bedrock-claude", overflow=True)
    spend(test_user, "0.75", via_user_id=hr_chat.id)
    spend(alice, "1.5", via_user_id=hr_chat.id)
    spend(hr_chat, "3.0")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.add(ClientViolation(
        rule_id="CC001", username="testuser", first_seen_at=now, last_seen_at=now, client_version="2.1.280",
    ))
    db_session.commit()
    model_changes.save(db_session, model_changes.clean_fields({
        "title": "Old model retires", "message": "It is switched off next month.", "renames": "old-llm -> test-llm",
    }), by="admin", shown=True)
    return test_user


def _sign_ins():
    return {"an API key": auth_header(), "an SSO sign-in": web_auth_header(sub="testuser", scopes=["admin"])}


class TestEveryAnswerFitsItsShape:
    @pytest.mark.parametrize("path", list(READS))
    def test_a_full_account(self, client, busy_account, path):
        for how, headers in _sign_ins().items():
            resp = client.get(path, headers=headers)
            assert resp.status_code == 200, (how, resp.text)
            assert _no_nulls(resp.json()), how
            _fits(READS[path], resp.json())

    @pytest.mark.parametrize("path", list(READS))
    def test_a_new_account_with_nothing(self, client, test_user, path):
        for how, headers in _sign_ins().items():
            resp = client.get(path, headers=headers)
            assert resp.status_code == 200, (how, resp.text)
            _fits(READS[path], resp.json())

    def test_the_parts_that_come_and_go_were_really_there(self, client, busy_account):
        """The fixture is only worth something if the optional keys showed
        up: a shape is not tested by an answer that leaves them all out."""
        by_key, signed_in = (
            {path: client.get(path, headers=headers).json() for path in READS} for headers in _sign_ins().values()
        )
        me = signed_in["/v1/me"]
        assert {"org_code", "is_admin", "cloud"} <= set(me) and "created_at" in me["api_key"]
        assert "is_admin" not in by_key["/v1/me"]
        usage = by_key["/v1/usage"]
        assert any(row.get("overflow") for row in usage["month"]["models"])
        assert {s["source"] for s in usage["month"]["sources"]} == {"own_key", "service_account"}
        assert any("description" in s for s in usage["month"]["sources"])
        accounts = signed_in["/v1/service-accounts"]
        [account] = accounts["service_accounts"]
        assert {"description", "department", "sent_for"} <= set(account) and "top_users" in accounts
        assert accounts["withheld"] == [{"username": "fin-etl", "department": "FIN2"}]
        assert "sent_for" not in by_key["/v1/service-accounts"]["service_accounts"][0]
        notices = by_key["/v1/notices"]
        assert {"message", "client_version", "patch"} <= set(notices["notices"][0]) and "patch" in notices
        assert notices["model_changes"]["changes"][0]["id"].startswith("MC")
        assert any("display_name" in m for m in by_key["/v1/status"]["models"])

    def test_a_model_that_is_down_and_one_that_reports_its_load(self, client, test_user):
        with patch("app.services.model_catalog.is_alive", side_effect=lambda url: "mock-llm" not in url), \
                patch("app.services.model_catalog.get_metrics", return_value={"running": 4, "waiting": 2}):
            body = client.get("/v1/status", headers=auth_header()).json()
        rows = {m["id"]: m for m in body["models"]}
        assert rows["test-llm"]["state"] == "down" and rows["test-vlm"]["running"] == 4
        _fits(shapes.Status, body)

    def test_a_concurrency_limit_with_off_peak_hours(self, client, test_user):
        with patch("app.services.account_view.get_concurrency_settings", return_value=("enforce", 4)), \
                patch("app.services.account_view.get_concurrency_offpeak",
                      return_value={"start": "22:00", "end": "06:00", "weekends": True}):
            body = client.get("/v1/me", headers=auth_header()).json()
        assert body["concurrency"]["limit"] == 4 and "offpeak" in body["concurrency"]
        _fits(shapes.Account, body)

    def test_the_two_writes(self, client, busy_account):
        for headers in _sign_ins().values():
            resolved = client.post("/v1/notices/resolved", headers=headers, json={"ids": ["CC001"]})
            assert resolved.status_code == 200
            _fits(shapes.NoticesResolved, resolved.json())
            recorded = client.post("/v1/events", headers=headers, json=EVENT)
            assert recorded.status_code == 200
            _fits(shapes.EventRecorded, recorded.json())


class TestTheOtherAnswersOfTheGatewaysOwn:
    """Outside the account endpoints the gateway has a few answers of its
    own shape (``app/core/api_docs.py``): the model lists, a token count,
    the versions, a key for a token, and what the dashboard's actions
    answer. The same rule: the real answer is exactly its shape."""

    @pytest.mark.parametrize("path", ["/v1/models", "/models", "/azure/v1/models", "/aws/v1/models"])
    def test_the_model_lists(self, client, db_session, test_user, path):
        test_user.can_use_azure = test_user.can_use_bedrock = True
        db_session.add(test_user)
        db_session.commit()
        body = client.get(path, headers=auth_header()).json()
        assert body["data"] and any("display_name" in entry for entry in client.get("/v1/models", headers=auth_header()).json()["data"])
        _fits(api_docs.ModelList, body)

    def test_the_versions(self, client):
        _fits(api_docs.Versions, client.get("/v1/version").json())

    def test_a_deprecated_version_fits_too(self):
        from app.core import api_versions

        versions = (
            api_versions.ApiVersion("v1", api_versions.DEPRECATED, sunset="2027-06-30", successor="v2", note="Move."),
            api_versions.ApiVersion("v2", api_versions.STABLE),
        )
        _fits(api_docs.Versions, api_versions.payload(versions))

    def test_a_key_for_a_token(self, client, test_user):
        resp = client.post("/auth/api-key", headers=web_auth_header(sub="testuser"))
        assert resp.status_code == 200
        _fits(api_docs.ApiKeyForToken, resp.json())

    def test_what_the_dashboard_answers(self, client, busy_account, db_session):
        from sqlmodel import select

        sso = {**web_auth_header(sub="testuser"), "Accept": "application/json"}
        hr_chat = db_session.exec(select(User).where(User.username == "hr-chat")).one()
        _fits(api_docs.ApiKeyShown, client.get("/dashboard/api/api-key", headers=sso).json())
        _fits(api_docs.ApiKeyShown, client.get(f"/dashboard/api/service-accounts/{hr_chat.id}/api-key", headers=sso).json())
        change = client.get("/v1/notices", headers=sso).json()["model_changes"]["changes"][0]["id"]
        _fits(api_docs.ModelChangeApplied, client.post(f"/dashboard/model-changes/{change}/applied", headers=sso).json())
        _fits(api_docs.NoticeResolved, client.post("/dashboard/client-rules/CC001/resolved", headers=sso).json())
        _fits(api_docs.AllNoticesResolved, client.post("/dashboard/client-rules/all/resolved", headers=sso).json())
        _fits(api_docs.KeyRegenerated, client.post(f"/dashboard/app/{hr_chat.id}/refresh-key", headers=sso).json())
        _fits(api_docs.KeyRegenerated, client.post("/dashboard/refresh-key", headers=sso).json())

    def test_they_are_in_the_document(self, client):
        spec = client.get("/openapi.json").json()
        named = {
            ("get", "/v1/models"): "ModelList", ("get", "/azure/v1/models"): "ModelList",
            ("get", "/aws/v1/models"): "ModelList", ("get", "/v1/version"): "Versions",
            ("post", "/auth/api-key"): "ApiKeyForToken", ("get", "/dashboard/api/api-key"): "ApiKeyShown",
            ("post", "/dashboard/refresh-key"): "KeyRegenerated",
            ("post", "/v1/messages/count_tokens"): "TokenCount",
        }
        for (method, path), name in named.items():
            schema = spec["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
            assert schema == {"$ref": f"#/components/schemas/{name}"}, (method, path)


class TestTheCheckItselfWorks:
    """``_fits`` must notice a shape and an answer that differ, both ways."""

    def test_a_key_the_shape_does_not_have(self, client, test_user):
        body = client.get("/v1/me", headers=auth_header()).json()
        with pytest.raises(AssertionError):
            _fits(shapes.Account, {**body, "something_new": 1})

    def test_a_key_the_shape_requires(self, client, test_user):
        body = client.get("/v1/me", headers=auth_header()).json()
        body.pop("legacy_key")
        with pytest.raises(ValidationError):
            _fits(shapes.Account, body)

    def test_a_null_is_never_a_value(self, client, test_user):
        body = client.get("/v1/me", headers=auth_header()).json()
        with pytest.raises(ValidationError):
            _fits(shapes.Account, {**body, "org_code": None})


class TestTheDocument:
    def test_every_account_answer_has_its_schema_in_openapi(self, client):
        spec = client.get("/openapi.json").json()
        named = {
            ("get", "/v1/me"): "Account", ("get", "/me"): "Account", ("get", "/v1/status"): "Status",
            ("get", "/v1/usage"): "Usage", ("get", "/v1/service-accounts"): "ServiceAccounts",
            ("get", "/v1/notices"): "Notices", ("get", "/v1/settings"): "Settings",
            ("post", "/v1/notices/resolved"): "NoticesResolved", ("post", "/v1/events"): "EventRecorded",
        }
        for (method, path), name in named.items():
            schema = spec["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
            assert schema == {"$ref": f"#/components/schemas/{name}"}, (method, path)
            assert spec["components"]["schemas"][name]["type"] == "object"

    def test_no_key_of_an_answer_is_nullable(self, client):
        """A key without a value is left out, so the document never offers
        ``null`` as a value of an answer."""
        import json

        spec = client.get("/openapi.json").json()["components"]["schemas"]
        answers = [
            name for name in dir(shapes)
            if isinstance(getattr(shapes, name), type) and hasattr(getattr(shapes, name), "__required_keys__")
        ]
        assert len(answers) > 25
        for name in answers:
            assert '"null"' not in json.dumps(spec[name]), name

    def test_optional_keys_are_optional_in_the_document(self, client):
        spec = client.get("/openapi.json").json()["components"]["schemas"]
        assert not {"is_admin", "org_code", "cloud"} & set(spec["Account"]["required"])
        assert {"username", "daily", "backends", "site"} <= set(spec["Account"]["required"])
        assert "sent_for" not in spec["ServiceAccount"]["required"]
        assert spec["Concurrency"]["required"] == ["mode"]

    def test_the_request_bodies_state_the_codes_limits(self, client):
        from app.services import client_events

        paths = client.get("/openapi.json").json()["paths"]
        event = paths["/v1/events"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert event["properties"]["event"]["enum"] == list(client_events.EVENTS)
        assert event["properties"]["os"]["enum"] == list(client_events.OS_FAMILIES)
        assert event["properties"]["kind"]["enum"] == list(client_events.KINDS)
        assert set(event["required"]) == {"event", "tool", "tool_version", "component", "os"}
        resolved = paths["/v1/notices/resolved"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        ids = next(option for option in resolved["properties"]["ids"]["anyOf"] if option.get("type") == "array")
        assert ids["maxItems"] == 50 and ids["items"]["maxLength"] == 20

    def test_the_shapes_filter_nothing(self):
        """They are in ``responses=``, never ``response_model=``: a shape
        that lags behind an answer must not be able to drop a key from it."""
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent / "app" / "routers" / "account_api.py").read_text()
        assert "response_model" not in source and source.count("_answers(account_schema.") == 9
