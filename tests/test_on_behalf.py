"""A service account that acts for its users (``X-On-Behalf-Of``).

An application people sign in to holds one service account key. With
``users.acts_for_users`` on, every request it sends names the person it is
for, and is then that person's request: their limit, their access, their
bill, with the service account recorded as the way it came in
(``usage_logs.via_user_id``). ``GET /v1/models`` then lists the models that
person sees with their own key."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, select

from app.models.schema import AppOwner, ClientVersion, UsageLog, User
from app.services import stats, usage_rollup
from tests.conftest import (
    _test_engine,
    auth_header,
    earlier_today,
    make_httpx_response,
    web_auth_header,
)

HEADER = "X-On-Behalf-Of"
APP_KEY = "sk-chat-app"
_CHAT = {"model": "test-llm", "messages": [{"role": "user", "content": "Hi"}]}
_MESSAGES = {"model": "test-llm", "max_tokens": 16, "messages": [{"role": "user", "content": "Hi"}]}
_OK_CHAT = {
    "id": "c1",
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "from on-prem"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
}
_OK_AZURE = {
    "id": "resp_1",
    "status": "completed",
    "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "from azure"}]}],
    "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
}


def _add(db_session: Session, **fields) -> User:
    user = User(**fields)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def chat_app(db_session) -> User:
    """A service account that acts for its users."""
    return _add(
        db_session, username="chat-app", api_key=APP_KEY, is_service_account=True,
        concurrency_waived=True, acts_for_users=True, daily_limit_usd=50.0,
    )


@pytest.fixture()
def alice(db_session) -> User:
    """A person without cloud access."""
    return _add(db_session, username="alice", api_key="sk-alice", daily_limit_usd=10.0)


def _for(name: str | None, key: str = APP_KEY) -> dict[str, str]:
    return {**auth_header(key), **({HEADER: name} if name is not None else {})}


def _downstream(client):
    """Answer by backend, recording which one was called."""
    calls: list[str] = []

    async def fake_post(url, **kwargs):
        if "openai.azure.com" in url:
            calls.append("azure")
            return make_httpx_response(200, _OK_AZURE)
        calls.append("vllm")
        return make_httpx_response(200, _OK_CHAT)

    return patch.object(client.__httpx_mock__, "post", side_effect=fake_post), calls


def _rows(user_id: int | None = None) -> list[UsageLog]:
    with Session(_test_engine) as s:
        stmt = select(UsageLog).order_by(UsageLog.id)
        if user_id is not None:
            stmt = stmt.where(UsageLog.user_id == user_id)
        return list(s.exec(stmt).all())


def _chat(client, headers, body=None):
    mock, calls = _downstream(client)
    with mock:
        resp = client.post("/v1/chat/completions", json=body or _CHAT, headers=headers)
    return resp, calls


def _sent_for(db_session, user: User, via: User | None, cost: str, **kwargs) -> None:
    kwargs.setdefault("created_at", earlier_today())
    db_session.add(UsageLog(
        user_id=user.id, model="test-llm", model_type="llm", input_tokens=1000, output_tokens=500,
        cost_usd=Decimal(cost), endpoint="/v1/chat/completions", backend="vllm",
        via_user_id=via.id if via is not None else 0, **kwargs,
    ))
    db_session.commit()


# ── Whose request it is ──

class TestTheRequestIsTheUsers:
    def test_billed_to_the_named_user_through_the_service_account(self, client, chat_app, alice):
        resp, calls = _chat(client, _for("alice"))
        assert resp.status_code == 200, resp.text
        assert calls == ["vllm"]
        [row] = _rows()
        assert (row.user_id, row.via_user_id) == (alice.id, chat_app.id)
        assert row.cost_usd > 0
        assert _rows(chat_app.id) == []

    def test_the_users_daily_limit_decides(self, client, db_session, chat_app, alice):
        _sent_for(db_session, alice, None, "10")  # alice has spent her $10
        resp, calls = _chat(client, _for("alice"))
        assert resp.status_code == 429 and "Daily spending limit" in resp.json()["detail"]
        assert calls == []

    def test_the_service_accounts_own_limit_does_not(self, client, db_session, chat_app, alice):
        chat_app.daily_limit_usd = 0.0
        db_session.add(chat_app)
        db_session.commit()
        resp, _ = _chat(client, _for("alice"))
        assert resp.status_code == 200

    def test_what_went_through_it_counts_toward_the_users_limit(self, client, db_session, chat_app, alice):
        _sent_for(db_session, alice, chat_app, "10")
        # ... for her own key too: it is one budget.
        resp, _ = _chat(client, auth_header("sk-alice"))
        assert resp.status_code == 429

    def test_the_users_cloud_access_decides(self, client, chat_app, alice, test_user):
        body = {**_CHAT, "model": "azure-gpt-4"}
        # alice has no Azure access: served on-prem like any alias she cannot use.
        resp, calls = _chat(client, _for("alice"), body)
        assert resp.status_code == 200 and calls == ["vllm"]
        # testuser has: the application does not need the access itself.
        assert not chat_app.can_use_azure
        resp, calls = _chat(client, _for("testuser"), body)
        assert resp.status_code == 200 and calls == ["azure"]
        assert [(r.backend, r.via_user_id) for r in _rows(test_user.id)] == [("azure", chat_app.id)]

    def test_the_user_reads_their_budget_with_their_own_key(self, client, db_session, chat_app, alice):
        """What the application sent for the user is on the user's budget,
        and the user reads it themselves: the application cannot."""
        _sent_for(db_session, alice, chat_app, "10")
        body = client.get("/v1/me", headers=auth_header("sk-alice")).json()
        assert body["username"] == "alice"
        assert body["daily"]["limit_usd"] == 10.0 and body["daily"]["remaining_usd"] == 0.0
        assert client.get("/v1/me", headers=_for("alice")).status_code == 403

    def test_the_key_use_is_the_service_accounts(self, client, db_session, chat_app, alice):
        from app.core import deps

        deps.reset_key_use_cache()
        _chat(client, _for("alice"))
        db_session.expire_all()
        assert db_session.get(User, chat_app.id).api_key_last_used_at is not None
        assert db_session.get(User, alice.id).api_key_last_used_at is None

    def test_the_address_check_is_the_service_accounts(self, client, chat_app, alice):
        with patch("app.core.deps.ip_allowlist.check") as check:
            _chat(client, _for("alice"))
        assert [call.args[1].username for call in check.call_args_list] == ["chat-app"]

    def test_client_rules_ignore_what_an_application_sends(self, client, db_session, chat_app, alice):
        ua = {"User-Agent": "claude-cli/2.1.280 (external, cli)"}
        mock, _ = _downstream(client)
        with mock:
            assert client.post("/v1/messages", json=_MESSAGES, headers={**_for("alice"), **ua}).status_code == 200
        # Not the person's client: no version is recorded under her name ...
        assert db_session.exec(select(ClientVersion)).all() == []
        with mock:
            client.post("/v1/messages", json=_MESSAGES, headers={**auth_header("sk-alice"), **ua})
        # ... as it is when she uses her own key.
        assert [v.username for v in db_session.exec(select(ClientVersion)).all()] == ["alice"]

    def test_langfuse_names_the_user_and_the_service_account(self, client, chat_app, alice):
        captured = []
        with patch("app.services.billing.get_langfuse", return_value=MagicMock()), \
             patch("app.services.billing.record_generation", captured.append):
            _chat(client, _for("alice"))
            _chat(client, auth_header("sk-alice"))
        assert [(rec.username, rec.service_account) for rec in captured] == [("alice", "chat-app"), ("alice", None)]


# ── What is refused ──

class TestRefused:
    def test_without_the_header(self, client, chat_app, alice):
        resp, calls = _chat(client, _for(None))
        assert resp.status_code == 400 and HEADER in resp.json()["detail"]
        assert calls == [] and _rows() == []

    def test_a_user_the_gateway_does_not_have(self, client, db_session, chat_app):
        resp, calls = _chat(client, _for("nobody"))
        assert resp.status_code == 403 and "no account" in resp.json()["detail"]
        assert calls == [] and _rows() == []
        # Nobody is provisioned on an application's word.
        assert db_session.exec(select(User).where(User.username == "nobody")).first() is None

    def test_a_disabled_user(self, client, db_session, chat_app, alice):
        alice.is_disabled = True
        db_session.add(alice)
        db_session.commit()
        resp, calls = _chat(client, _for("alice"))
        assert resp.status_code == 403 and "disabled" in resp.json()["detail"]
        assert calls == []

    def test_a_service_account_is_not_a_user(self, client, db_session, chat_app):
        _add(db_session, username="batch", api_key="sk-batch", is_service_account=True)
        for name in ("batch", "chat-app"):
            resp, calls = _chat(client, _for(name))
            assert resp.status_code == 403 and "service account" in resp.json()["detail"], name
            assert calls == []

    @pytest.mark.parametrize("value", ["alice, bob", "alice bob", " ", "a" * 129])
    def test_a_value_that_is_not_one_username(self, client, chat_app, alice, value):
        resp, calls = _chat(client, _for(value))
        assert resp.status_code == 400 and "exactly one username" in resp.json()["detail"]
        assert calls == []

    def test_the_header_sent_twice(self, client, chat_app, alice):
        mock, calls = _downstream(client)
        with mock:
            resp = client.post(
                "/v1/chat/completions", content=json.dumps(_CHAT),
                headers=[("Authorization", f"Bearer {APP_KEY}"), ("Content-Type", "application/json"),
                         (HEADER, "alice"), (HEADER, "testuser")],
            )
        assert resp.status_code == 400 and calls == []

    def test_a_bad_key_is_still_a_401(self, client, chat_app, alice):
        resp, _ = _chat(client, _for("alice", key="sk-wrong"))
        assert resp.status_code == 401

    def test_a_disabled_service_account(self, client, db_session, chat_app, alice):
        chat_app.is_disabled = True
        db_session.add(chat_app)
        db_session.commit()
        resp, calls = _chat(client, _for("alice"))
        assert resp.status_code == 403 and calls == []



# ── The application's own labels (X-Gateway-Tags) ──

TAGS = "X-Gateway-Tags"


class TestItsOwnLabels:
    """A service account that acts for its users may label a request in
    ``X-Gateway-Tags``: recorded in Langfuse with it, and nowhere else."""

    def _records(self, client, headers, body=None):
        captured = []
        with patch("app.services.billing.get_langfuse", return_value=MagicMock()), \
             patch("app.services.billing.record_generation", captured.append):
            resp, _ = _chat(client, headers, body)
        return resp, captured

    def test_they_go_to_langfuse(self, client, chat_app, alice):
        from app.services.observability import trace_metadata

        resp, [rec] = self._records(
            client, {**_for("alice"), TAGS: "module=invoice-ocr, ticket=4821,team=%E8%B2%A1%E5%8B%99"},
        )
        assert resp.status_code == 200
        assert (rec.username, rec.service_account) == ("alice", "chat-app")
        assert rec.request_tags == {"module": "invoice-ocr", "ticket": "4821", "team": "財務"}
        assert trace_metadata(rec)["tag_module"] == "invoice-ocr"

    def test_the_header_may_be_sent_more_than_once(self, client, chat_app, alice):
        captured = []
        mock, _ = _downstream(client)
        with mock, patch("app.services.billing.get_langfuse", return_value=MagicMock()), \
             patch("app.services.billing.record_generation", captured.append):
            resp = client.post(
                "/v1/chat/completions", json=_CHAT,
                headers=[*_for("alice").items(), (TAGS, "module=a"), (TAGS, "ticket=1")],
            )
        assert resp.status_code == 200 and captured[0].request_tags == {"module": "a", "ticket": "1"}

    def test_they_need_a_named_user_like_every_request(self, client, chat_app, alice):
        resp, calls = _chat(client, {**_for(None), TAGS: "module=a"})
        assert resp.status_code == 400 and HEADER in resp.json()["detail"] and calls == []

    def test_a_request_without_them_carries_none(self, client, chat_app, alice):
        resp, [rec] = self._records(client, _for("alice"))
        assert resp.status_code == 200 and rec.request_tags == {}

    @pytest.mark.parametrize("key", ["sk-alice", "sk-batch"])
    def test_from_any_other_key_the_header_is_not_read(self, client, db_session, chat_app, alice, key):
        """Not even a malformed one is refused: it is not this account's to send."""
        _add(db_session, username="batch-job", api_key="sk-batch", is_service_account=True)
        resp, [rec] = self._records(client, {**auth_header(key), TAGS: "not a tag"})
        assert resp.status_code == 200 and rec.request_tags == {}

    @pytest.mark.parametrize("value", [
        "module",                       # no value
        "module=",
        "bad key=x",
        "a.b=x",
        "module=a,module=b",
        "module=" + "x" * 201,
        "module=%FF",                   # not UTF-8
        "module=a%0Ab",                 # a line break
        ",".join(f"k{i}=v" for i in range(17)),
    ])
    def test_what_is_refused(self, client, chat_app, alice, value):
        resp, calls = _chat(client, {**_for("alice"), TAGS: value})
        assert resp.status_code == 400 and TAGS in resp.json()["detail"]
        assert calls == [] and _rows() == []

    def test_on_a_route_without_a_body_too(self, client, chat_app, alice):
        assert client.get("/v1/models", headers={**_for("alice"), TAGS: "module=a"}).status_code == 200
        assert client.get("/v1/models", headers={**_for("alice"), TAGS: "module"}).status_code == 400

    def test_nothing_of_it_is_sent_downstream(self, client, chat_app, alice):
        seen: list[dict] = []

        async def fake_post(url, **kwargs):
            seen.append(kwargs)
            return make_httpx_response(200, _OK_CHAT)

        with patch.object(client.__httpx_mock__, "post", side_effect=fake_post):
            resp = client.post(
                "/v1/chat/completions", json=_CHAT, headers={**_for("alice"), TAGS: "module=invoice-ocr"},
            )
        assert resp.status_code == 200 and seen
        assert all("invoice-ocr" not in repr(k) and "alice" not in repr(k.get("headers")) for k in seen)

    def test_the_body_names_nobody(self, client, chat_app, alice):
        """``metadata.user_id`` is not a second way to name the user (it was
        built and taken out by the owner's decision): the header is."""
        resp, calls = _chat(client, _for(None), {**_CHAT, "metadata": {"user_id": "alice"}})
        assert resp.status_code == 400 and calls == []


