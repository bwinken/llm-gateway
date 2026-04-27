"""
Tests for POST /v1/tokenize and /tokenize (vLLM-native pass-through).

The gateway forwards the body to the downstream vLLM ``/tokenize`` endpoint,
swapping the model alias to its real_model. Tokenize is a metadata query —
no row is written to ``usage_logs``.
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.models.schema import UsageLog
from tests.conftest import auth_header, make_httpx_response


def _make_post_coro(response):
    async def _post(*args, **kwargs):
        return response
    return _post


def _make_post_coro_capture(response):
    captured: dict = {}

    async def _post(*args, **kwargs):
        captured["url"] = args[0] if args else kwargs.get("url")
        captured["body"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        return response

    return _post, captured


def _make_post_coro_raise(exc):
    async def _post(*args, **kwargs):
        raise exc
    return _post


class TestTokenize:

    def test_messages_payload(self, client, test_user, db_session: Session):
        downstream_body = {
            "count": 12,
            "max_model_len": 32768,
            "tokens": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
        }
        post_coro, captured = _make_post_coro_capture(
            make_httpx_response(200, downstream_body)
        )
        client.__httpx_mock__.post = post_coro

        resp = client.post(
            "/v1/tokenize",
            json={
                "model": "test-llm",
                "messages": [{"role": "user", "content": "hello world"}],
                "add_generation_prompt": True,
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert resp.json() == downstream_body
        # Model alias is swapped to real_model
        assert captured["body"]["model"] == "real-llm-v1"
        # Other fields are forwarded verbatim
        assert captured["body"]["messages"] == [
            {"role": "user", "content": "hello world"}
        ]
        assert captured["body"]["add_generation_prompt"] is True
        # Forwarded to downstream /tokenize (no /v1 prefix)
        assert captured["url"].endswith("/tokenize")

        # No usage row written — tokenize is a metadata query, not billable
        rows = db_session.exec(
            select(UsageLog).where(UsageLog.user_id == test_user.id)
        ).all()
        assert rows == []

    def test_prompt_payload(self, client, test_user):
        downstream_body = {"count": 3, "max_model_len": 8192, "tokens": [1, 2, 3]}
        post_coro, captured = _make_post_coro_capture(
            make_httpx_response(200, downstream_body)
        )
        client.__httpx_mock__.post = post_coro

        resp = client.post(
            "/v1/tokenize",
            json={"model": "test-llm", "prompt": "hi there"},
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert resp.json()["count"] == 3
        assert captured["body"]["prompt"] == "hi there"
        assert captured["body"]["model"] == "real-llm-v1"

    def test_alias_without_v1_prefix(self, client, test_user):
        """``/tokenize`` mirrors ``/v1/tokenize`` for clients that prefer the
        vLLM-native path."""
        client.__httpx_mock__.post = _make_post_coro(
            make_httpx_response(200, {"count": 5, "tokens": [1, 2, 3, 4, 5]})
        )
        resp = client.post(
            "/tokenize",
            json={"model": "test-llm", "prompt": "abcde"},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 5

    def test_x_api_key(self, client, test_user):
        client.__httpx_mock__.post = _make_post_coro(
            make_httpx_response(200, {"count": 2, "tokens": [1, 2]})
        )
        resp = client.post(
            "/v1/tokenize",
            json={"model": "test-llm", "prompt": "hi"},
            headers={"x-api-key": "sk-testkey123"},
        )
        assert resp.status_code == 200

    def test_401_without_auth(self, client):
        resp = client.post(
            "/v1/tokenize",
            json={"model": "test-llm", "prompt": "hi"},
        )
        assert resp.status_code in (401, 403)

    def test_invalid_json_body(self, client, test_user):
        resp = client.post(
            "/v1/tokenize",
            content=b"not json",
            headers={**auth_header(), "Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_downstream_error_returns_502(self, client, test_user):
        client.__httpx_mock__.post = _make_post_coro_raise(
            Exception("connection refused")
        )
        resp = client.post(
            "/v1/tokenize",
            json={"model": "test-llm", "prompt": "hi"},
            headers=auth_header(),
        )
        assert resp.status_code == 502

    def test_downstream_non_200_propagated(self, client, test_user):
        client.__httpx_mock__.post = _make_post_coro(
            make_httpx_response(404, {"detail": "Not Found"})
        )
        resp = client.post(
            "/v1/tokenize",
            json={"model": "test-llm", "prompt": "hi"},
            headers=auth_header(),
        )
        assert resp.status_code == 404

    def test_unknown_model_falls_back(self, client, test_user):
        """Unknown alias should fall back to an alive llm/vlm and tag the
        response with X-Model-Fallback."""
        post_coro, captured = _make_post_coro_capture(
            make_httpx_response(200, {"count": 1, "tokens": [1]})
        )
        client.__httpx_mock__.post = post_coro

        resp = client.post(
            "/v1/tokenize",
            json={"model": "does-not-exist", "prompt": "hi"},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert "X-Model-Fallback" in resp.headers
        # Forwarded with the fallback's real_model, not the unknown alias
        assert captured["body"]["model"] in ("real-llm-v1", "real-vlm-v1")
