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

from app.services.anthropic_adapter import (
    normalize_anthropic_messages,
    sanitize_native_messages_body,
)
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


class TestNormalizeAnthropicMessages:
    """Pure-function tests for the native path's system-role normalization."""

    def test_clean_request_returned_unchanged_same_object(self):
        body = {
            "model": "x",
            "system": "You are Claude Code.",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ],
        }
        assert normalize_anthropic_messages(body) is body

    def test_leading_system_entry_hoisted_to_system_field(self):
        body = {
            "model": "x",
            "system": "Base instructions.",
            "messages": [
                {"role": "system", "content": "Extra leading."},
                {"role": "user", "content": "hi"},
            ],
        }
        out = normalize_anthropic_messages(body)
        roles = [m["role"] for m in out["messages"]]
        assert roles == ["user"]
        system_texts = [b["text"] for b in out["system"]]
        assert system_texts == ["Base instructions.", "Extra leading."]
        # Input body not mutated.
        assert len(body["messages"]) == 2
        assert body["system"] == "Base instructions."

    def test_system_block_list_with_cache_control_preserved(self):
        cc_block = {
            "type": "text",
            "text": "Base.",
            "cache_control": {"type": "ephemeral"},
        }
        body = {
            "model": "x",
            "system": [cc_block],
            "messages": [
                {"role": "system", "content": "Extra."},
                {"role": "user", "content": "hi"},
            ],
        }
        out = normalize_anthropic_messages(body)
        assert out["system"][0] is cc_block  # original block kept verbatim
        assert out["system"][1]["text"] == "Extra."

    def test_mid_conversation_system_merged_into_previous_user(self):
        body = {
            "model": "x",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "system", "content": "IDE opened file foo.py"},
                {"role": "assistant", "content": "hello"},
            ],
        }
        out = normalize_anthropic_messages(body)
        roles = [m["role"] for m in out["messages"]]
        assert roles == ["user", "assistant"]
        user_content = out["messages"][0]["content"]
        assert isinstance(user_content, list)
        assert user_content[0] == {"type": "text", "text": "hi"}
        assert "<system-reminder>" in user_content[1]["text"]
        assert "IDE opened file foo.py" in user_content[1]["text"]

    def test_system_after_assistant_prepended_to_next_user(self):
        body = {
            "model": "x",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "system", "content": "Reminder text"},
                {"role": "user", "content": "next question"},
            ],
        }
        out = normalize_anthropic_messages(body)
        roles = [m["role"] for m in out["messages"]]
        assert roles == ["user", "assistant", "user"]
        next_user = out["messages"][2]["content"]
        assert "<system-reminder>" in next_user[0]["text"]
        assert next_user[1] == {"type": "text", "text": "next question"}

    def test_trailing_system_becomes_own_user_message(self):
        body = {
            "model": "x",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "system", "content": "Trailing reminder"},
            ],
        }
        out = normalize_anthropic_messages(body)
        assert out["messages"][-1]["role"] == "user"
        assert "<system-reminder>" in out["messages"][-1]["content"][0]["text"]

    def test_normalization_applied_on_native_path(self, client, test_user, native_llm):
        downstream = make_httpx_response(200, {
            "id": "msg_1", "type": "message", "role": "assistant",
            "model": "real-llm-v1",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 1},
        })
        post, captured = make_post_router({"/messages": downstream})
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/messages",
            json={
                "model": "test-llm",
                "max_tokens": 100,
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "system", "content": "IDE event"},
                    {"role": "assistant", "content": "hello"},
                    {"role": "user", "content": "next"},
                ],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        sent = captured["json"]["messages"]
        assert [m["role"] for m in sent] == ["user", "assistant", "user"]
        assert "<system-reminder>" in json.dumps(sent[0]["content"])


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


class TestSanitizeNativeBody:
    """sanitize_native_messages_body: Claude Code beta fields (auto mode's
    classifier requests carry output_config / context_management, adaptive
    thinking, tool strict/defer_loading) are stripped before hitting stock
    vLLM's strict schema instead of 400ing the whole request."""

    def test_clean_body_returned_same_object(self):
        body = {
            "model": "test-llm",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "enabled", "budget_tokens": 2048},
            "tools": [{"name": "t", "description": "d", "input_schema": {}}],
        }
        out, dropped = sanitize_native_messages_body(body)
        assert out is body
        assert dropped == []

    def test_beta_top_level_fields_dropped(self):
        body = {
            "model": "test-llm",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
            "context_management": {"edits": []},
            "output_config": {"format": {"type": "json_schema"}},
            "betas": ["context-management-2025-06-27"],
        }
        out, dropped = sanitize_native_messages_body(body)
        assert "context_management" not in out
        assert "output_config" not in out
        assert "betas" not in out
        assert set(dropped) == {"context_management", "output_config", "betas"}
        # Known fields survive untouched.
        assert out["max_tokens"] == 100
        assert out["messages"] == body["messages"]

    def test_adaptive_thinking_dropped_enabled_kept(self):
        adaptive = {
            "model": "m", "messages": [],
            "thinking": {"type": "adaptive"},
        }
        out, dropped = sanitize_native_messages_body(adaptive)
        assert "thinking" not in out
        assert dropped == ["thinking(type='adaptive')"]

        enabled = {
            "model": "m", "messages": [],
            "thinking": {"type": "disabled"},
        }
        out, dropped = sanitize_native_messages_body(enabled)
        assert out is enabled
        assert dropped == []

    def test_beta_tool_fields_stripped(self):
        body = {
            "model": "m", "messages": [],
            "tools": [
                {"name": "t1", "description": "d", "input_schema": {},
                 "strict": True, "defer_loading": True},
                {"name": "t2", "input_schema": {}},
            ],
        }
        out, dropped = sanitize_native_messages_body(body)
        assert out["tools"][0] == {"name": "t1", "description": "d", "input_schema": {}}
        assert out["tools"][1] == {"name": "t2", "input_schema": {}}
        assert sorted(dropped) == ["tools[].defer_loading", "tools[].strict"]

    def test_native_path_forwards_sanitized_body(self, client, test_user, native_llm):
        """End-to-end: beta fields never reach the downstream."""
        downstream = make_httpx_response(200, {
            "id": "msg_1", "type": "message", "role": "assistant",
            "model": "real-llm-v1",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 1},
        })
        post, captured = make_post_router({"/messages": downstream})
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/messages",
            json={
                "model": "test-llm",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
                "thinking": {"type": "adaptive"},
                "context_management": {"edits": []},
                "output_config": {"format": {"type": "json_schema"}},
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        sent = captured["json"]
        assert "context_management" not in sent
        assert "output_config" not in sent
        assert "thinking" not in sent
        assert sent["model"] == "real-llm-v1"