# ── Who may speak for someone ──

class TestOnlyFromSuchAServiceAccount:
    def test_a_persons_key_cannot_put_a_request_on_someone_else(self, client, chat_app, alice, test_user):
        resp, _ = _chat(client, _for("testuser", key="sk-alice"))
        assert resp.status_code == 200
        [row] = _rows()
        assert (row.user_id, row.via_user_id) == (alice.id, 0)

    def test_a_service_account_without_the_switch_is_itself(self, client, db_session, alice):
        batch = _add(db_session, username="batch", api_key="sk-batch", is_service_account=True)
        resp, _ = _chat(client, _for("alice", key="sk-batch"))
        assert resp.status_code == 200
        [row] = _rows()
        assert (row.user_id, row.via_user_id) == (batch.id, 0)
        # ... and needs no header.
        resp, _ = _chat(client, _for(None, key="sk-batch"))
        assert resp.status_code == 200

    def test_the_switch_means_nothing_on_a_persons_account(self, client, db_session, alice, test_user):
        alice.acts_for_users = True  # not reachable through the admin panel
        db_session.add(alice)
        db_session.commit()
        resp, _ = _chat(client, _for("testuser", key="sk-alice"))
        assert resp.status_code == 200
        assert [(r.user_id, r.via_user_id) for r in _rows()] == [(alice.id, 0)]


