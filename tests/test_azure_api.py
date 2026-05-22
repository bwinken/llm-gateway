"""Tests for the /azure/v1/* endpoints.

The gateway forwards every Azure LLM/VLM call to the Responses API
(``{endpoint}/openai/v1/responses``) and translates back to the public
surface (OpenAI chat completions or Anthropic Messages). Mocked downstream
responses therefore use the Responses API shape, not chat completions.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx

from tests.conftest import FakeStreamResponse, auth_header


class TestAzureModelsListing:
    def test_list_azure_models_returns_configured_aliases(self, client):
        resp = client.get("/azure/v1/models", headers=auth_header())
        assert resp.status_code == 200
        ids = [m["id"] for m in resp.json()["data"]]
        assert "azure-gpt-4" in ids

    def test_azure_models_marked_as_azure_owner(self, client):
        resp = client.get("/azure/v1/models", headers=auth_header())
        for entry in resp.json()["data"]:
            assert entry["owned_by"] == "azure-openai"

    def test_azure_models_requires_auth(self, client):
        resp = client.get("/azure/v1/models")
        assert resp.status_code == 401

    def test_azure_blocked_without_can_use_azure(self, client, db_session, test_user):
        test_user.can_use_azure = False
        db_session.add(test_user); db_session.commit()
        resp = client.get("/azure/v1/models", headers=auth_header())
        assert resp.status_code == 403


class TestAzureChatCompletions:
    def test_chat_completion_basic(self, client):
        client.__httpx_mock__.post = AsyncMock(
            return_value=_fake_response(200, _responses_payload("hi", 5, 2)),
        )
        resp = client.post(
            "/azure/v1/chat/completions",
            json={"model": "azure-gpt-4", "messages": [{"role": "user", "content": "hello"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        # Gateway returns chat-completions-shaped body, translated from Responses.
        assert resp.json()["choices"][0]["message"]["content"] == "hi"
        assert resp.json()["choices"][0]["finish_reason"] == "stop"
        assert resp.json()["usage"]["prompt_tokens"] == 5
        assert resp.json()["usage"]["completion_tokens"] == 2

    def test_chat_completion_uses_responses_url_and_api_key_header(self, client):
        captured: dict = {}

        async def fake_post(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers", {})
            captured["json"] = kwargs.get("json", {})
            return _fake_response(200, _responses_payload("ok", 1, 1))

        client.__httpx_mock__.post = fake_post
        resp = client.post(
            "/azure/v1/chat/completions",
            json={"model": "azure-gpt-4", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        # URL: v1 Responses surface (no deployment in URL, no api-version)
        assert captured["url"].endswith("/openai/v1/responses")
        assert "api-version=" not in captured["url"]
        assert "deployments/" not in captured["url"]
        # Auth header is api-key (not Authorization Bearer)
        assert captured["headers"].get("api-key") == "azure-test-key"
        assert "Authorization" not in captured["headers"]
        # body.model is set to the Azure deployment name, and messages have
        # been translated to Responses `input` items.
        assert captured["json"]["model"] == "gpt-4-deploy"
        assert "messages" not in captured["json"]
        assert "input" in captured["json"]

    def test_chat_completion_translates_max_tokens(self, client):
        captured: dict = {}

        async def fake_post(url, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return _fake_response(200, _responses_payload("ok", 1, 1))

        client.__httpx_mock__.post = fake_post
        client.post(
            "/azure/v1/chat/completions",
            json={
                "model": "azure-gpt-4",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 256,
            },
            headers=auth_header(),
        )
        # Chat completions `max_tokens` -> Responses `max_output_tokens`
        assert captured["json"].get("max_output_tokens") == 256
        assert "max_tokens" not in captured["json"]

    def test_chat_completion_hoists_system_to_instructions(self, client):
        captured: dict = {}

        async def fake_post(url, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return _fake_response(200, _responses_payload("ok", 1, 1))

        client.__httpx_mock__.post = fake_post
        client.post(
            "/azure/v1/chat/completions",
            json={
                "model": "azure-gpt-4",
                "messages": [
                    {"role": "system", "content": "You are helpful."},
                    {"role": "user", "content": "hi"},
                ],
            },
            headers=auth_header(),
        )
        assert captured["json"].get("instructions") == "You are helpful."
        # System message is not in `input`
        roles = [item.get("role") for item in captured["json"].get("input", []) if "role" in item]
        assert "system" not in roles

    def test_unknown_alias_returns_400(self, client):
        resp = client.post(
            "/azure/v1/chat/completions",
            json={"model": "not-configured", "messages": []},
            headers=auth_header(),
        )
        assert resp.status_code == 400

    def test_system_only_message_injects_probe_placeholder(self, client):
        """Roo Code's connection-validate probes send only a system
        message, which collapses to empty Responses `input`. Rather than
        400, the gateway injects a minimal user placeholder so the probe
        gets a 200 and the IDE marks the provider as working."""
        captured: dict = {}

        async def fake_post(url, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return _fake_response(200, _responses_payload("ok", 1, 1))

        client.__httpx_mock__.post = fake_post
        resp = client.post(
            "/azure/v1/chat/completions",
            json={
                "model": "azure-gpt-4",
                "messages": [{"role": "system", "content": "be helpful"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        # Azure call DID happen, and the body has non-empty `input`.
        assert "input" in captured["json"]
        assert len(captured["json"]["input"]) >= 1
        # System hoisted to instructions, placeholder went into input.
        assert captured["json"].get("instructions") == "be helpful"


class TestAzureResponsesPassthrough:
    """The /azure/v1/responses route is a pure pass-through for clients
    that already speak Responses API natively. The only mutation is
    body.model from alias to deployment."""

    def test_passthrough_basic(self, client):
        client.__httpx_mock__.post = AsyncMock(
            return_value=_fake_response(200, {
                "id": "resp_1",
                "status": "completed",
                "output": [{
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}],
                }],
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            }),
        )
        resp = client.post(
            "/azure/v1/responses",
            json={"model": "azure-gpt-4", "input": "hi"},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        # Body is returned as-is (Responses shape).
        body = resp.json()
        assert body["id"] == "resp_1"
        assert body["output"][0]["content"][0]["text"] == "ok"

    def test_passthrough_swaps_model_alias_to_deployment(self, client):
        captured: dict = {}

        async def fake_post(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers", {})
            captured["json"] = kwargs.get("json", {})
            return _fake_response(200, {
                "id": "resp_x", "status": "completed",
                "output": [], "usage": {"input_tokens": 1, "output_tokens": 1},
            })

        client.__httpx_mock__.post = fake_post
        resp = client.post(
            "/azure/v1/responses",
            json={
                "model": "azure-gpt-4",
                "input": "hi",
                "temperature": 0.7,  # pass-through, NOT stripped
                "previous_response_id": "resp_prev",
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        # URL: /openai/v1/responses (same as chat completions target)
        assert captured["url"].endswith("/openai/v1/responses")
        # Auth header is api-key
        assert captured["headers"].get("api-key") == "azure-test-key"
        # body.model swapped to deployment name
        assert captured["json"]["model"] == "gpt-4-deploy"
        # input passed through as-is (no translation)
        assert captured["json"]["input"] == "hi"
        # Pass-through: temperature and previous_response_id survive
        # (chat completions path strips temperature; here it's the client's call)
        assert captured["json"]["temperature"] == 0.7
        assert captured["json"]["previous_response_id"] == "resp_prev"

    def test_passthrough_unknown_alias_returns_400(self, client):
        resp = client.post(
            "/azure/v1/responses",
            json={"model": "not-configured", "input": "hi"},
            headers=auth_header(),
        )
        assert resp.status_code == 400


class TestAzureEmbeddingsRouteRemoved:
    def test_embeddings_route_is_gone(self, client):
        """Responses API has no embeddings; /azure/v1/embeddings is intentionally
        not exposed. Hitting it should 404."""
        resp = client.post(
            "/azure/v1/embeddings",
            json={"model": "azure-gpt-4", "input": ["hello"]},
            headers=auth_header(),
        )
        assert resp.status_code == 404


class TestAzureMessagesAnthropic:
    def test_anthropic_messages_translation(self, client):
        client.__httpx_mock__.post = AsyncMock(
            return_value=_fake_response(200, _responses_payload("hi", 7, 3)),
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
        assert body["type"] == "message"
        assert body["role"] == "assistant"
        assert body["content"][0]["type"] == "text"
        assert body["content"][0]["text"] == "hi"
        assert body["usage"]["input_tokens"] == 7
        assert body["usage"]["output_tokens"] == 3

    def test_count_tokens_returns_estimate(self, client):
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


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class TestAzureChatCompletionsStream:
    def test_stream_basic_translates_responses_sse_to_chat_chunks(self, client):
        sse_lines = [
            'data: {"type":"response.output_text.delta","delta":"Hello"}',
            'data: {"type":"response.output_text.delta","delta":"!"}',
            'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":5,"output_tokens":2,"total_tokens":7}}}',
        ]
        fake_stream = FakeStreamResponse(sse_lines)
        mock = client.__httpx_mock__
        mock.build_request.return_value = httpx.Request(
            "POST", "https://test.openai.azure.com/openai/v1/responses",
        )
        mock.send.return_value = fake_stream

        resp = client.post(
            "/azure/v1/chat/completions",
            json={
                "model": "azure-gpt-4",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        body = resp.text
        # Content deltas should arrive as chat-completion chunks.
        assert '"content": "Hello"' in body or '"content":"Hello"' in body
        assert '"content": "!"' in body or '"content":"!"' in body
        # Final chunk has finish_reason and usage.
        assert '"finish_reason": "stop"' in body or '"finish_reason":"stop"' in body
        assert "data: [DONE]" in body

    def test_stream_pre_flight_surfaces_4xx_as_json_error(self, client):
        err_body = (
            b'{"error":{"message":"No tool output found for function call call_X",'
            b'"type":"invalid_request_error","param":"input"}}'
        )
        fake_stream = FakeStreamResponse(
            lines=[],
            status_code=400,
            body_bytes=err_body,
        )
        mock = client.__httpx_mock__
        mock.build_request.return_value = httpx.Request(
            "POST", "https://test.openai.azure.com/openai/v1/responses",
        )
        mock.send.return_value = fake_stream

        resp = client.post(
            "/azure/v1/chat/completions",
            json={
                "model": "azure-gpt-4",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            headers=auth_header(),
        )
        # The pre-flight check converts the 4xx body to a JSON response
        # (so clients don't see an empty-but-successful stream).
        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body
        assert "No tool output found" in body["error"]["message"]


class TestAzureMessagesStream:
    def test_messages_stream_emits_anthropic_events(self, client):
        sse_lines = [
            'data: {"type":"response.output_text.delta","delta":"Hi"}',
            'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":3,"output_tokens":1}}}',
        ]
        fake_stream = FakeStreamResponse(sse_lines)
        mock = client.__httpx_mock__
        mock.build_request.return_value = httpx.Request(
            "POST", "https://test.openai.azure.com/openai/v1/responses",
        )
        mock.send.return_value = fake_stream

        resp = client.post(
            "/azure/v1/messages",
            json={
                "model": "azure-gpt-4",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        body = resp.text
        # Anthropic SSE shape: message_start + content_block_* + message_stop
        assert "event: message_start" in body
        assert "event: content_block_delta" in body
        assert "event: message_stop" in body


class TestAzureResponsesStream:
    """The /azure/v1/responses route streams raw Responses API SSE through,
    only sniffing `response.completed` for billing."""

    def test_responses_stream_passes_lines_through(self, client):
        sse_lines = [
            'event: response.output_text.delta',
            'data: {"type":"response.output_text.delta","delta":"Hello"}',
            '',
            'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":4,"output_tokens":1}}}',
        ]
        fake_stream = FakeStreamResponse(sse_lines)
        mock = client.__httpx_mock__
        mock.build_request.return_value = httpx.Request(
            "POST", "https://test.openai.azure.com/openai/v1/responses",
        )
        mock.send.return_value = fake_stream

        resp = client.post(
            "/azure/v1/responses",
            json={"model": "azure-gpt-4", "input": "hi", "stream": True},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        # SSE pass-through preserves the original event/data lines.
        body = resp.text
        assert 'event: response.output_text.delta' in body
        assert '"response.output_text.delta"' in body
        assert '"Hello"' in body
        assert '"response.completed"' in body


def _responses_payload(text: str, in_tk: int, out_tk: int, *, status: str = "completed") -> dict:
    """Build a minimal Azure Responses API non-stream payload."""
    return {
        "id": "resp_test",
        "status": status,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {
            "input_tokens": in_tk,
            "output_tokens": out_tk,
            "total_tokens": in_tk + out_tk,
        },
    }


def _fake_response(status_code: int, json_body: dict):
    class _Resp:
        def __init__(self, code, body):
            self.status_code = code
            self._body = body
            self.text = str(body)
        def json(self):
            return self._body
    return _Resp(status_code, json_body)
