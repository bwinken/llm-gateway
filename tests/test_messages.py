"""
Tests for POST /v1/messages (Anthropic Messages API compatibility).

The gateway translates Anthropic requests to OpenAI chat completions for the
downstream vLLM server, then translates the response back to Anthropic format.
"""

from __future__ import annotations

import json

import httpx

from app.services.anthropic_adapter import (
    AnthropicStreamTranslator,
    anthropic_to_openai_request,
    openai_to_anthropic_response,
)
from tests.conftest import FakeStreamResponse, auth_header, make_httpx_response


# ---------------------------------------------------------------------------
# Helpers (mirrors test_chat_completions.py)
# ---------------------------------------------------------------------------

def make_post_coro(response):
    async def _post(*args, **kwargs):
        return response
    return _post


def make_post_coro_capture(response):
    """Returns (post_coro, captured) where captured["body"] holds the JSON sent."""
    captured: dict = {}

    async def _post(*args, **kwargs):
        captured["body"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        return response

    return _post, captured


def make_post_coro_raise(exc):
    async def _post(*args, **kwargs):
        raise exc
    return _post


# ---------------------------------------------------------------------------
# Pure translation unit tests
# ---------------------------------------------------------------------------

class TestRequestTranslation:

    def test_simple_text_request(self):
        body = {
            "model": "claude-3-opus",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        out = anthropic_to_openai_request(body)
        assert out["model"] == "claude-3-opus"
        assert out["max_tokens"] == 100
        assert out["messages"] == [{"role": "user", "content": "Hello"}]

    def test_system_prompt_string(self):
        body = {
            "model": "x",
            "system": "You are helpful",
            "messages": [{"role": "user", "content": "hi"}],
        }
        out = anthropic_to_openai_request(body)
        assert out["messages"][0] == {"role": "system", "content": "You are helpful"}
        assert out["messages"][1]["content"] == "hi"

    def test_system_prompt_list(self):
        body = {
            "model": "x",
            "system": [{"type": "text", "text": "You are helpful"}],
            "messages": [{"role": "user", "content": "hi"}],
        }
        out = anthropic_to_openai_request(body)
        assert out["messages"][0]["content"] == "You are helpful"

    def test_image_block_base64(self):
        body = {
            "model": "x",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What's in this image?"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "iVBORw0KGgo=",
                            },
                        },
                    ],
                }
            ],
        }
        out = anthropic_to_openai_request(body)
        msg = out["messages"][0]
        assert msg["role"] == "user"
        assert isinstance(msg["content"], list)
        assert msg["content"][0]["type"] == "text"
        assert msg["content"][1]["type"] == "image_url"
        assert msg["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_tools_translation(self):
        body = {
            "model": "x",
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Get weather",
                    "input_schema": {
                        "type": "object",
                        "properties": {"location": {"type": "string"}},
                        "required": ["location"],
                    },
                }
            ],
            "tool_choice": {"type": "auto"},
        }
        out = anthropic_to_openai_request(body)
        assert out["tools"][0]["type"] == "function"
        assert out["tools"][0]["function"]["name"] == "get_weather"
        assert out["tools"][0]["function"]["parameters"]["required"] == ["location"]
        assert out["tool_choice"] == "auto"

    def test_tool_use_and_result_round_trip(self):
        body = {
            "model": "x",
            "messages": [
                {"role": "user", "content": "weather in SF?"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_01",
                            "name": "get_weather",
                            "input": {"location": "SF"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_01",
                            "content": "72F sunny",
                        }
                    ],
                },
            ],
        }
        out = anthropic_to_openai_request(body)
        # 3 messages: user, assistant (tool_calls), tool result
        roles = [m["role"] for m in out["messages"]]
        assert roles == ["user", "assistant", "tool"]
        assistant_msg = out["messages"][1]
        assert assistant_msg["tool_calls"][0]["function"]["name"] == "get_weather"
        args = json.loads(assistant_msg["tool_calls"][0]["function"]["arguments"])
        assert args["location"] == "SF"
        tool_msg = out["messages"][2]
        assert tool_msg["tool_call_id"] == "toolu_01"
        assert tool_msg["content"] == "72F sunny"

    def test_stop_sequences_mapped(self):
        body = {
            "model": "x",
            "messages": [{"role": "user", "content": "hi"}],
            "stop_sequences": ["\n\n"],
        }
        out = anthropic_to_openai_request(body)
        assert out["stop"] == ["\n\n"]


