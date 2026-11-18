"""Peak overflow (app/services/overflow.py): while an on-prem server is
congested, the job moves accounts to the cloud model its models name, and
their requests are served there until it moves them back."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
from sqlmodel import Session, select

from app.models.schema import (
    ConcurrencyLease,
    OverflowBudget,
    OverflowServer,
    OverflowStay,
    ScheduledJob,
    UsageLog,
    User,
)
from app.services import overflow, scheduler, usage_rollup
from tests.conftest import (
    OVERFLOW_OFF,
    TEST_MODEL_ROUTING,
    FakeStreamResponse,
    _test_engine,
    auth_header,
    earlier_today,
    make_httpx_response,
    web_auth_header,
)

LLM_URL = TEST_MODEL_ROUTING["test-llm"]["base_url"]
VLM_URL = TEST_MODEL_ROUTING["test-vlm"]["base_url"]

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
_OK_BEDROCK = {
    "output": {"message": {"role": "assistant", "content": [{"text": "from bedrock"}]}},
    "stopReason": "end_turn",
    "usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120},
}


@contextmanager
def _settings(**values):
    """Overflow on (``enforce`` unless told otherwise) with a $50 budget."""
    s = {**OVERFLOW_OFF, "mode": "enforce", "daily_budget_usd": 50.0, **values}
    with patch("app.services.overflow.get_overflow_settings", return_value=s):
        yield s


@contextmanager
def _overflows_to(target: str = "azure-gpt-4", alias: str = "test-llm", **extra):
    """``alias`` names ``target`` as its overflow model."""
    with patch.dict(TEST_MODEL_ROUTING[alias], {"overflow_to": target, **extra}):
        yield


@contextmanager
def _queue(waiting: int | None, url: str = LLM_URL):
    """The health loop's last reading of one server's queue (None = unreadable)."""
    def metrics(base_url):
        if base_url != url or waiting is None:
            return None
        return {"waiting": waiting, "running": 4}
    with patch("app.services.overflow.get_metrics", side_effect=metrics):
        yield


def _person(session: Session, name: str, **fields) -> User:
    user = User(username=name, api_key=f"sk-{name}", daily_limit_usd=100.0, **fields)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _used(session: Session, user: User, tokens: int = 1000, *, model: str = "test-llm",
          backend: str = "vllm", at: datetime | None = None, overflow_row: bool = False, cost: str = "0.01") -> None:
    session.add(UsageLog(
        user_id=user.id, model=model, model_type="llm", input_tokens=tokens, output_tokens=0,
        cost_usd=Decimal(cost), endpoint="/v1/chat/completions", backend=backend,
        overflow=overflow_row, created_at=at or earlier_today(1),
    ))
    session.commit()


def _open(session: Session) -> list[str]:
    session.expire_all()
    return sorted(
        s.username for s in session.exec(select(OverflowStay).where(OverflowStay.released_at.is_(None))).all()
    )


def _stay(session: Session, name: str, *, url: str = LLM_URL, mode: str = "enforce",
          entered: datetime | None = None, minutes_left: int = 10) -> OverflowStay:
    now = overflow.utcnow()
    stay = OverflowStay(
        username=name, base_url=url, mode=mode, entered_at=entered or now,
        expires_at=now + timedelta(minutes=minutes_left), reason_in="test",
    )
    session.add(stay)
    session.commit()
    return stay


def _rows(user_id: int) -> list[UsageLog]:
    with Session(_test_engine) as s:
        return list(s.exec(select(UsageLog).where(UsageLog.user_id == user_id).order_by(UsageLog.id)).all())


def _downstream(client, *, azure_status: int = 200, seen: dict | None = None):
    """Answer by backend, recording which one was called and with what model."""
    seen = seen if seen is not None else {}
    seen.setdefault("calls", [])

    async def fake_post(url, **kwargs):
        if "openai.azure.com" in url:
            seen["calls"].append(("azure", (kwargs.get("json") or {}).get("model")))
            if azure_status != 200:
                return make_httpx_response(azure_status, {"error": {"message": "nope", "code": "rate_limit_exceeded"}})
            return make_httpx_response(200, _OK_AZURE)
        if "amazonaws.com" in url:
            seen["calls"].append(("bedrock", url))
            return make_httpx_response(200, _OK_BEDROCK)
        body = kwargs.get("json")
        if body is None and kwargs.get("content"):
            body = json.loads(kwargs["content"])
        seen["calls"].append(("vllm", (body or {}).get("model")))
        return make_httpx_response(200, _OK_CHAT)

    return patch.object(client.__httpx_mock__, "post", side_effect=fake_post), seen


# ── The job: who is moved, and when ──

class TestGrab:
    def test_one_reading_over_is_noise_two_move_the_busiest(self, db_session):
        light = _person(db_session, "light")
        heavy = _person(db_session, "heavy")
        _used(db_session, light, 1_000)
        _used(db_session, heavy, 90_000)
        with _settings(grab_max=1), _overflows_to(), _queue(9):
            overflow.tick(db_session)
            assert _open(db_session) == []
            overflow.tick(db_session)
            assert _open(db_session) == ["heavy"]
            overflow.tick(db_session)  # still over: the next one follows
            assert _open(db_session) == ["heavy", "light"]

    def test_more_are_moved_the_further_over(self):
        # one just over the threshold, the most at twice the threshold
        assert overflow.how_many(7, high=8, grab_max=3) == 0
        assert overflow.how_many(8, high=8, grab_max=3) == 1
        assert overflow.how_many(12, high=8, grab_max=3) == 2
        assert overflow.how_many(16, high=8, grab_max=3) == 3
        assert overflow.how_many(400, high=8, grab_max=3) == 3
        assert overflow.how_many(400, high=8, grab_max=1) == 1

    def test_only_the_congested_servers_own_users(self, db_session):
        on_llm = _person(db_session, "on-llm")
        on_vlm = _person(db_session, "on-vlm")
        _used(db_session, on_llm)
        _used(db_session, on_vlm, 500_000, model="test-vlm")
        with _settings(consecutive=1), _overflows_to(), _overflows_to("bedrock-nova", "test-vlm"), _queue(20):
            overflow.tick(db_session)
        stays = db_session.exec(select(OverflowStay)).all()
        assert [(s.username, s.base_url) for s in stays] == [("on-llm", LLM_URL)]

    def test_who_is_never_moved(self, db_session):
        _used(db_session, _person(db_session, "robot", is_service_account=True))
        _used(db_session, _person(db_session, "kept-home", can_overflow_cloud=False))
        _used(db_session, _person(db_session, "gone", is_disabled=True))
        _used(db_session, _person(db_session, "ok"))
        with _settings(consecutive=1, grab_max=5), _overflows_to(), _queue(40):
            overflow.tick(db_session)
        assert _open(db_session) == ["ok"]

    def test_accounts_are_allowed_unless_an_admin_said_no(self, db_session):
        assert _person(db_session, "new").can_overflow_cloud is True

    def test_a_model_without_a_target_never_overflows(self, db_session):
        _used(db_session, _person(db_session, "busy"))
        with _settings(consecutive=1), _queue(40):
            assert overflow.servers() == {}
            overflow.tick(db_session)
        assert _open(db_session) == []
        with _settings(consecutive=1), _overflows_to("azure-embed"), _queue(40):  # not a chat model
            assert overflow.servers() == {}

    def test_an_unreadable_queue_moves_nobody(self, db_session):
        _used(db_session, _person(db_session, "busy"))
        with _settings(consecutive=1), _overflows_to(), _queue(None):
            overflow.tick(db_session)
        assert _open(db_session) == []
        assert db_session.get(OverflowServer, LLM_URL).waiting is None

    def test_between_the_thresholds_nothing_moves(self, db_session):
        _used(db_session, _person(db_session, "busy"))
        base = overflow.utcnow()
        with _settings(consecutive=1, min_stay_min=0), _overflows_to():
            with _queue(9):
                overflow.tick(db_session, now=base)
            with _queue(5):  # under 8, over 2
                for minute in (1, 2, 3):
                    overflow.tick(db_session, now=base + timedelta(minutes=minute))
        assert _open(db_session) == ["busy"]

    def test_a_servers_own_thresholds(self, db_session):
        with _settings(), _overflows_to(overflow_waiting_high=30, overflow_waiting_low=10):
            server = overflow.servers()[LLM_URL]
            assert (server.high, server.low) == (30, 10)
        with _settings(), _overflows_to(overflow_waiting_high=1, overflow_waiting_low=5):  # nonsense: the gateway's
            server = overflow.servers()[LLM_URL]
            assert (server.high, server.low) == (8, 2)

    def test_the_state_is_one_row_a_server_however_long_it_runs(self, db_session):
        with _settings(), _overflows_to(), _queue(9):
            for _ in range(6):
                overflow.tick(db_session)
        rows = db_session.exec(select(OverflowServer)).all()
        assert [(r.base_url, r.over_streak, r.waiting) for r in rows] == [(LLM_URL, 6, 9)]
        assert len(db_session.exec(select(OverflowBudget)).all()) == 1


class TestRelease:
    def _three_on_the_cloud(self, db_session, base):
        """a (first in, still working), b (second in, idle), c (too recent)."""
        a, b, c = (_person(db_session, n) for n in ("a", "b", "c"))
        _stay(db_session, "a", entered=base - timedelta(minutes=60))
        _stay(db_session, "b", entered=base - timedelta(minutes=30))
        _stay(db_session, "c", entered=base - timedelta(minutes=3))
        _used(db_session, a, at=datetime.now(timezone.utc))
        _used(db_session, c, at=datetime.now(timezone.utc))
        _used(db_session, b, at=datetime.now(timezone.utc) - timedelta(minutes=40))

    def test_idle_first_then_the_longest_and_never_before_the_minimum_stay(self, db_session):
        base = overflow.utcnow()
        self._three_on_the_cloud(db_session, base)
        with _settings(), _overflows_to(), _queue(0):
            overflow.tick(db_session, now=base)
            assert _open(db_session) == ["a", "b", "c"]  # one short reading proves nothing
            overflow.tick(db_session, now=base)
            assert _open(db_session) == ["a", "c"]  # b had gone quiet
            overflow.tick(db_session, now=base)
            assert _open(db_session) == ["c"]  # then the longest on the cloud
            overflow.tick(db_session, now=base)
            assert _open(db_session) == ["c"]  # 3 of its 10 minutes
            overflow.tick(db_session, now=base + timedelta(minutes=8))
            assert _open(db_session) == []
        reasons = {s.username: s.reason_out for s in db_session.exec(select(OverflowStay)).all()}
        assert "idle" in reasons["b"] and "longest" in reasons["a"]

    def test_as_many_a_round_as_the_admin_set(self, db_session):
        base = overflow.utcnow()
        self._three_on_the_cloud(db_session, base)
        with _settings(consecutive=1, release_per_round=2), _overflows_to(), _queue(0):
            overflow.tick(db_session, now=base)
        assert _open(db_session) == ["c"]

    def test_a_stay_is_kept_as_one_row_after_it_ends(self, db_session):
        _person(db_session, "a")
        base = overflow.utcnow()
        _stay(db_session, "a", entered=base - timedelta(minutes=30))
        with _settings(consecutive=1), _overflows_to(), _queue(0):
            overflow.tick(db_session, now=base)
        [stay] = db_session.exec(select(OverflowStay)).all()
        assert stay.released_at == base and stay.reason_out


class TestLease:
    def test_the_job_keeps_a_stay_alive_and_a_stopped_job_lets_it_lapse(self, db_session, test_user):
        base = overflow.utcnow()
        _stay(db_session, test_user.username, minutes_left=1)
        route = TEST_MODEL_ROUTING["test-llm"]
        with _settings(), _overflows_to(), _queue(5):
            overflow.tick(db_session, now=base)
            [stay] = db_session.exec(select(OverflowStay)).all()
            assert stay.expires_at == base + overflow.LEASE
            assert overflow.target_for(test_user, route, now=base + timedelta(minutes=9)) == "azure-gpt-4"
            # no round for longer than the lease: back on-prem without anyone acting
            assert overflow.target_for(test_user, route, now=base + overflow.LEASE + timedelta(seconds=1)) is None
            overflow.tick(db_session, now=base + timedelta(minutes=30))
        assert _open(db_session) == []
        assert "lapsed" in db_session.exec(select(OverflowStay)).one().reason_out

    def test_a_queue_that_cannot_be_read_is_not_renewed(self, db_session, test_user):
        base = overflow.utcnow()
        _stay(db_session, test_user.username, minutes_left=4)
        with _settings(), _overflows_to(), _queue(None):
            overflow.tick(db_session, now=base)
        assert db_session.exec(select(OverflowStay)).one().expires_at < base + timedelta(minutes=5)

    def test_switching_off_moves_everyone_back(self, db_session, test_user):
        _stay(db_session, test_user.username)
        with _overflows_to(), _queue(40):
            assert overflow.tick(db_session) == "off"
        assert _open(db_session) == []

    def test_a_monitor_list_is_not_carried_into_enforce(self, db_session, test_user):
        _stay(db_session, test_user.username, mode="monitor")
        with _settings(), _overflows_to(), _queue(5):
            assert overflow.target_for(test_user, TEST_MODEL_ROUTING["test-llm"]) is None
            overflow.tick(db_session)
        assert _open(db_session) == []

    def test_an_account_switched_off_is_moved_back_by_the_next_round(self, db_session):
        user = _person(db_session, "a")
        _stay(db_session, "a")
        user.can_overflow_cloud = False
        db_session.add(user)
        db_session.commit()
        with _settings(), _overflows_to(), _queue(5):
            overflow.tick(db_session)
        assert _open(db_session) == []

    def test_it_runs_as_a_claimed_job(self, db_session):
        with _settings(), _overflows_to(), _queue(9):
            assert scheduler.run_if_due(overflow.JOB) == "ok"
            assert scheduler.run_if_due(overflow.JOB) is None  # claimed for this interval
        job = db_session.get(ScheduledJob, overflow.JOB_NAME)
        assert job.last_status == "ok" and "mode=enforce" in job.last_detail


class TestBudget:
    def test_no_budget_moves_nobody(self, db_session):
        _used(db_session, _person(db_session, "busy"))
        with _settings(consecutive=1, daily_budget_usd=0.0), _overflows_to(), _queue(40):
            overflow.tick(db_session)
        assert _open(db_session) == []

    def test_past_the_stop_share_nobody_new_and_spent_sends_everyone_back(self, db_session):
        first = _person(db_session, "first")
        _used(db_session, _person(db_session, "second"))
        _stay(db_session, "first")
        with _settings(consecutive=1, daily_budget_usd=10.0, budget_stop_pct=80), _overflows_to(), _queue(40):
            _used(db_session, first, model="azure-gpt-4", backend="azure", overflow_row=True, cost="8.5")
            assert "budget=closing" in overflow.tick(db_session)
            assert _open(db_session) == ["first"]  # stays, but "second" is not moved
            _used(db_session, first, model="azure-gpt-4", backend="azure", overflow_row=True, cost="2")
            assert "budget=spent" in overflow.tick(db_session)
            assert _open(db_session) == []
            assert overflow.target_for(first, TEST_MODEL_ROUTING["test-llm"]) is None
        assert float(db_session.exec(select(OverflowBudget)).one().spent_usd) == 10.5

    def test_only_overflow_rows_count(self, db_session, test_user):
        _stay(db_session, test_user.username)
        _used(db_session, test_user, model="azure-gpt-4", backend="azure", cost="3")  # its own choice
        _used(db_session, test_user, cost="4")  # on-prem
        _used(db_session, test_user, model="azure-gpt-4", backend="azure", overflow_row=True, cost="1.25")
        assert overflow.spent_today(db_session) == Decimal("1.25")

    def test_the_request_path_stops_at_the_budget_without_waiting_for_a_round(self, db_session, test_user):
        _stay(db_session, test_user.username)
        route = TEST_MODEL_ROUTING["test-llm"]
        with _overflows_to():
            with _settings(daily_budget_usd=5.0):
                overflow.refresh()
                assert overflow.target_for(test_user, route) == "azure-gpt-4"
            with _settings(daily_budget_usd=0.0):  # the admin just lowered it
                assert overflow.target_for(test_user, route) is None


# ── The request path ──

@pytest.fixture()
def plain_user(db_session) -> User:
    """A person without cloud access of their own."""
    return _person(db_session, "plain")


def _on_the_cloud(db_session, user: User, url: str = LLM_URL) -> None:
    _stay(db_session, user.username, url=url)
    overflow.refresh()


class TestDispatch:
    def test_a_moved_account_is_served_by_the_cloud_model(self, client, db_session, plain_user):
        mock, seen = _downstream(client)
        with _settings(), _overflows_to(), mock:
            _on_the_cloud(db_session, plain_user)
            resp = client.post("/v1/chat/completions", json=_CHAT, headers=auth_header("sk-plain"))
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "from azure"
        assert resp.headers["X-Overflow"] == "test-llm is busy; served by azure-gpt-4"
        assert seen["calls"] == [("azure", "gpt-4-deploy")]
        [row] = _rows(plain_user.id)
        assert (row.backend, row.model, row.overflow) == ("azure", "azure-gpt-4", True)
        assert row.cost_usd > 0

    def test_everyone_else_stays_on_prem(self, client, db_session, plain_user, test_user):
        mock, seen = _downstream(client)
        with _settings(), _overflows_to(), mock:
            _on_the_cloud(db_session, plain_user)
            resp = client.post("/v1/chat/completions", json=_CHAT, headers=auth_header())
        assert resp.json()["choices"][0]["message"]["content"] == "from on-prem"
        assert "X-Overflow" not in resp.headers
        assert seen["calls"] == [("vllm", "real-llm-v1")]
        assert [(r.backend, r.overflow) for r in _rows(test_user.id)] == [("vllm", False)]

    def test_only_for_the_congested_server(self, client, db_session, plain_user):
        mock, seen = _downstream(client)
        with _settings(), _overflows_to(), _overflows_to("bedrock-nova", "test-vlm"), mock:
            _on_the_cloud(db_session, plain_user)  # for test-llm's server
            client.post("/v1/chat/completions", json={**_CHAT, "model": "test-vlm"}, headers=auth_header("sk-plain"))
        assert seen["calls"] == [("vllm", "real-vlm-v1")]

    @pytest.mark.parametrize("mode", ["off", "monitor"])
    def test_nothing_is_moved_unless_it_is_on(self, client, db_session, plain_user, mode):
        mock, seen = _downstream(client)
        with _settings(), _overflows_to():
            _on_the_cloud(db_session, plain_user)
        with _settings(mode=mode), _overflows_to(), mock:
            client.post("/v1/chat/completions", json=_CHAT, headers=auth_header("sk-plain"))
        assert seen["calls"] == [("vllm", "real-llm-v1")]

    @pytest.mark.parametrize("fields", [{"can_overflow_cloud": False}, {"is_service_account": True}])
    def test_an_account_that_may_not_overflow_is_never_sent_out(self, client, db_session, fields):
        user = _person(db_session, "special", **fields)
        mock, seen = _downstream(client)
        with _settings(), _overflows_to(), mock:
            _on_the_cloud(db_session, user)  # a row that should not exist; the request path still refuses
            client.post("/v1/chat/completions", json=_CHAT, headers=auth_header("sk-special"))
        assert seen["calls"] == [("vllm", "real-llm-v1")]

    def test_a_model_the_gateway_does_not_have_overflows_with_its_stand_in(self, client, db_session, plain_user):
        # Claude Code subagents name models that are not configured: they are
        # served by the on-prem stand-in, so they overflow with that server.
        mock, seen = _downstream(client)
        with _settings(), _overflows_to(), mock:
            _on_the_cloud(db_session, plain_user)
            resp = client.post(
                "/v1/messages", json={**_MESSAGES, "model": "claude-haiku-4-5"}, headers=auth_header("sk-plain"),
            )
        assert resp.status_code == 200
        assert seen["calls"] == [("azure", "gpt-4-deploy")]
        assert resp.headers["X-Overflow"].startswith("test-llm is busy")

    def test_if_the_cloud_refuses_it_is_served_on_prem(self, client, db_session, plain_user):
        mock, seen = _downstream(client, azure_status=429)
        with _settings(), _overflows_to(), mock:
            _on_the_cloud(db_session, plain_user)
            resp = client.post("/v1/chat/completions", json=_CHAT, headers=auth_header("sk-plain"))
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "from on-prem"
        assert "X-Overflow" not in resp.headers
        # the on-prem call names the model that was asked for, not the cloud one
        assert seen["calls"][0][0] == "azure" and seen["calls"][-1] == ("vllm", "real-llm-v1")
        [row] = _rows(plain_user.id)
        assert (row.backend, row.model, row.overflow) == ("vllm", "test-llm", False)

    def test_every_chat_endpoint_overflows(self, client, db_session, plain_user):
        mock, seen = _downstream(client)
        with _settings(), _overflows_to(), mock:
            _on_the_cloud(db_session, plain_user)
            for path, body in (
                ("/v1/chat/completions", _CHAT), ("/chat/completions", _CHAT),
                ("/v1/messages", _MESSAGES), ("/v1/responses", {"model": "test-llm", "input": "Hi"}),
            ):
                resp = client.post(path, json=body, headers=auth_header("sk-plain"))
                assert resp.status_code == 200 and resp.headers.get("X-Overflow"), path
        assert [c[0] for c in seen["calls"]] == ["azure"] * 4
        assert all(r.overflow and r.backend == "azure" for r in _rows(plain_user.id))
        # count_tokens bills nothing and is not sent to the cloud
        counted = client.post("/v1/messages/count_tokens", json=_MESSAGES, headers=auth_header("sk-plain"))
        assert counted.status_code == 200 and "X-Overflow" not in counted.headers
        assert [c[0] for c in seen["calls"]].count("azure") == 4
        assert len(_rows(plain_user.id)) == 4

    def test_responses_has_no_bedrock_and_messages_does(self, client, db_session, plain_user):
        mock, seen = _downstream(client)
        with _settings(), _overflows_to("bedrock-claude"), mock:
            _on_the_cloud(db_session, plain_user)
            client.post("/v1/responses", json={"model": "test-llm", "input": "Hi"}, headers=auth_header("sk-plain"))
            assert [c[0] for c in seen["calls"]] == ["vllm"]
            resp = client.post("/v1/messages", json=_MESSAGES, headers=auth_header("sk-plain"))
        assert resp.status_code == 200 and resp.json()["content"][0]["text"] == "from bedrock"
        assert [c[0] for c in seen["calls"]] == ["vllm", "bedrock"]
        assert [(r.backend, r.overflow) for r in _rows(plain_user.id)][-1] == ("bedrock", True)

    def test_a_stream_is_marked_too(self, client, db_session, plain_user):
        lines = [
            'data: {"type":"response.output_text.delta","delta":"Hello"}',
            'data: {"type":"response.completed","response":{"status":"completed",'
            '"usage":{"input_tokens":5,"output_tokens":2,"total_tokens":7}}}',
        ]
        mock = client.__httpx_mock__
        with _settings(), _overflows_to(), \
                patch.object(mock, "build_request", return_value=httpx.Request("POST", "https://test.openai.azure.com/x")), \
                patch.object(mock, "send", return_value=FakeStreamResponse(lines)):
            _on_the_cloud(db_session, plain_user)
            resp = client.post(
                "/v1/chat/completions", json={**_CHAT, "stream": True}, headers=auth_header("sk-plain"),
            )
        assert resp.status_code == 200 and "Hello" in resp.text
        assert resp.headers["X-Overflow"]
        [row] = _rows(plain_user.id)
        assert (row.backend, row.overflow) == ("azure", True)

    def test_the_guardrail_screens_local_traffic_only(self, client, db_session, plain_user):
        mock, _ = _downstream(client)
        with _settings(), _overflows_to(), mock, patch("app.routers.v1_api.guardrail.screen") as screen:
            _on_the_cloud(db_session, plain_user)
            client.post("/v1/chat/completions", json=_CHAT, headers=auth_header("sk-plain"))
        screen.assert_not_called()

    def test_an_account_with_its_own_cloud_cap_spent_stays_on_prem(self, client, db_session, test_user):
        test_user.cloud_daily_limit_usd = 1.0
        db_session.add(test_user)
        db_session.commit()
        _used(db_session, test_user, model="azure-gpt-4", backend="azure", cost="1.5")
        mock, seen = _downstream(client)
        with _settings(), _overflows_to(), mock:
            _on_the_cloud(db_session, test_user)
            resp = client.post("/v1/chat/completions", json=_CHAT, headers=auth_header())
        assert resp.status_code == 200  # not a 429: it asked for an on-prem model
        assert seen["calls"] == [("vllm", "real-llm-v1")]

    def test_it_counts_toward_the_accounts_daily_limit(self, client, db_session, plain_user):
        from app.core.deps import get_today_usage
        mock, _ = _downstream(client)
        with _settings(), _overflows_to(), mock:
            _on_the_cloud(db_session, plain_user)
            client.post("/v1/chat/completions", json=_CHAT, headers=auth_header("sk-plain"))
        today = get_today_usage(plain_user)
        assert today["total_cost_usd"] > 0 and today["total_cost_usd"] == today["azure_cost_usd"]

    def test_a_broken_snapshot_never_fails_the_request(self, client, db_session, plain_user):
        mock, seen = _downstream(client)
        with _settings(), _overflows_to(), mock, \
                patch("app.routers.v1_api.overflow.target_for", side_effect=RuntimeError("boom")):
            resp = client.post("/v1/chat/completions", json=_CHAT, headers=auth_header("sk-plain"))
        assert resp.status_code == 200 and seen["calls"] == [("vllm", "real-llm-v1")]

    def test_the_concurrency_pool_is_the_cloud(self, client, db_session, plain_user):
        seen: dict = {}

        async def fake_post(url, **kwargs):
            with Session(_test_engine) as s:
                seen["pools"] = [lease.pool for lease in s.exec(select(ConcurrencyLease)).all()]
            return make_httpx_response(200, _OK_AZURE if "azure" in url else _OK_CHAT)

        with _settings(), _overflows_to(), \
                patch("app.core.deps.get_concurrency_settings", return_value=("enforce", 5)), \
                patch("app.core.deps.get_concurrency_cloud_limit", return_value=10), \
                patch.object(client.__httpx_mock__, "post", side_effect=fake_post):
            _on_the_cloud(db_session, plain_user)
            client.post("/v1/chat/completions", json=_CHAT, headers=auth_header("sk-plain"))
        assert seen["pools"] == ["cloud"]


# ── What the account sees ──

class TestUsage:
    def _overflow_once(self, client, db_session, user):
        mock, _ = _downstream(client)
        with _settings(), _overflows_to(), mock:
            _on_the_cloud(db_session, user)
            client.post("/v1/chat/completions", json=_CHAT, headers=auth_header(user.api_key))

    def test_shown_as_overflow_even_without_cloud_access(self, client, db_session, plain_user):
        self._overflow_once(client, db_session, plain_user)
        month = client.get("/v1/usage", headers=auth_header("sk-plain")).json()["month"]
        [row] = month["models"]
        assert (row["model"], row["backend"], row["overflow"], row["requests"]) == ("azure-gpt-4", "azure", True, 1)
        assert month["cost_usd"] == row["cost_usd"] > 0
        # the dashboard: the hero says so, and Cost by Model lists the row
        page = client.get("/dashboard", headers=web_auth_header(sub="plain")).text
        assert "of it is cloud overflow" in page
        card = client.get("/dashboard/api/model-breakdown?period=day", headers=web_auth_header(sub="plain")).json()
        [group] = card["groups"]
        assert [(m["model"], m["overflow"]) for m in group["rows"]] == [("azure-gpt-4", True)]
        assert card["total_cost_usd"] == row["cost_usd"]

    def test_the_dashboard_says_nothing_of_it_otherwise(self, client, db_session, test_user):
        _used(db_session, test_user, model="azure-gpt-4", backend="azure", cost="2")
        page = client.get("/dashboard", headers=web_auth_header(sub="testuser")).text
        assert "cloud overflow" not in page
        card = client.get("/dashboard/api/model-breakdown", headers=web_auth_header(sub="testuser")).json()
        assert [m["overflow"] for g in card["groups"] for m in g["rows"]] == [False]

    def test_cloud_usage_it_did_not_get_by_overflow_stays_hidden(self, client, db_session, plain_user):
        _used(db_session, plain_user, model="azure-gpt-4", backend="azure", cost="2")  # from when it had access
        self._overflow_once(client, db_session, plain_user)
        rows = client.get("/v1/usage", headers=auth_header("sk-plain")).json()["month"]["models"]
        assert [(r["model"], r["overflow"]) for r in rows] == [("azure-gpt-4", True)]

    def test_apart_from_the_same_model_chosen_by_name(self, client, db_session, test_user):
        self._overflow_once(client, db_session, test_user)
        with _downstream(client)[0]:
            client.post("/v1/chat/completions", json={**_CHAT, "model": "azure-gpt-4"}, headers=auth_header())
        rows = client.get("/v1/usage", headers=auth_header()).json()["month"]["models"]
        assert [(r["model"], r["overflow"], r["requests"]) for r in rows] == [
            ("azure-gpt-4", False, 1), ("azure-gpt-4", True, 1),
        ]

    def test_the_same_once_rolled_up(self, db_session, plain_user):
        day = datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)
        _used(db_session, plain_user, 100, model="azure-gpt-4", backend="azure", overflow_row=True, cost="1", at=day)
        _used(db_session, plain_user, 200, model="azure-gpt-4", backend="azure", cost="2", at=day)
        first, last = day.date(), day.date() + timedelta(days=1)
        dims = ("model", "backend", "overflow")
        before = usage_rollup.aggregate(db_session, first, last, dims)
        usage_rollup.advance(db_session, now=day + timedelta(days=2))
        assert usage_rollup.rolled_through(db_session) >= first
        after = usage_rollup.aggregate(db_session, first, last, dims)
        assert before == after
        assert after[("azure-gpt-4", "azure", True)]["input_tokens"] == 100
        assert after[("azure-gpt-4", "azure", False)]["input_tokens"] == 200


# ── The admin's side ──

def _admin():
    return web_auth_header(sub="admin", scopes=["admin"])


class TestAdmin:
    def test_the_card_is_on_the_limits_tab_and_lists_who_is_on_the_cloud(self, client, db_session, admin_user, plain_user):
        _used(db_session, plain_user, model="azure-gpt-4", backend="azure", overflow_row=True, cost="1.5")
        with _settings(), _overflows_to(), _queue(9):
            _stay(db_session, "plain")
            overflow.tick(db_session)
            body = client.get("/admin", headers=_admin()).text
        card = body[body.index('id="cloud-overflow"'):]
        card = card[:card.index('<div data-panel="settings">')]
        assert body.index('data-panel="limits"') < body.index('id="cloud-overflow"')
        assert "test-llm" in card and "azure-gpt-4" in card
        assert "On the cloud now (1)" in card and ">plain" in card
        assert "$1.50" in card and "of $50" in card

    def test_saving_the_settings(self, client, admin_user):
        form = {
            "mode": "enforce", "daily_budget_usd": "25", "budget_stop_pct": "90", "waiting_high": "12",
            "waiting_low": "3", "consecutive": "2", "grab_max": "3", "release_per_round": "1",
            "min_stay_min": "10", "idle_min": "5",
        }
        with patch("app.routers.admin.settings.set_overflow_settings") as save:
            resp = client.post("/admin/overflow", data=form, headers={**_admin(), "Accept": "application/json"})
        assert resp.status_code == 200
        saved = save.call_args.args[0]
        assert saved["mode"] == "enforce" and saved["daily_budget_usd"] == 25.0
        assert (saved["waiting_high"], saved["waiting_low"], saved["grab_max"]) == (12, 3, 3)

    @pytest.mark.parametrize("change", [
        {"mode": "sometimes"}, {"waiting_low": "12", "waiting_high": "12"}, {"grab_max": "0"},
        {"daily_budget_usd": "-1"}, {"consecutive": "two"},
    ])
    def test_what_the_settings_refuse(self, client, admin_user, change):
        with patch("app.core.config._save_app_keys") as write:
            resp = client.post("/admin/overflow", data={"mode": "monitor", **change}, headers=_admin())
        assert resp.status_code == 400
        write.assert_not_called()

    def test_switching_it_off_moves_everyone_back_at_once(self, client, db_session, admin_user, plain_user):
        _stay(db_session, "plain")
        with patch("app.routers.admin.settings.set_overflow_settings"):
            client.post("/admin/overflow", data={"mode": "off"}, headers=_admin())
        assert _open(db_session) == []

    def test_only_admins(self, client, test_user):
        resp = client.post("/admin/overflow", data={"mode": "enforce"}, headers=web_auth_header(sub="testuser"))
        assert resp.status_code == 403

    def test_turning_an_account_off_takes_it_back_now(self, client, db_session, admin_user, plain_user):
        with _settings(), _overflows_to():
            _on_the_cloud(db_session, plain_user)
            assert overflow.target_for(plain_user, TEST_MODEL_ROUTING["test-llm"]) == "azure-gpt-4"
            resp = client.post(
                f"/admin/users/{plain_user.id}/toggle-overflow", headers={**_admin(), "Accept": "application/json"},
            )
            assert resp.json() == {"ok": True, "can_overflow_cloud": False}
            assert _open(db_session) == []
            db_session.refresh(plain_user)
            assert overflow.target_for(plain_user, TEST_MODEL_ROUTING["test-llm"]) is None
        back = client.post(
            f"/admin/users/{plain_user.id}/toggle-overflow", headers={**_admin(), "Accept": "application/json"},
        )
        assert back.json()["can_overflow_cloud"] is True

    def test_the_admin_api_takes_the_flag(self, client, db_session, admin_user, plain_user):
        _stay(db_session, "plain")
        resp = client.patch(f"/admin/users/{plain_user.id}", json={"can_overflow_cloud": False}, headers=_admin())
        assert resp.status_code == 200 and _open(db_session) == []
        db_session.refresh(plain_user)
        assert plain_user.can_overflow_cloud is False
        listed = {u["username"]: u for u in client.get("/admin/users", headers=_admin()).json()}
        assert listed["plain"]["can_overflow_cloud"] is False
        bad = client.patch(f"/admin/users/{plain_user.id}", json={"can_overflow_cloud": "false"}, headers=_admin())
        assert bad.status_code == 400

    def test_accounts_kept_on_prem_are_special_access(self, client, db_session, admin_user, plain_user):
        from app.routers.admin.users import _special_access_summary
        _person(db_session, "robot", is_service_account=True, can_overflow_cloud=False)  # never moved anyway
        plain_user.can_overflow_cloud = False
        db_session.add(plain_user)
        db_session.commit()
        summary = {row["key"]: row for row in _special_access_summary(db_session)}
        assert (summary["no_overflow"]["users"], summary["no_overflow"]["apps"]) == (1, 0)
        panel = client.get("/admin/user-table?kind=users&status=no_overflow", headers=_admin()).text
        assert 'data-username="plain"' in panel and 'data-username="admin"' not in panel
        assert 'data-action="overflow"' in panel
        apps = client.get("/admin/user-table?kind=apps", headers=_admin()).text
        assert 'data-action="overflow"' not in apps  # a service account has no such switch

    def test_deleting_an_account_leaves_its_stays(self, client, db_session, admin_user, plain_user):
        _stay(db_session, "plain")
        resp = client.post(f"/admin/users/{plain_user.id}/delete", headers={**_admin(), "Accept": "application/json"})
        assert resp.status_code == 200
        assert len(db_session.exec(select(OverflowStay)).all()) == 1
        with _settings(), _overflows_to(), _queue(5):
            overflow.tick(db_session)  # and the next round ends them
        assert _open(db_session) == []


class TestModelConfig:
    def _put(self, client, change: dict):
        body = {
            "models": {"test-llm": {"base_url": "http://mock:8000/v1", "real_model": "m", "type": "llm", **change}},
            "pricing": {"_default": {"input_price_per_1m": 0.1, "output_price_per_1m": 0.1}},
            "fallback": {},
            "azure_models": {
                "azure-gpt-4": {"type": "llm", "endpoint": "https://x.openai.azure.com", "deployment": "d", "api_key": "k"},
                "azure-embed": {"type": "embedding", "endpoint": "https://x.openai.azure.com", "deployment": "e", "api_key": "k"},
            },
            "azure_fallback": {},
        }
        with patch("app.routers.admin.model_config.save_config") as save:
            resp = client.put("/admin/api/config", json=body, headers=_admin())
        return resp, save

    def test_a_cloud_chat_model_is_accepted(self, client, admin_user):
        resp, save = self._put(client, {"overflow_to": "azure-gpt-4", "overflow_waiting_high": 20, "overflow_waiting_low": 4})
        assert resp.status_code == 200, resp.text
        assert save.call_args.args[0]["test-llm"]["overflow_to"] == "azure-gpt-4"

    @pytest.mark.parametrize("change, said", [
        ({"overflow_to": "no-such-model"}, "not an Azure or AWS model"),
        ({"overflow_to": "test-vlm"}, "not an Azure or AWS model"),
        ({"overflow_to": "azure-embed"}, "must be llm or vlm"),
        ({"overflow_to": "azure-gpt-4", "overflow_waiting_high": 0}, "at least 1"),
        ({"overflow_to": "azure-gpt-4", "overflow_waiting_high": 5, "overflow_waiting_low": 5}, "below"),
        ({"overflow_to": "azure-gpt-4", "overflow_waiting_low": True}, "whole number"),
    ])
    def test_what_is_refused(self, client, admin_user, change, said):
        resp, save = self._put(client, change)
        assert resp.status_code == 400 and said in resp.json()["detail"]
        save.assert_not_called()

    def test_the_keys_survive_a_config_round_trip_and_stay_out_of_the_model_list(self, client):
        from app.core import config
        raw = {"models": {"llm": {"m": {
            "base_url": "http://x/v1", "overflow_to": "cloud-m", "overflow_waiting_high": 20, "overflow_waiting_low": "",
        }}}}
        routing = config._build_config(raw)[1]
        assert routing["m"]["overflow_to"] == "cloud-m" and routing["m"]["overflow_waiting_high"] == 20
        assert "overflow_waiting_low" not in routing["m"]
        with _overflows_to():
            listed = client.get("/v1/models", headers=auth_header()).json()["data"]
        assert all("overflow_to" not in m for m in listed)


class TestSettings:
    def test_a_typo_never_turns_it_on(self):
        from app.core import config
        with patch.dict(config.APP_CONFIG, {
            "overflow_mode": "enfroce", "overflow_waiting_high": "many", "overflow_waiting_low": 50,
            "overflow_daily_budget_usd": "lots", "overflow_grab_max": True,
        }):
            s = config.get_overflow_settings()
        assert s["mode"] == "off" and s["daily_budget_usd"] == 0.0
        assert (s["waiting_high"], s["waiting_low"], s["grab_max"]) == (8, 2, 2)

    def test_defaults(self):
        from app.core import config
        with patch.dict(config.APP_CONFIG, {}, clear=True):
            s = config.get_overflow_settings()
        assert s == OVERFLOW_OFF
