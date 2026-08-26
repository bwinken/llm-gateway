"""
Tests for POST /v1/chat/completions/render and /chat/completions/render.

The gateway forwards the body to the downstream vLLM
``/chat/completions/render`` endpoint (a debug aid: the request is rendered
through the model's chat template but never generated from), swapping the
model alias to its real_model on the way down and back to the alias on the
way up. Rendering is not billable — no row is written to ``usage_logs``.
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


class TestChatCompletionsRender:

    def test_basic_render(self, client, test_user, db_session: Session):
        downstream_body = {
            "request_id": "chatcmpl-abc",
            "token_ids": [1, 2, 3, 4],
            "model": "real-llm-v1",
            "sampling_params": {"temperature": 0.7, "max_tokens": 128},
            "stream": False,
        }
        post_coro, captured = _make_post_coro_capture(
            make_httpx_response(200, downstream_body)
        )
        client.__httpx_mock__.post = post_coro

        resp = client.post(
            "/v1/chat/completions/render",
            json={
                "model": "test-llm",
                "messages": [{"role": "user", "content": "hello world"}],
                "temperature": 0.7,
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["token_ids"] == [1, 2, 3, 4]
        assert data["sampling_params"]["temperature"] == 0.7
        # real_model is swapped back to the user-facing alias
        assert data["model"] == "test-llm"

        # Downstream got the real model and the verbatim body
        assert captured["body"]["model"] == "real-llm-v1"
        assert captured["body"]["messages"] == [
            {"role": "user", "content": "hello world"}
        ]
        assert captured["body"]["temperature"] == 0.7
        # Routed to the downstream's own /v1/chat/completions/render — the
        # render endpoint lives under vLLM's /v1 prefix (unlike /tokenize,
        # which sits at the server root), so the configured base_url is used
        # as-is with the suffix appended.
        assert captured["url"] == "http://mock-llm:8000/v1/chat/completions/render"

        # Rendering is a debug/metadata call — never billed
        rows = db_session.exec(
            select(UsageLog).where(UsageLog.user_id == test_user.id)
        ).all()
        assert rows == []

    def test_alias_without_v1_prefix(self, client, test_user):
        """``/chat/completions/render`` and ``/v1/chat/completions/render``
        are the same handler and hit the same downstream URL — the gateway's
        own prefix has no bearing on where the request lands."""
        post_coro, captured = _make_post_coro_capture(
            make_httpx_response(200, {"token_ids": [7, 8]})
        )
        client.__httpx_mock__.post = post_coro

        resp = client.post(
            "/chat/completions/render",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert resp.json()["token_ids"] == [7, 8]
        assert captured["url"] == "http://mock-llm:8000/v1/chat/completions/render"

    def test_reasoning_dialect_aligned(self, client, test_user):
        """Render must reflect what /chat/completions would actually send, so
        the reasoning / reasoning_content aliasing applies here too."""
        post_coro, captured = _make_post_coro_capture(
            make_httpx_response(200, {"token_ids": [1]})
        )
        client.__httpx_mock__.post = post_coro

        resp = client.post(
            "/v1/chat/completions/render",
            json={
                "model": "test-llm",
                "messages": [
                    {"role": "user", "content": "hi"},
                    {
                        "role": "assistant",
                        "content": "hello",
                        "reasoning_content": "thinking hard",
                    },
                ],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assistant = captured["body"]["messages"][1]
        assert assistant["reasoning"] == "thinking hard"
        assert assistant["reasoning_content"] == "thinking hard"

    def test_x_api_key(self, client, test_user):
        client.__httpx_mock__.post = _make_post_coro(
            make_httpx_response(200, {"token_ids": [1, 2]})
        )
        resp = client.post(
            "/v1/chat/completions/render",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
            headers={"x-api-key": "sk-testkey123"},
        )
        assert resp.status_code == 200

    def test_401_without_auth(self, client):
        resp = client.post(
            "/v1/chat/completions/render",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code in (401, 403)

    def test_invalid_json_body(self, client, test_user):
        resp = client.post(
            "/v1/chat/completions/render",
            content=b"not json",
            headers={**auth_header(), "Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_downstream_error_returns_502(self, client, test_user):
        client.__httpx_mock__.post = _make_post_coro_raise(
            Exception("connection refused")
        )
        resp = client.post(
            "/v1/chat/completions/render",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 502

    def test_downstream_404_propagated(self, client, test_user):
        """A vLLM too old to serve /chat/completions/render answers 404 — the
        gateway propagates it instead of pretending the endpoint worked."""
        client.__httpx_mock__.post = _make_post_coro(
            make_httpx_response(404, {"detail": "Not Found"})
        )
        resp = client.post(
            "/v1/chat/completions/render",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 404

    def test_unknown_model_falls_back(self, client, test_user):
        post_coro, captured = _make_post_coro_capture(
            make_httpx_response(200, {"token_ids": [1]})
        )
        client.__httpx_mock__.post = post_coro

        resp = client.post(
            "/v1/chat/completions/render",
            json={
                "model": "does-not-exist",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert "X-Model-Fallback" in resp.headers
        assert captured["body"]["model"] in ("real-llm-v1", "real-vlm-v1")

    def test_azure_alias_stays_on_vllm(self, client, test_user):
        """On-prem only: an Azure-configured alias is not dispatched to Azure
        here — it falls back through _resolve_model to a vLLM route, exactly
        like any other alias the vLLM side doesn't know."""
        post_coro, captured = _make_post_coro_capture(
            make_httpx_response(200, {"token_ids": [1]})
        )
        client.__httpx_mock__.post = post_coro

        resp = client.post(
            "/v1/chat/completions/render",
            json={
                "model": "azure-gpt-4",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert captured["body"]["model"] in ("real-llm-v1", "real-vlm-v1")