# ── Every route ──

class TestEveryRoute:
    """The header is read where the key is, so it works the same on every
    API route, with and without the ``/v1`` prefix."""

    ROUTES = [
        ("get", "/v1/models"), ("get", "/models"), ("get", "/azure/v1/models"), ("get", "/aws/v1/models"),
        ("post", "/v1/chat/completions"), ("post", "/chat/completions"),
        ("post", "/v1/messages"), ("post", "/messages"),
        ("post", "/v1/responses"), ("post", "/responses"),
        ("post", "/v1/messages/count_tokens"), ("post", "/v1/embeddings"), ("post", "/v1/rerank"),
        ("post", "/v1/tokenize"), ("post", "/v1/systemone"),
        ("post", "/azure/v1/chat/completions"), ("post", "/azure/v1/messages"), ("post", "/azure/v1/responses"),
        ("post", "/aws/v1/chat/completions"), ("post", "/aws/v1/messages"),
    ]

    def _call(self, client, method: str, path: str, headers: dict):
        kwargs = {"json": _CHAT} if method == "post" else {}
        return getattr(client, method)(path, headers=headers, **kwargs)

    @pytest.mark.parametrize("method,path", ROUTES)
    def test_it_needs_the_header_and_a_real_user(self, client, chat_app, alice, method, path):
        mock, calls = _downstream(client)
        with mock:
            assert self._call(client, method, path, _for(None)).status_code == 400
            assert self._call(client, method, path, _for("nobody")).status_code == 403
        assert calls == [] and _rows() == []

    @pytest.mark.parametrize("path", ["/chat/completions", "/v1/chat/completions", "/messages", "/v1/messages"])
    def test_billed_to_the_user_with_or_without_the_prefix(self, client, chat_app, alice, path):
        mock, _ = _downstream(client)
        body = _MESSAGES if path.endswith("messages") else _CHAT
        with mock:
            assert client.post(path, json=body, headers=_for("alice")).status_code == 200
        assert [(r.user_id, r.via_user_id) for r in _rows()] == [(alice.id, chat_app.id)]

    def test_a_cloud_surface_asks_for_the_users_access(self, client, chat_app, alice, test_user):
        # alice has no Azure access: /azure/v1 refuses her, whatever the application has.
        assert client.get("/azure/v1/models", headers=_for("alice")).status_code == 403
        assert client.get("/azure/v1/models", headers=_for("testuser")).status_code == 200


