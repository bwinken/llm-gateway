"""
Tests for POST /v1/chat/completions

Covers: non-streaming, streaming, tool calls, structured output, auth, model fallback.
"""

from __future__ import annotations

import json

import httpx
import pytest

from tests.conftest import FakeStreamResponse, auth_header, make_httpx_response


# ---------------------------------------------------------------------------
# Non-streaming
# ---------------------------------------------------------------------------

class TestChatCompletionsNonStream:

    def test_basic_completion(self, client, test_user):
        downstream_body = {
            "id": "chatcmpl-abc",
            "object": "chat.completion",
            "model": "real-llm-v1",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        mock = client.__httpx_mock__
        mock.post = make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "Hi"}]},
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["choices"][0]["message"]["content"] == "Hello!"
        assert data["usage"]["prompt_tokens"] == 10

    def test_401_without_auth(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "Hi"}]},
        )
        assert resp.status_code in (401, 403)

    def test_model_fallback(self, client, test_user):
        """Requesting a non-existent model should fall back to first matching type."""
        downstream_body = {
            "id": "chatcmpl-fb",
            "object": "chat.completion",
            "model": "real-llm-v1",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "fallback"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }
        mock = client.__httpx_mock__
        mock.post = make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/chat/completions",
            json={"model": "nonexistent-model", "messages": [{"role": "user", "content": "test"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200

    def test_downstream_error_returns_502(self, client, test_user):
        mock = client.__httpx_mock__
        mock.post = make_post_coro_raise(Exception("connection refused"))

        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "Hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 502

    def test_downstream_non_200(self, client, test_user):
        mock = client.__httpx_mock__
        mock.post = make_post_coro(
            make_httpx_response(429, {"error": {"message": "rate limited"}})
        )

        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "Hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------

class TestChatCompletionsToolCall:

    def test_tool_call_response(self, client, test_user):
        downstream_body = {
            "id": "chatcmpl-tool",
            "object": "chat.completion",
            "model": "real-llm-v1",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc123",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"location": "San Francisco"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
        }
        mock = client.__httpx_mock__
        mock.post = make_post_coro(make_httpx_response(200, downstream_body))

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get the current weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"location": {"type": "string"}},
                        "required": ["location"],
                    },
                },
            }
        ]

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-llm",
                "messages": [{"role": "user", "content": "What is the weather in SF?"}],
                "tools": tools,
                "tool_choice": "auto",
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["choices"][0]["finish_reason"] == "tool_calls"
        tc = data["choices"][0]["message"]["tool_calls"][0]
        assert tc["function"]["name"] == "get_weather"
        args = json.loads(tc["function"]["arguments"])
        assert args["location"] == "San Francisco"

    def test_tool_call_multi_turn(self, client, test_user):
        """Simulate sending tool result back to the model."""
        downstream_body = {
            "id": "chatcmpl-tool2",
            "object": "chat.completion",
            "model": "real-llm-v1",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "The weather in SF is 72F and sunny."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 80, "completion_tokens": 15, "total_tokens": 95},
        }
        mock = client.__httpx_mock__
        mock.post = make_post_coro(make_httpx_response(200, downstream_body))

        messages = [
            {"role": "user", "content": "What is the weather in SF?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc123",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"location": "San Francisco"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_abc123",
                "content": '{"temperature": 72, "condition": "sunny"}',
            },
        ]

        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test-llm", "messages": messages},
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["choices"][0]["finish_reason"] == "stop"
        assert "72" in data["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Structured output (response_format)
# ---------------------------------------------------------------------------

class TestChatCompletionsStructuredOutput:

    def test_json_mode(self, client, test_user):
        downstream_body = {
            "id": "chatcmpl-json",
            "object": "chat.completion",
            "model": "real-llm-v1",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": '{"name": "Alice", "age": 30}',
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        }
        mock = client.__httpx_mock__
        mock.post = make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-llm",
                "messages": [{"role": "user", "content": "Return a JSON object with name and age."}],
                "response_format": {"type": "json_object"},
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        content = json.loads(data["choices"][0]["message"]["content"])
        assert content["name"] == "Alice"
        assert content["age"] == 30

    def test_json_schema(self, client, test_user):
        downstream_body = {
            "id": "chatcmpl-schema",
            "object": "chat.completion",
            "model": "real-llm-v1",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": '{"steps": [{"explanation": "Add", "output": "3"}], "final_answer": "3"}',
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50},
        }
        mock = client.__httpx_mock__
        mock.post = make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-llm",
                "messages": [{"role": "user", "content": "Solve 1+2"}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "math_response",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "steps": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "explanation": {"type": "string"},
                                            "output": {"type": "string"},
                                        },
                                    },
                                },
                                "final_answer": {"type": "string"},
                            },
                            "required": ["steps", "final_answer"],
                        },
                    },
                },
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        content = json.loads(data["choices"][0]["message"]["content"])
        assert content["final_answer"] == "3"
        assert len(content["steps"]) == 1


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

class TestChatCompletionsStream:

    def test_stream_basic(self, client, test_user):
        sse_lines = [
            'data: {"id":"chatcmpl-s1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-s1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-s1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"!"},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-s1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}',
            "data: [DONE]",
        ]
        fake_stream = FakeStreamResponse(sse_lines)
        mock = client.__httpx_mock__
        mock.build_request.return_value = httpx.Request("POST", "http://mock-llm:8000/v1/chat/completions")
        mock.send.return_value = fake_stream

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-llm",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        body = resp.text
        assert "Hello" in body
        assert "data: [DONE]" in body


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_post_coro(response):
    """Return an async function that returns the given response."""
    async def _post(*args, **kwargs):
        return response
    return _post


def make_post_coro_raise(exc):
    """Return an async function that raises the given exception."""
    async def _post(*args, **kwargs):
        raise exc
    return _post