class TestResponseTranslation:

    def test_basic_text_response(self):
        openai_resp = {
            "id": "chatcmpl-1",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello there!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 8, "completion_tokens": 3},
        }
        out = openai_to_anthropic_response(openai_resp, "claude-3-opus")
        assert out["type"] == "message"
        assert out["role"] == "assistant"
        assert out["model"] == "claude-3-opus"
        assert out["content"] == [{"type": "text", "text": "Hello there!"}]
        assert out["stop_reason"] == "end_turn"
        assert out["usage"] == {"input_tokens": 8, "output_tokens": 3}

    def test_tool_call_response(self):
        openai_resp = {
            "id": "chatcmpl-2",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"location": "SF"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 30, "completion_tokens": 12},
        }
        out = openai_to_anthropic_response(openai_resp, "claude-3-opus")
        assert out["stop_reason"] == "tool_use"
        tu = out["content"][0]
        assert tu["type"] == "tool_use"
        assert tu["id"] == "call_1"
        assert tu["name"] == "get_weather"
        assert tu["input"] == {"location": "SF"}

    def test_max_tokens_reason(self):
        openai_resp = {
            "id": "x",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "..."}, "finish_reason": "length"}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        out = openai_to_anthropic_response(openai_resp, "x")
        assert out["stop_reason"] == "max_tokens"


class TestStreamTranslator:

    def test_text_stream(self):
        t = AnthropicStreamTranslator("claude-x")
        events: list[str] = []
        events.extend(t.start())
        events.extend(t.handle_chunk({
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]
        }))
        events.extend(t.handle_chunk({
            "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}]
        }))
        events.extend(t.handle_chunk({
            "choices": [{"index": 0, "delta": {"content": " world"}, "finish_reason": None}]
        }))
        events.extend(t.handle_chunk({
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }))
        events.extend(t.finish())

        joined = "".join(events)
        assert "event: message_start" in joined
        assert "event: content_block_start" in joined
        assert "event: content_block_delta" in joined
        assert "Hello" in joined
        assert "event: content_block_stop" in joined
        assert "event: message_delta" in joined
        assert "event: message_stop" in joined
        assert t.input_tokens == 5
        assert t.output_tokens == 2
        assert t.stop_reason == "end_turn"

        # message_start usage has the full Anthropic shape (zeros upfront
        # because vLLM reports usage only on the final chunk)
        assert '"input_tokens": 0' in joined
        assert '"cache_creation_input_tokens": 0' in joined
        assert '"cache_read_input_tokens": 0' in joined
        # message_delta carries the FINAL counts including input_tokens —
        # Claude Code parses this to drive its context-window indicator
        assert '"input_tokens": 5' in joined
        assert '"output_tokens": 2' in joined

    def test_tool_call_stream(self):
        t = AnthropicStreamTranslator("claude-x")
        events: list[str] = []
        events.extend(t.start())
        events.extend(t.handle_chunk({
            "choices": [{
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {"index": 0, "id": "call_1", "type": "function",
                         "function": {"name": "get_weather", "arguments": ""}}
                    ]
                },
                "finish_reason": None,
            }]
        }))
        events.extend(t.handle_chunk({
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"loc'}}]},
                "finish_reason": None,
            }]
        }))
        events.extend(t.handle_chunk({
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'ation":"SF"}'}}]},
                "finish_reason": None,
            }]
        }))
        events.extend(t.handle_chunk({
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }))
        events.extend(t.finish())

        joined = "".join(events)
        assert "tool_use" in joined
        assert "input_json_delta" in joined
        assert "get_weather" in joined
        assert t.stop_reason == "tool_use"


# ---------------------------------------------------------------------------
# End-to-end HTTP tests via TestClient
# ---------------------------------------------------------------------------