# ── The account endpoints ──

class TestAccountEndpoints:
    """The header says whose model request this is. An account endpoint
    answers about the account the key belongs to, and nobody else's."""

    READS = ("/v1/me", "/me", "/v1/status", "/v1/usage", "/v1/service-accounts", "/v1/notices", "/v1/settings")

    def test_an_application_cannot_read_a_users_account(self, client, db_session, chat_app, alice):
        _sent_for(db_session, alice, chat_app, "3")
        for path in self.READS:
            resp = client.get(path, headers=_for("alice"))
            assert resp.status_code == 403 and HEADER in resp.json()["detail"], path
            assert "alice" not in resp.text, path
        for path, body in (("/v1/notices/resolved", {}), ("/v1/events", {"event": "installed"})):
            assert client.post(path, json=body, headers=_for("alice")).status_code == 403, path

    def test_whoever_it_names(self, client, chat_app, alice):
        # Refused before the name is looked at: no account can be probed this way.
        for name in ("alice", "nobody-here", "chat-app"):
            assert client.get("/v1/me", headers=_for(name)).status_code == 403, name

    def test_without_the_header_it_reads_its_own_account(self, client, db_session, chat_app, alice):
        """One endpoint for a person and for a service account: each key
        reads its own account. What it sent for its users is theirs, so it
        is no part of the service account's own figures."""
        _sent_for(db_session, alice, chat_app, "3")
        body = client.get("/v1/me", headers=_for(None)).json()
        assert body["username"] == "chat-app"
        assert body["daily"]["limit_usd"] == 50.0 and body["daily"]["used_usd"] == 0.0
        for path in self.READS:
            assert client.get(path, headers=_for(None)).status_code == 200, path

    def test_the_user_reads_them_with_their_own_key(self, client, chat_app, alice):
        for path in self.READS:
            assert client.get(path, headers=auth_header("sk-alice")).status_code == 200, path

    def test_a_service_account_that_does_not_act_for_users_is_as_before(self, client, db_session, alice):
        # The header means nothing from any other key: it is not read.
        _add(db_session, username="batch", api_key="sk-batch", is_service_account=True, concurrency_waived=True)
        body = client.get("/v1/me", headers=_for("alice", key="sk-batch")).json()
        assert body["username"] == "batch"
        assert client.get("/v1/me", headers=_for("testuser", key="sk-alice")).json()["username"] == "alice"

    def test_the_model_list_is_still_the_users(self, client, chat_app, alice):
        # /v1/models is the model API: what the user's requests can name.
        assert client.get("/v1/models", headers=_for("alice")).status_code == 200


# ── The model list ──

def _listed(client, headers, path="/v1/models") -> list[str]:
    resp = client.get(path, headers=headers)
    assert resp.status_code == 200, resp.text
    return [m["id"] for m in resp.json()["data" if path.endswith("models") else "models"]]


class TestModels:
    def test_each_user_gets_their_own_models(self, client, chat_app, alice, test_user):
        for_alice = _listed(client, _for("alice"))
        for_testuser = _listed(client, _for("testuser"))
        assert "test-llm" in for_alice and "azure-gpt-4" not in for_alice
        assert {"test-llm", "azure-gpt-4"} <= set(for_testuser)
        # Exactly what each gets with their own key: nothing is per application.
        assert for_alice == _listed(client, auth_header("sk-alice"))
        assert for_testuser == _listed(client, auth_header())
        assert _listed(client, _for("alice"), "/models") == _listed(client, auth_header("sk-alice"), "/models")

    def test_what_an_admin_hides_is_hidden_for_everyone(self, client, chat_app, alice):
        """Which models are listed at all is one switch per model (Hidden,
        Admin → Models), the same whoever asks and through whatever key."""
        from tests.conftest import TEST_MODEL_ROUTING

        assert "test-vlm" in _listed(client, _for("alice"))
        with patch.dict(TEST_MODEL_ROUTING["test-vlm"], {"hidden": True}):
            assert "test-vlm" not in _listed(client, _for("alice"))
            assert "test-vlm" not in _listed(client, auth_header("sk-alice"))
            # Hidden is a listing concern: named, it is still served.
            resp, calls = _chat(client, _for("alice"), {**_CHAT, "model": "test-vlm"})
        assert resp.status_code == 200 and calls == ["vllm"]

    def test_a_service_account_has_no_model_list_of_its_own(self):
        from app.models import schema

        assert "model_list" not in schema.User.model_fields
        assert not hasattr(schema, "listed_models")



# ── Reports ──

