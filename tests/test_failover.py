"""Tests for request-time failover and daily-limit Retry-After.

A server that just died would otherwise 502 every request until the next
30s health probe; the failover layer retries ONCE against another alive
server of the same type on connect failures (marking the dead server DOWN)
and on 502/503/504 responses (server stays UP). Streams only fail over
before anything is sent to the client.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.core.server_state import is_alive as real_is_alive, set_alive
from app.core.timeutil import seconds_until_local_midnight
from app.models.schema import UsageLog
from tests.conftest import (
    FakeStreamResponse,
    TEST_MODEL_ROUTING,
    auth_header,
    make_httpx_response,
)

_PRIMARY_URL = "http://mock-llm:8000/v1"
_BACKUP_URL = "http://mock-llm-b:8000/v1"

_CHAT_OK = {
    "id": "c1",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}

_CHAT_SSE = [
    'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}',
    'data: {"id":"c1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1}}',
    "data: [DONE]",
]


@pytest.fixture
def backup_llm():
    """Add a second llm-type server so failover has somewhere to go."""
    TEST_MODEL_ROUTING["test-llm-b"] = {
        "base_url": _BACKUP_URL,
        "real_model": "real-llm-b",
        "api_key": "B_KEY",
        "type": "llm",
    }
    yield
    TEST_MODEL_ROUTING.pop("test-llm-b", None)
    # mark_down writes the REAL health cache (is_alive is patched in
    # vllm_proxy, so routing in other tests is unaffected) — clean it up.
    set_alive(_PRIMARY_URL, True)


def _routing_post(mapping):
    """Mock POST routed by base_url prefix; raises when value is an Exception."""
    captured = {"urls": []}

    async def _post(url, *args, **kwargs):
        captured["urls"].append(str(url))
        captured["json"] = kwargs.get("json")
        for prefix, result in mapping.items():
            if str(url).startswith(prefix):
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"unexpected url {url}")

    return _post, captured


class TestConnectFailover:
    def test_chat_non_stream_fails_over_and_marks_down(self, client, test_user, backup_llm):
        post, captured = _routing_post({
            _PRIMARY_URL: httpx.ConnectError("connection refused"),
            _BACKUP_URL: make_httpx_response(200, _CHAT_OK),
        })
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "ok"
        assert [u.split("/v1/")[0] for u in captured["urls"]] == [
            "http://mock-llm:8000", "http://mock-llm-b:8000",
        ]
        # Second attempt carried the backup server's real_model.
        assert captured["json"]["model"] == "real-llm-b"
        assert "failover" in resp.headers.get("x-model-fallback", "")
        # The dead server was circuit-broken in the real health cache.
        assert real_is_alive(_PRIMARY_URL) is False

    def test_chat_stream_fails_over_before_first_byte(self, client, test_user, backup_llm):
        mock = client.__httpx_mock__
        mock.send.side_effect = [
            httpx.ConnectError("connection refused"),
            FakeStreamResponse(_CHAT_SSE),
        ]
        try:
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "test-llm", "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers=auth_header(),
            )
            assert resp.status_code == 200
            assert "hi" in resp.text
            assert "failover" in resp.headers.get("x-model-fallback", "")
        finally:
            mock.send.side_effect = None

    def test_messages_translation_fails_over(self, client, test_user, backup_llm):
        post, captured = _routing_post({
            _PRIMARY_URL: httpx.ConnectError("connection refused"),
            _BACKUP_URL: make_httpx_response(200, _CHAT_OK),
        })
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/messages",
            json={
                "model": "test-llm", "max_tokens": 50,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert resp.json()["content"][0]["text"] == "ok"
        assert len(captured["urls"]) == 2

    def test_no_backup_returns_502(self, client, test_user):
        post, captured = _routing_post({
            _PRIMARY_URL: httpx.ConnectError("connection refused"),
        })
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )

        assert resp.status_code == 502
        # Only one attempt — no distinct alive candidate to retry on.
        assert len(captured["urls"]) == 1
        set_alive(_PRIMARY_URL, True)  # cleanup real health cache


class TestStatusFailover:
    def test_503_fails_over_without_marking_down(self, client, test_user, backup_llm):
        set_alive(_PRIMARY_URL, True)
        post, captured = _routing_post({
            _PRIMARY_URL: make_httpx_response(503, {"error": "overloaded"}),
            _BACKUP_URL: make_httpx_response(200, _CHAT_OK),
        })
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert len(captured["urls"]) == 2
        # 503 = alive but overloaded; the health cache must NOT flip.
        assert real_is_alive(_PRIMARY_URL) is True

    def test_500_is_surfaced_not_failed_over(self, client, test_user, backup_llm):
        """500 is a request/model-specific error — retrying elsewhere would
        just fail again, so it surfaces as-is."""
        post, captured = _routing_post({
            _PRIMARY_URL: make_httpx_response(500, {"error": "boom"}),
            _BACKUP_URL: make_httpx_response(200, _CHAT_OK),
        })
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )

        assert resp.status_code == 500
        assert len(captured["urls"]) == 1

    def test_second_attempt_error_is_returned(self, client, test_user, backup_llm):
        """No retry loops: the failover target's own 503 is surfaced."""
        post, captured = _routing_post({
            _PRIMARY_URL: make_httpx_response(503, {"error": "overloaded"}),
            _BACKUP_URL: make_httpx_response(503, {"error": "also overloaded"}),
        })
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )

        assert resp.status_code == 503
        assert len(captured["urls"]) == 2


class TestRetryAfter:
    def test_daily_limit_429_carries_retry_after(self, client, db_session, test_user):
        db_session.add(UsageLog(
            user_id=test_user.id, model="test-llm", model_type="llm",
            input_tokens=0, output_tokens=0,
            cost_usd=Decimal(str(test_user.daily_limit_usd + 1)),
            endpoint="/v1/chat/completions",
        ))
        db_session.commit()

        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )

        assert resp.status_code == 429
        retry_after = resp.headers.get("retry-after")
        assert retry_after is not None and retry_after.isdigit()
        assert 1 <= int(retry_after) <= 86400

    def test_seconds_until_local_midnight_bounds(self):
        s = seconds_until_local_midnight()
        assert 1 <= s <= 86400
