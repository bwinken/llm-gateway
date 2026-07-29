"""
Tests for the `native_messages` route flag: Anthropic requests are forwarded
as-is to a downstream vLLM's native /v1/messages endpoint (vLLM >= 0.11.1)
instead of being translated through the OpenAI pivot. A downstream that
answers 404/405 (older vLLM) falls back to the translation path.
"""

from __future__ import annotations

import json

import httpx
import pytest

from tests.conftest import (
    FakeStreamResponse,
    TEST_MODEL_ROUTING,
    auth_header,
    make_httpx_response,
)


@pytest.fixture
def native_llm():
    """Flip test-llm to native_messages for the duration of one test.

    Mutates the canonical TEST_MODEL_ROUTING dict in place (the autouse
    patch points at the same object) and restores it afterwards.
    """
    TEST_MODEL_ROUTING["test-llm"]["native_messages"] = True
    yield
    TEST_MODEL_ROUTING["test-llm"].pop("native_messages", None)


def make_post_router(responses_by_suffix: dict):
    """Route mock POSTs by URL suffix; records every URL and the last body."""
    captured: dict = {"urls": []}

    async def _post(url, *args, **kwargs):
        captured["urls"].append(str(url))
        captured["json"] = kwargs.get("json")
        for suffix, resp in responses_by_suffix.items():
            if str(url).endswith(suffix):
                return resp
        raise AssertionError(f"unexpected downstream url: {url}")

    return _post, captured


NATIVE_SSE_LINES = [
    "event: message_start",
    'data: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant","model":"real-llm-v1","content":[],"usage":{"input_tokens":7,"cache_read_input_tokens":2}}}',
    "",
    "event: content_block_start",
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
    "",
    "event: content_block_delta",
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi there"}}',
    "",
    "event: content_block_stop",
    'data: {"type":"content_block_stop","index":0}',
    "",
    "event: message_delta",
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":3}}',
    "",
    "event: message_stop",
    'data: {"type":"message_stop"}',
    "",
]


class TestNativeNonStream:

    def test_passthrough_hits_native_endpoint(self, client, test_user, native_llm):
        downstream = make_httpx_response(200, {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "real-llm-v1",
            "content": [{"type": "text", "text": "Hello"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3},
        })
        post, captured = make_post_router({"/messages": downstream})
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/messages",
            json={
                "model": "test-llm",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        # Real model name hidden behind the alias; content passed through.
        assert data["model"] == "test-llm"
        assert data["content"][0]["text"] == "Hello"
        # Went to the native endpoint, with the alias swapped to real_model.
        assert captured["urls"] == ["http://mock-llm:8000/v1/messages"]
        assert captured["json"]["model"] == "real-llm-v1"
        # Anthropic body forwarded untranslated (still Anthropic shape).
        assert captured["json"]["messages"] == [{"role": "user", "content": "hi"}]

    def test_404_falls_back_to_translation(self, client, test_user, native_llm):
        native_404 = make_httpx_response(404, {"detail": "Not Found"})
        openai_resp = make_httpx_response(200, {
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hello via translation"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        })
        post, captured = make_post_router({
            "/messages": native_404,
            "/chat/completions": openai_resp,
        })
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/messages",
            json={
                "model": "test-llm",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["content"][0]["text"] == "Hello via translation"
        # Tried native first, then fell back to the translated path.
        assert captured["urls"] == [
            "http://mock-llm:8000/v1/messages",
            "http://mock-llm:8000/v1/chat/completions",
        ]

    def test_downstream_error_propagates(self, client, test_user, native_llm):
        post, _ = make_post_router({
            "/messages": make_httpx_response(500, {"error": "boom"}),
        })
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/messages",
            json={
                "model": "test-llm",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 500


class TestNativeStream:

    def _post_stream(self, client):
        return client.post(
            "/v1/messages",
            json={
                "model": "test-llm",
                "max_tokens": 100,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )

    def test_stream_passthrough(self, client, test_user, native_llm):
        mock = client.__httpx_mock__
        mock.build_request.return_value = httpx.Request("POST", "http://mock-llm:8000/v1/messages")
        mock.send.return_value = FakeStreamResponse(NATIVE_SSE_LINES)

        resp = self._post_stream(client)

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        body = resp.text
        assert "event: message_start" in body
        assert "Hi there" in body
        assert "event: message_stop" in body
        assert "event: error" not in body
        # message_start's model field rewritten to the public alias.
        assert '"model": "test-llm"' in body
        assert "real-llm-v1" not in body

    def test_stream_truncation_emits_overloaded_error(self, client, test_user, native_llm):
        # Stream dies after the first text delta — no message_delta/stop.
        truncated = NATIVE_SSE_LINES[:9]
        mock = client.__httpx_mock__
        mock.build_request.return_value = httpx.Request("POST", "http://mock-llm:8000/v1/messages")
        mock.send.return_value = FakeStreamResponse(truncated)

        resp = self._post_stream(client)

        assert resp.status_code == 200
        body = resp.text
        assert "event: error" in body
        assert "overloaded_error" in body

    def test_stream_404_falls_back_to_translation(self, client, test_user, native_llm):
        openai_lines = [
            'data: {"id":"c1","choices":[{"index":0,"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}',
            'data: {"id":"c1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":4,"completion_tokens":1}}',
            "data: [DONE]",
        ]
        calls = {"n": 0}

        async def send(req, stream=False):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeStreamResponse([], status_code=404, body_bytes=b'{"detail":"Not Found"}')
            return FakeStreamResponse(openai_lines)

        mock = client.__httpx_mock__
        mock.build_request.return_value = httpx.Request("POST", "http://mock-llm:8000/v1/messages")
        # side_effect (not attribute replacement) — the mock client is a
        # module-level singleton shared across tests and reset_mock() clears
        # neither replaced attributes nor side_effect, so restore it after.
        mock.send.side_effect = send
        try:
            resp = self._post_stream(client)
        finally:
            mock.send.side_effect = None

        assert resp.status_code == 200
        body = resp.text
        assert calls["n"] == 2  # native preflight, then translated stream
        assert "event: message_start" in body
        assert "Hello" in body
        assert "event: message_stop" in body


class TestNativeCountTokens:

    def test_native_count_tokens(self, client, test_user, native_llm):
        post, captured = make_post_router({
            "/messages/count_tokens": make_httpx_response(200, {"input_tokens": 42}),
        })
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "test-llm",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert resp.json() == {"input_tokens": 42}
        assert captured["urls"] == ["http://mock-llm:8000/v1/messages/count_tokens"]
        assert captured["json"]["model"] == "real-llm-v1"

    def test_native_count_tokens_falls_back_to_tokenize(self, client, test_user, native_llm):
        post, captured = make_post_router({
            "/messages/count_tokens": make_httpx_response(404, {"detail": "Not Found"}),
            "/tokenize": make_httpx_response(200, {"count": 9}),
        })
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "test-llm",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert resp.json() == {"input_tokens": 9}
        assert captured["urls"][0].endswith("/messages/count_tokens")
        assert captured["urls"][1].endswith("/tokenize")


class TestFlagOff:

    def test_without_flag_uses_translation(self, client, test_user):
        openai_resp = make_httpx_response(200, {
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hi"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        })
        post, captured = make_post_router({"/chat/completions": openai_resp})
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/messages",
            json={
                "model": "test-llm",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert captured["urls"] == ["http://mock-llm:8000/v1/chat/completions"]