class TestReports:
    def test_the_same_once_rolled_up(self, db_session, chat_app, alice):
        day = datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)
        _sent_for(db_session, alice, chat_app, "1", created_at=day)
        _sent_for(db_session, alice, None, "2", created_at=day)
        first, last = day.date(), day.date() + timedelta(days=1)
        dims = ("user_id", "via_user_id")
        before = usage_rollup.aggregate(db_session, first, last, dims)
        only_app = usage_rollup.aggregate(db_session, first, last, dims, via_user_ids=[chat_app.id])
        usage_rollup.advance(db_session, now=day + timedelta(days=2))
        assert usage_rollup.rolled_through(db_session) >= first
        assert usage_rollup.aggregate(db_session, first, last, dims) == before
        assert usage_rollup.aggregate(db_session, first, last, dims, via_user_ids=[chat_app.id]) == only_app
        assert before[(alice.id, chat_app.id)]["cost_usd"] == Decimal("1")
        assert before[(alice.id, 0)]["cost_usd"] == Decimal("2")
        assert list(only_app) == [(alice.id, chat_app.id)]
        # The user's own figures hold both: it is one bill.
        assert usage_rollup.aggregate(db_session, first, last, ("user_id",))[(alice.id,)]["cost_usd"] == Decimal("3")

    def test_what_a_service_account_sent_for_its_users(self, db_session, chat_app, alice, test_user):
        _sent_for(db_session, alice, chat_app, "1.5")
        _sent_for(db_session, alice, chat_app, "0.5")
        _sent_for(db_session, test_user, chat_app, "3")
        _sent_for(db_session, test_user, None, "9")  # her own key: not through it
        expected = {chat_app.id: {"cost": 5.0, "input_tokens": 3000, "output_tokens": 1500, "requests": 3, "users": 2}}
        assert stats.get_on_behalf_month(db_session) == expected
        assert stats.get_on_behalf_month(db_session, [chat_app.id]) == expected
        assert stats.get_on_behalf_month(db_session, [alice.id]) == {}
        # None of it is the service account's own usage.
        assert stats.get_monthly_all_users_usage(db_session).get(chat_app.id) is None

    def test_the_owner_sees_it_apart_from_the_accounts_own_month(self, client, db_session, chat_app, alice, test_user):
        db_session.add(AppOwner(app_id=chat_app.id, owner_id=test_user.id))
        db_session.commit()
        _sent_for(db_session, alice, chat_app, "1.5")
        [account] = client.get("/v1/service-accounts", headers=auth_header()).json()["service_accounts"]
        assert account["acts_for_users"] is True
        assert account["on_behalf"] == {
            "users": 1, "requests": 1, "input_tokens": 1000, "output_tokens": 500, "cost_usd": 1.5,
        }
        assert account["month"]["cost_usd"] == 0.0 and account["month"]["requests"] == 0
        page = client.get("/dashboard", headers=web_auth_header(sub="testuser")).text
        card = page[page.index('data-service-account="chat-app"'):]
        card = card[:card.index("data-service-howto=")]
        # It leads with what it sent for its users, not with an own month of $0.00.
        assert 'data-service-figures="for-users"' in card and "Sent for its users" in card
        assert "$1.50" in card and "Nothing was billed to this account itself" in card
        assert "No usage this month." not in page

    def test_a_service_account_that_is_itself_keeps_its_own_figures(self, client, db_session, test_user):
        batch = _add(db_session, username="batch", api_key="sk-batch", is_service_account=True)
        db_session.add(AppOwner(app_id=batch.id, owner_id=test_user.id))
        db_session.commit()
        _sent_for(db_session, batch, None, "2.25")
        page = client.get("/dashboard", headers=web_auth_header(sub="testuser")).text
        assert 'data-service-figures="own"' in page and "Cost this month" in page and "$2.25" in page

    def test_the_admin_leaderboard_ranks_what_went_through_it(self, client, db_session, admin_user, chat_app, alice):
        batch = _add(db_session, username="batch", api_key="sk-batch", is_service_account=True)
        _sent_for(db_session, batch, None, "1")
        _sent_for(db_session, alice, chat_app, "5")
        board = stats.get_monthly_overview(db_session)["app_leaderboard"]
        assert [(r["username"], r["monthly_cost"], r["for_users_cost"]) for r in board] == [
            ("chat-app", 0.0, 5.0), ("batch", 1.0, 0.0),
        ]
        # Billed to alice: counted once in the gateway's total.
        assert stats.get_monthly_overview(db_session)["totals"]["cost"] == 6.0
        page = client.get("/admin", headers=web_auth_header(sub="admin", scopes=["admin"])).text
        assert "+ $5.00 for users" in page

    def test_the_user_sees_what_came_through_an_application_today(self, client, db_session, chat_app, alice):
        _sent_for(db_session, alice, chat_app, "1.25")
        _sent_for(db_session, alice, None, "2")
        assert stats.get_user_on_behalf_today(db_session, alice.id) == [
            {"service_account": "chat-app", "cost_usd": 1.25, "requests": 1},
        ]
        assert stats.get_user_daily_summary(db_session, alice.id)["total_cost_usd"] == 3.25
        page = client.get("/dashboard", headers=web_auth_header(sub="alice")).text
        assert "through" in page and "chat-app" in page

    def test_a_deleted_service_account_leaves_the_usage(self, db_session, chat_app, alice):
        _sent_for(db_session, alice, chat_app, "1")
        gone = chat_app.id
        db_session.delete(chat_app)
        db_session.commit()
        assert stats.get_user_on_behalf_today(db_session, alice.id) == [
            {"service_account": f"#{gone}", "cost_usd": 1.0, "requests": 1},
        ]


# ── The month by the way it came in: the dashboard's Usage Flow card, "sources" in /v1/usage ──

def _came_in(sources: list[dict]) -> list[tuple]:
    return [(s["source"], s.get("service_account"), s["cost_usd"], s["requests"]) for s in sources]