class TestMessagesEndpointNonStream:

    def test_basic_message(self, client, test_user):
        downstream_body = {
            "id": "chatcmpl-abc",
            "object": "chat.completion",
            "model": "real-llm-v1",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hi!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        post_coro, captured = make_post_coro_capture(make_httpx_response(200, downstream_body))
        client.__httpx_mock__.post = post_coro

        resp = client.post(
            "/v1/messages",
            json={
                "model": "test-llm",
                "max_tokens": 50,
                "messages": [{"role": "user", "content": "Hello"}],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "message"
        assert data["role"] == "assistant"
        assert data["content"] == [{"type": "text", "text": "Hi!"}]
        assert data["stop_reason"] == "end_turn"
        assert data["usage"] == {"input_tokens": 10, "output_tokens": 5}
        # downstream model must be swapped to real_model
        assert captured["body"]["model"] == "real-llm-v1"
        assert captured["body"]["max_tokens"] == 50

    def test_x_api_key_header(self, client, test_user):
        """Anthropic-style x-api-key header is accepted."""
        downstream_body = {
            "id": "x",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        client.__httpx_mock__.post = make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/messages",
            json={
                "model": "test-llm",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"x-api-key": "sk-testkey123"},
        )
        assert resp.status_code == 200

    def test_401_without_auth(self, client):
        resp = client.post(
            "/v1/messages",
            json={"model": "test-llm", "max_tokens": 10,
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code in (401, 403)

    def test_system_prompt_forwarded(self, client, test_user):
        downstream_body = {
            "id": "x",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }
        post_coro, captured = make_post_coro_capture(make_httpx_response(200, downstream_body))
        client.__httpx_mock__.post = post_coro

        resp = client.post(
            "/v1/messages",
            json={
                "model": "test-llm",
                "max_tokens": 10,
                "system": "You are a pirate.",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        sent_messages = captured["body"]["messages"]
        assert sent_messages[0] == {"role": "system", "content": "You are a pirate."}
        assert sent_messages[1]["role"] == "user"

    def test_tool_call_response(self, client, test_user):
        downstream_body = {
            "id": "x",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_42",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"location": "Taipei"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42},
        }
        client.__httpx_mock__.post = make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/messages",
            json={
                "model": "test-llm",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "weather in Taipei?"}],
                "tools": [
                    {
                        "name": "get_weather",
                        "description": "Get weather",
                        "input_schema": {
                            "type": "object",
                            "properties": {"location": {"type": "string"}},
                            "required": ["location"],
                        },
                    }
                ],
                "tool_choice": {"type": "auto"},
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["stop_reason"] == "tool_use"
        tu = next(b for b in data["content"] if b["type"] == "tool_use")
        assert tu["name"] == "get_weather"
        assert tu["input"] == {"location": "Taipei"}

    def test_downstream_error_502(self, client, test_user):
        client.__httpx_mock__.post = make_post_coro_raise(Exception("boom"))
        resp = client.post(
            "/v1/messages",
            json={
                "model": "test-llm",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 502

    def test_beta_query_param_passthrough(self, client, test_user):
        """Claude Code posts to ``/v1/messages?beta=true``. FastAPI matches
        the route regardless of undeclared query params, but pin it down so
        we don't accidentally break the route with a conflicting param
        declaration in the future."""
        downstream_body = {
            "id": "x",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        client.__httpx_mock__.post = make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/messages?beta=true",
            json={
                "model": "test-llm",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200

    def test_alias_without_v1_prefix(self, client, test_user):
        """Accept ``/messages`` for clients whose ``ANTHROPIC_BASE_URL`` already
        ends in ``/v1`` — otherwise their requests would 404 silently, which
        looks like a timeout on the client side (the symptom reported for
        the direct connection)."""
        downstream_body = {
            "id": "x",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        client.__httpx_mock__.post = make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/messages",
            json={
                "model": "test-llm",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200


class TestCountTokensEndpoint:

    def test_count_tokens_basic(self, client, test_user):
        downstream_body = {"count": 17, "max_model_len": 32768, "tokens": [1, 2, 3]}
        post_coro, captured = make_post_coro_capture(make_httpx_response(200, downstream_body))
        client.__httpx_mock__.post = post_coro

        resp = client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "test-llm",
                "messages": [{"role": "user", "content": "hello world"}],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert resp.json() == {"input_tokens": 17}
        # Downstream model alias is swapped to real_model
        assert captured["body"]["model"] == "real-llm-v1"
        assert captured["body"]["add_generation_prompt"] is True
        assert captured["body"]["messages"][0]["content"] == "hello world"

    def test_count_tokens_with_system_and_tools(self, client, test_user):
        downstream_body = {"count": 42}
        post_coro, captured = make_post_coro_capture(make_httpx_response(200, downstream_body))
        client.__httpx_mock__.post = post_coro

        resp = client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "test-llm",
                "system": "You are a helpful assistant.",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [
                    {
                        "name": "get_weather",
                        "description": "Get weather",
                        "input_schema": {
                            "type": "object",
                            "properties": {"location": {"type": "string"}},
                        },
                    }
                ],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert resp.json() == {"input_tokens": 42}
        # System prompt + tools must be included in the tokenize payload
        assert captured["body"]["messages"][0]["role"] == "system"
        assert captured["body"]["tools"][0]["function"]["name"] == "get_weather"

    def test_count_tokens_x_api_key(self, client, test_user):
        client.__httpx_mock__.post = make_post_coro(
            make_httpx_response(200, {"count": 5})
        )
        resp = client.post(
            "/v1/messages/count_tokens",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
            headers={"x-api-key": "sk-testkey123"},
        )
        assert resp.status_code == 200
        assert resp.json()["input_tokens"] == 5

    def test_count_tokens_401_without_auth(self, client):
        resp = client.post(
            "/v1/messages/count_tokens",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code in (401, 403)

    def test_count_tokens_fallback_on_downstream_error(self, client, test_user):
        """When /tokenize is unavailable, return a rough estimate instead of 5xx."""
        client.__httpx_mock__.post = make_post_coro_raise(Exception("connection refused"))
        resp = client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "test-llm",
                "messages": [{"role": "user", "content": "hello world this is a test"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        # Estimate is chars/4, so should be > 0
        assert resp.json()["input_tokens"] > 0

    def test_count_tokens_alias_without_v1_prefix(self, client, test_user):
        """``/messages/count_tokens`` mirrors ``/v1/messages/count_tokens``
        for clients whose base URL already contains ``/v1``."""
        client.__httpx_mock__.post = make_post_coro(
            make_httpx_response(200, {"count": 7})
        )
        resp = client.post(
            "/messages/count_tokens",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert resp.json()["input_tokens"] == 7

    def test_count_tokens_fallback_on_404(self, client, test_user):
        """Some downstreams may not implement /tokenize → fall back to estimate."""
        client.__httpx_mock__.post = make_post_coro(
            make_httpx_response(404, {"detail": "Not Found"})
        )
        resp = client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "test-llm",
                "messages": [{"role": "user", "content": "abcdefgh"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert resp.json()["input_tokens"] >= 1


class TestMessagesEndpointStream:

    def test_stream_basic(self, client, test_user):
        sse_lines = [
            'data: {"id":"c1","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
            'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}',
            'data: {"id":"c1","choices":[{"index":0,"delta":{"content":" world"},"finish_reason":null}]}',
            'data: {"id":"c1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":4,"completion_tokens":2}}',
            "data: [DONE]",
        ]
        fake = FakeStreamResponse(sse_lines)
        mock = client.__httpx_mock__
        mock.build_request.return_value = httpx.Request("POST", "http://mock-llm:8000/v1/chat/completions")
        mock.send.return_value = fake

        resp = client.post(
            "/v1/messages",
            json={
                "model": "test-llm",
                "max_tokens": 100,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        body = resp.text
        # Expect the standard Anthropic SSE event sequence
        assert "event: message_start" in body
        assert "event: content_block_start" in body
        assert "event: content_block_delta" in body
        assert "Hello" in body
        assert "event: content_block_stop" in body
        assert "event: message_delta" in body
        assert "event: message_stop" in body
        # message_delta usage must include input_tokens in addition to
        # output_tokens — Claude Code reads this to update its context bar.
        # Without input_tokens, the client's context indicator resets to 0.
        assert '"input_tokens": 4' in body
        assert '"output_tokens": 2' in body
