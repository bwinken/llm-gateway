"""Tests for the /azure/v1/* endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock

from tests.conftest import FakeStreamResponse, auth_header


class TestAzureModelsListing:
    def test_list_azure_models_returns_configured_aliases(self, client):
        resp = client.get("/azure/v1/models", headers=auth_header())
        assert resp.status_code == 200
        ids = [m["id"] for m in resp.json()["data"]]
        assert "azure-gpt-4" in ids
        assert "azure-embed" in ids

    def test_azure_models_marked_as_azure_owner(self, client):
        resp = client.get("/azure/v1/models", headers=auth_header())
        for entry in resp.json()["data"]:
            assert entry["owned_by"] == "azure-openai"

    def test_azure_models_requires_auth(self, client):
        resp = client.get("/azure/v1/models")
        assert resp.status_code == 401

    def test_azure_blocked_without_can_use_azure(self, client, db_session, test_user):
        # Revoke the default-granted Azure access for this test
        test_user.can_use_azure = False
        db_session.add(test_user); db_session.commit()
        resp = client.get("/azure/v1/models", headers=auth_header())
        assert resp.status_code == 403


class TestAzureChatCompletions:
    def test_chat_completion_basic(self, client):
        client.__httpx_mock__.post = AsyncMock(
            return_value=_fake_response(
                200,
                {
                    "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                },
            )
        )
        resp = client.post(
            "/azure/v1/chat/completions",
            json={"model": "azure-gpt-4", "messages": [{"role": "user", "content": "hello"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "hi"

    def test_chat_completion_uses_azure_url_and_api_key_header(self, client):
        captured: dict = {}

        async def fake_post(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers", {})
            captured["json"] = kwargs.get("json", {})
            return _fake_response(
                200,
                {
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        client.__httpx_mock__.post = fake_post
        resp = client.post(
            "/azure/v1/chat/completions",
            json={"model": "azure-gpt-4", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        # URL: {endpoint}/openai/deployments/{deployment}/chat/completions?api-version=...
        assert "openai/deployments/gpt-4-deploy/chat/completions" in captured["url"]
        assert "api-version=2024-08-01-preview" in captured["url"]
        # Header: api-key (not Authorization Bearer)
        assert captured["headers"].get("api-key") == "azure-test-key"
        assert "Authorization" not in captured["headers"]
        # Body.model is stripped (Azure routes by URL deployment)
        assert "model" not in captured["json"]

    def test_unknown_alias_returns_400(self, client):
        resp = client.post(
            "/azure/v1/chat/completions",
            json={"model": "not-configured", "messages": []},
            headers=auth_header(),
        )
        assert resp.status_code == 400


class TestAzureEmbeddings:
    def test_embeddings_basic(self, client):
        client.__httpx_mock__.post = AsyncMock(
            return_value=_fake_response(
                200,
                {
                    "data": [{"embedding": [0.1, 0.2]}],
                    "usage": {"prompt_tokens": 4, "total_tokens": 4},
                },
            )
        )
        resp = client.post(
            "/azure/v1/embeddings",
            json={"model": "azure-embed", "input": ["hello"]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert resp.json()["data"][0]["embedding"] == [0.1, 0.2]


class TestAzureMessagesAnthropic:
    def test_anthropic_messages_translation(self, client):
        client.__httpx_mock__.post = AsyncMock(
            return_value=_fake_response(
                200,
                {
                    "id": "x",
                    "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                },
            )
        )
        resp = client.post(
            "/azure/v1/messages",
            json={
                "model": "azure-gpt-4",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "hello"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        body = resp.json()
        # Anthropic-shaped response
        assert body["type"] == "message"
        assert body["role"] == "assistant"
        assert body["content"][0]["type"] == "text"
        assert body["usage"]["input_tokens"] == 7
        assert body["usage"]["output_tokens"] == 3

    def test_count_tokens_returns_estimate(self, client):
        # Azure does not expose tokenize; we always return chars/4 estimate
        resp = client.post(
            "/azure/v1/messages/count_tokens",
            json={
                "model": "azure-gpt-4",
                "messages": [{"role": "user", "content": "hello world"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert "input_tokens" in resp.json()
        assert resp.json()["input_tokens"] >= 1


def _fake_response(status_code: int, json_body: dict):
    """Build a minimal mock httpx.Response."""
    class _Resp:
        def __init__(self, code, body):
            self.status_code = code
            self._body = body
            self.text = str(body)
        def json(self):
            return self._body
    return _Resp(status_code, json_body)