class TestWhereItCameFrom:
    def _usage(self, client, key: str = "sk-alice") -> dict:
        resp = client.get("/v1/usage", headers=auth_header(key))
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_the_users_own_key_and_each_application_apart(self, client, db_session, chat_app, alice):
        ocr = _add(db_session, username="invoice-ocr", api_key="sk-ocr", is_service_account=True, acts_for_users=True)
        _sent_for(db_session, alice, None, "2")
        _sent_for(db_session, alice, chat_app, "1.25")
        _sent_for(db_session, alice, chat_app, "0.25")
        _sent_for(db_session, alice, ocr, "3")
        sources = stats.get_usage_sources(db_session, alice)
        # Her own key first, then the applications by what they cost her.
        assert _came_in(sources) == [
            ("own_key", None, 2.0, 1), ("service_account", "invoice-ocr", 3.0, 1), ("service_account", "chat-app", 1.5, 2),
        ]
        assert [(m["model"], m["backend"], m["requests"]) for m in sources[2]["models"]] == [("test-llm", "vllm", 2)]
        # Part of her month, never on top of it.
        assert sum(s["cost_usd"] for s in sources) == stats.get_user_monthly_summary(db_session, alice.id)["total_cost_usd"]
        assert _came_in(stats.get_usage_sources(db_session, alice, period="day")) == _came_in(sources)
        # The application's own account has none of it.
        assert stats.get_usage_sources(db_session, chat_app) == []

    def test_the_usage_endpoint_gives_it_beside_what_it_gave_before(self, client, db_session, chat_app, alice):
        _sent_for(db_session, alice, None, "2")
        before = self._usage(client)
        assert _came_in(before["month"]["sources"]) == [("own_key", None, 2.0, 1)]
        _sent_for(db_session, alice, chat_app, "1.5")
        body = self._usage(client)
        for period in ("today", "month"):
            block = body[period]
            # Every key a client was built against is still there, counting everything.
            assert set(before[period]) == set(block)
            assert block["cost_usd"] == 3.5 and block["requests"] == 2
            assert [(m["model"], m["backend"], m["requests"], m["cost_usd"]) for m in block["models"]] == [
                ("test-llm", "onprem", 2, 3.5),
            ]
            assert block["sources"] == [
                {"source": "own_key", "requests": 1, "input_tokens": 1000, "output_tokens": 500, "cost_usd": 2.0,
                 "models": [{"model": "test-llm", "backend": "onprem", "type": "llm", "overflow": False,
                             "requests": 1, "input_tokens": 1000, "output_tokens": 500, "cost_usd": 2.0}]},
                {"source": "service_account", "service_account": "chat-app",
                 "requests": 1, "input_tokens": 1000, "output_tokens": 500, "cost_usd": 1.5,
                 "models": [{"model": "test-llm", "backend": "onprem", "type": "llm", "overflow": False,
                             "requests": 1, "input_tokens": 1000, "output_tokens": 500, "cost_usd": 1.5}]},
            ]
            # They add up to the period's own totals.
            for key in ("requests", "input_tokens", "output_tokens", "cost_usd"):
                assert sum(s[key] for s in block["sources"]) == block[key]
        assert body["days"] == before["days"][:-1] + [{**before["days"][-1], "requests": 2, "input_tokens": 2000,
                                                       "output_tokens": 1000, "cost_usd": 3.5}]

    def test_nothing_used_is_an_empty_list(self, client, alice):
        body = self._usage(client)
        assert body["month"]["sources"] == [] and body["today"]["sources"] == []

    def test_the_description_an_admin_wrote_comes_with_the_name(self, client, db_session, chat_app, alice):
        _sent_for(db_session, alice, chat_app, "1")
        assert "description" not in self._usage(client)["month"]["sources"][0]  # none: no key, never a null
        chat_app.description = "HR chat page"
        db_session.add(chat_app)
        db_session.commit()
        [source] = self._usage(client)["month"]["sources"]
        assert (source["service_account"], source["description"]) == ("chat-app", "HR chat page")
        assert stats.get_user_on_behalf_today(db_session, alice.id) == [
            {"service_account": "chat-app", "description": "HR chat page", "cost_usd": 1.0, "requests": 1},
        ]

    def test_the_same_once_rolled_up(self, client, db_session, chat_app, alice):
        earlier = datetime.now(timezone.utc) - timedelta(days=2)
        _sent_for(db_session, alice, chat_app, "1", created_at=earlier)
        _sent_for(db_session, alice, None, "2", created_at=earlier)
        _sent_for(db_session, alice, chat_app, "0.5")
        before = self._usage(client)["month"]["sources"]
        usage_rollup.advance(db_session, now=datetime.now(timezone.utc))
        assert self._usage(client)["month"]["sources"] == before

    def test_a_cloud_backend_the_user_may_not_use_is_left_out_as_in_cost_by_model(
        self, client, db_session, chat_app, alice,
    ):
        _sent_for(db_session, alice, chat_app, "1")
        db_session.add(UsageLog(
            user_id=alice.id, model="azure-gpt-4", model_type="llm", input_tokens=10, output_tokens=5,
            cost_usd=Decimal("4"), endpoint="/v1/chat/completions", backend="azure", via_user_id=chat_app.id,
            created_at=earlier_today(),
        ))
        db_session.commit()
        month = self._usage(client)["month"]  # alice has no Azure access
        assert month["cost_usd"] == 1.0
        assert _came_in(month["sources"]) == [("service_account", "chat-app", 1.0, 1)]

    def test_a_deleted_application_keeps_its_place(self, db_session, chat_app, alice):
        _sent_for(db_session, alice, chat_app, "1")
        gone = chat_app.id
        db_session.delete(chat_app)
        db_session.commit()
        assert _came_in(stats.get_usage_sources(db_session, alice)) == [("service_account", f"#{gone}", 1.0, 1)]

    def test_an_application_cannot_read_it_for_a_user(self, client, chat_app, alice):
        assert client.get("/v1/usage", headers=_for("alice")).status_code == 403

    def _flow(self, client, sub: str) -> str:
        page = client.get("/dashboard", headers=web_auth_header(sub=sub)).text
        card = page[page.index("data-usage-flow"):]
        return card[:card.index("Cost by Model")]

    def test_the_dashboard_shows_the_month_as_a_flow(self, client, db_session, chat_app, alice):
        chat_app.description = "HR <b>chat</b> page"
        db_session.add(chat_app)
        db_session.commit()
        _sent_for(db_session, alice, None, "2")
        _sent_for(db_session, alice, chat_app, "1.5")
        card = self._flow(client, "alice")
        assert 'data-flow-source="Your API key"' in card and 'data-flow-source="chat-app"' in card
        assert "$2.00" in card and "$1.50" in card
        # What the admin wrote is shown as text, never as markup.
        assert "HR &lt;b&gt;chat&lt;/b&gt; page" in card and "<b>chat</b>" not in card
        # The drawing reads the same data, from one place.
        data = card[card.index("data-sources='") + len("data-sources='"):]
        sources = json.loads(data[:data.index("'")].replace("&#34;", '"').replace("&lt;", "<").replace("&gt;", ">"))
        assert [(s["label"], s["cost_usd"]) for s in sources] == [("Your API key", 2.0), ("chat-app", 1.5)]
        # Today's line names it too.
        page = client.get("/dashboard", headers=web_auth_header(sub="alice")).text
        assert "(HR &lt;b&gt;chat&lt;/b&gt; page)" in page

    def test_a_month_without_usage_says_so(self, client, alice):
        card = self._flow(client, "alice")
        assert "Nothing used yet this month." in card and "data-sources" not in card


# ── What a service account is, in an admin's words ──

class TestDescription:
    def _describe(self, client, user: User, text: str, headers=None):
        return client.post(f"/admin/users/{user.id}/description", data={"description": text},
                           headers=headers or {**_admin(), **_JSON})

    def test_an_admin_writes_one_line(self, client, db_session, admin_user, chat_app):
        resp = self._describe(client, chat_app, "  HR chat page\n (intranet)  ")
        assert resp.json() == {"ok": True, "description": "HR chat page (intranet)"}
        db_session.expire_all()
        assert db_session.get(User, chat_app.id).description == "HR chat page (intranet)"
        # Empty clears it.
        assert self._describe(client, chat_app, "").json() == {"ok": True, "description": ""}

    def test_what_is_refused(self, client, db_session, admin_user, chat_app, alice):
        from app.routers.admin.users import DESCRIPTION_MAX

        assert self._describe(client, chat_app, "x" * DESCRIPTION_MAX).status_code == 200
        assert self._describe(client, chat_app, "x" * (DESCRIPTION_MAX + 1)).status_code == 400
        # A person is named by their sign-in, not by an admin.
        resp = self._describe(client, alice, "a colleague")
        assert resp.status_code == 400 and "service account" in resp.json()["detail"]
        assert client.patch(f"/admin/users/{alice.id}", json={"description": "x"}, headers=_admin()).status_code == 400
        assert self._describe(client, chat_app, "x", headers=web_auth_header(sub="alice")).status_code == 403
        assert client.post("/admin/users/99999/description", data={"description": "x"},
                           headers={**_admin(), **_JSON}).status_code == 404
        db_session.expire_all()
        assert db_session.get(User, alice.id).description == ""

    def test_given_when_the_account_is_created(self, client, db_session, admin_user):
        resp = client.post("/admin/users/create", data={"username": "billing-etl", "description": "Nightly billing export"},
                           headers={**_admin(), **_JSON})
        assert resp.status_code == 200, resp.text
        created = db_session.exec(select(User).where(User.username == "billing-etl")).one()
        assert created.description == "Nightly billing export" and created.is_service_account

    def test_the_json_api(self, client, db_session, admin_user, chat_app):
        resp = client.post("/admin/users", json={"username": "ocr", "is_service_account": True, "description": "Invoice OCR"},
                           headers=_admin())
        assert resp.status_code == 200, resp.text
        assert client.post("/admin/users", json={"username": "bob", "description": "a person"},
                           headers=_admin()).status_code == 400
        assert client.patch(f"/admin/users/{chat_app.id}", json={"description": "HR chat page"}, headers=_admin()).status_code == 200
        rows = {u["username"]: u["description"] for u in client.get("/admin/users", headers=_admin()).json()}
        assert rows["ocr"] == "Invoice OCR" and rows["chat-app"] == "HR chat page"
        assert rows["admin"] == ""  # a person has none: an empty string, as stored

    def test_the_admin_sees_and_finds_it(self, client, db_session, admin_user, chat_app, alice):
        self._describe(client, chat_app, "HR chat page")
        drawer = client.get(f"/admin/users/{chat_app.id}/detail", headers=_admin()).text
        assert f'action="/admin/users/{chat_app.id}/description"' in drawer and 'value="HR chat page"' in drawer
        assert "/description" not in client.get(f"/admin/users/{alice.id}/detail", headers=_admin()).text
        table = client.get("/admin/user-table", params={"kind": "apps", "q": "hr chat"}, headers=_admin()).text
        assert 'data-username="chat-app"' in table and "HR chat page" in table

    def test_the_owner_sees_it_on_the_card_and_by_key(self, client, db_session, admin_user, chat_app, test_user):
        db_session.add(AppOwner(app_id=chat_app.id, owner_id=test_user.id))
        db_session.commit()
        [account] = client.get("/v1/service-accounts", headers=auth_header()).json()["service_accounts"]
        assert "description" not in account
        self._describe(client, chat_app, "HR <i>chat</i> page")
        [account] = client.get("/v1/service-accounts", headers=auth_header()).json()["service_accounts"]
        assert account["description"] == "HR <i>chat</i> page"
        page = client.get("/dashboard", headers=web_auth_header(sub="testuser")).text
        card = page[page.index('data-service-account="chat-app"'):]
        card = card[:card.index("data-service-howto=")]
        assert "data-service-description" in card and "HR &lt;i&gt;chat&lt;/i&gt; page" in card

    def test_the_change_is_in_the_audit_log(self, client, db_session, admin_user, chat_app):
        from app.models.schema import AuditLog

        assert self._describe(client, chat_app, "HR chat page").status_code == 200
        db_session.expire_all()
        [row] = [r for r in db_session.exec(select(AuditLog)).all() if r.route.endswith("/description")]
        detail = json.loads(row.detail)
        assert (row.actor, detail["target"], detail["request"]) == ("admin", "chat-app", {"description": "HR chat page"})


# ── The owner's dashboard: how to use it, and how to test ──

class TestOwnersDashboard:
    def _card(self, client, db_session, owner: User, account: User) -> str:
        db_session.add(AppOwner(app_id=account.id, owner_id=owner.id))
        db_session.commit()
        page = client.get("/dashboard", headers=web_auth_header(sub=owner.username)).text
        card = page[page.index(f'data-service-account="{account.username}"'):]
        card = card[card.index("data-service-howto="):]  # the instructions, not the key row above them
        return card[:card.index("</details>")]

    def test_it_says_how_to_send_a_request_for_a_user_and_check_it(self, client, db_session, chat_app, test_user):
        card = self._card(client, db_session, test_user, chat_app)
        assert card.startswith('data-service-howto="for-users"')
        # A test the owner can run as it stands: on their own budget.
        assert '-H "X-On-Behalf-Of: testuser"' in card
        assert "/v1/models" in card and "/v1/chat/completions" in card
        assert "through chat-app" in card
        for status in ("400", "403", "429"):
            assert f">{status}<" in card, status
        # The key is never written into the instructions.
        assert "$SERVICE_ACCOUNT_KEY" in card and APP_KEY not in card

    def test_it_only_works_once_an_admin_turned_it_on(self, client, db_session, test_user):
        batch = _add(db_session, username="batch", api_key="sk-batch", is_service_account=True)
        card = self._card(client, db_session, test_user, batch)
        assert card.startswith('data-service-howto="own"')
        assert "off for this account" in card and "the header is ignored" in card
        assert "an administrator has turned on" in card
        assert '-H "X-On-Behalf-Of' not in card  # no example that would not do what it shows


# ── The admin's side ──

def _admin() -> dict[str, str]:
    return web_auth_header(sub="admin", scopes=["admin"])


_JSON = {"Accept": "application/json"}


class TestAdmin:
    def _switch(self, client, user: User, value: str | None):
        data = {} if value is None else {"enabled": value}
        return client.post(f"/admin/users/{user.id}/acts-for-users", data=data, headers={**_admin(), **_JSON})

    def test_the_switch_takes_a_value_never_a_flip(self, client, db_session, admin_user):
        batch = _add(db_session, username="batch", api_key="sk-batch", is_service_account=True)
        assert self._switch(client, batch, "on").json() == {"ok": True, "acts_for_users": True}
        assert self._switch(client, batch, "on").json()["acts_for_users"] is True  # asked twice: still on
        assert self._switch(client, batch, None).status_code == 400
        assert self._switch(client, batch, "maybe").status_code == 400
        db_session.expire_all()
        assert db_session.get(User, batch.id).acts_for_users is True
        assert self._switch(client, batch, "off").json()["acts_for_users"] is False

    def test_only_a_service_account_can_act_for_users(self, client, db_session, admin_user, alice):
        resp = self._switch(client, alice, "on")
        assert resp.status_code == 400 and "service account" in resp.json()["detail"]
        resp = client.patch(f"/admin/users/{alice.id}", json={"acts_for_users": True}, headers=_admin())
        assert resp.status_code == 400
        db_session.expire_all()
        assert db_session.get(User, alice.id).acts_for_users is False

    def test_admins_only(self, client, db_session, chat_app, test_user):
        resp = client.post(f"/admin/users/{chat_app.id}/acts-for-users", data={"enabled": "off"},
                           headers=web_auth_header(sub="testuser"))
        assert resp.status_code == 403

    def test_the_json_api(self, client, db_session, admin_user, chat_app):
        resp = client.patch(f"/admin/users/{chat_app.id}", json={"acts_for_users": False}, headers=_admin())
        assert resp.status_code == 200, resp.text
        assert client.patch(f"/admin/users/{chat_app.id}", json={"acts_for_users": "yes"}, headers=_admin()).status_code == 400
        [row] = [u for u in client.get("/admin/users", headers=_admin()).json() if u["username"] == "chat-app"]
        assert row["acts_for_users"] is False and "model_list" not in row

    def test_it_is_special_access(self, client, db_session, admin_user, chat_app, alice):
        from app.routers.admin.users import _special_access_summary

        _add(db_session, username="batch", api_key="sk-batch", is_service_account=True, concurrency_waived=True)
        summary = {s["key"]: (s["users"], s["apps"]) for s in _special_access_summary(db_session)}
        assert summary["on_behalf"] == (0, 1)
        assert summary["special"][1] == 1

        def names(kind, status):
            body = client.get("/admin/user-table", params={"kind": kind, "status": status}, headers=_admin()).text
            return sorted(n for n in ("chat-app", "batch", "alice") if f'data-username="{n}"' in body)

        assert names("apps", "on_behalf") == ["chat-app"]
        assert names("apps", "special") == ["chat-app"]
        assert names("users", "on_behalf") == []

    def test_the_row_has_the_switch_and_what_went_through(self, client, db_session, admin_user, chat_app, alice):
        _sent_for(db_session, alice, chat_app, "1.5")
        apps = client.get("/admin/user-table", params={"kind": "apps"}, headers=_admin()).text
        assert "um-chip-onbehalf" in apps and "for 1 user" in apps
        # The switch is in the account's drawer.
        drawer = client.get(f"/admin/users/{chat_app.id}/detail", headers=_admin()).text
        assert f'action="/admin/users/{chat_app.id}/acts-for-users"' in drawer
        assert 'name="enabled" value="off"' in drawer  # it is on: the next click asks for off
        # People have neither.
        users = client.get("/admin/user-table", params={"kind": "users"}, headers=_admin()).text
        assert "um-chip-onbehalf" not in users
        assert "acts-for-users" not in client.get(f"/admin/users/{alice.id}/detail", headers=_admin()).text
        # No list of models per account: which models are listed is Admin → Models (Hidden).
        assert "data-model-list" not in apps and "model-list" not in apps

    def test_sorting_by_spend_counts_what_went_through(self, client, db_session, admin_user, chat_app, alice):
        batch = _add(db_session, username="batch", api_key="sk-batch", is_service_account=True)
        _sent_for(db_session, batch, None, "1")
        _sent_for(db_session, alice, chat_app, "5")
        body = client.get("/admin/user-table", params={"kind": "apps", "sort": "spend"}, headers=_admin()).text
        assert body.index('data-username="chat-app"') < body.index('data-username="batch"')

    def test_the_change_is_in_the_audit_log(self, client, db_session, admin_user, chat_app):
        from app.models.schema import AuditLog

        # AuditMiddleware is part of the test app: nothing in the handler writes this.
        resp = client.post(f"/admin/users/{chat_app.id}/acts-for-users", data={"enabled": "off"},
                           headers={**_admin(), **_JSON})
        assert resp.status_code == 200
        db_session.expire_all()
        rows = db_session.exec(select(AuditLog)).all()
        [row] = [r for r in rows if "acts-for-users" in r.route]
        detail = json.loads(row.detail)
        assert (row.actor, detail["target"], detail["request"]) == ("admin", "chat-app", {"enabled": "off"})
