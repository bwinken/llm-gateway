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
    anthropic_request_io,
    anthropic_to_openai_request,
    empty_turn_warning,
    openai_message_to_anthropic,
    openai_to_anthropic_response,
    summarize_request_shape,
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

    def test_leading_system_messages_hoisted_to_front(self):
        """System / developer entries BEFORE the first real turn join the
        top-level `system` field as the single leading system message — strict
        downstream chat templates (Qwen3.x) raise
        "System message must be at the beginning" for anything else.
        """
        body = {
            "model": "x",
            "system": "You are Claude Code.",
            "messages": [
                {"role": "system", "content": "Extra leading instructions."},
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ],
        }
        out = anthropic_to_openai_request(body)
        system_indices = [
            i for i, m in enumerate(out["messages"]) if m["role"] == "system"
        ]
        assert system_indices == [0]
        assert "You are Claude Code." in out["messages"][0]["content"]
        assert "Extra leading instructions." in out["messages"][0]["content"]
        assert [m["role"] for m in out["messages"][1:]] == ["user", "assistant"]

    def test_mid_conversation_system_merged_into_previous_user(self):
        """A `role: "system"` reminder between a user and an assistant turn
        (e.g. Claude Code's IDE open-file event) is merged into the preceding
        user message as <system-reminder> text — never emitted as a
        mid-conversation system message, and never hoisted to the front (which
        would lose its temporal position and invalidate the downstream's
        prefix cache every turn).
        """
        body = {
            "model": "x",
            "system": "You are Claude Code.",
            "messages": [
                {"role": "user", "content": "A"},
                {
                    "role": "system",
                    "content": "The user opened the file /home/u/JAM.v in the IDE.",
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "call_1", "name": "Skill", "input": {}},
                    ],
                },
            ],
        }
        out = anthropic_to_openai_request(body)
        roles = [m["role"] for m in out["messages"]]
        assert roles == ["system", "user", "assistant"]
        # Leading system prompt untouched by the reminder.
        assert out["messages"][0]["content"] == "You are Claude Code."
        # Reminder appended after the original user text, wrapped in tags.
        user_content = out["messages"][1]["content"]
        assert user_content.startswith("A")
        assert "<system-reminder>" in user_content
        assert "JAM.v" in user_content
        assert user_content.index("A") < user_content.index("JAM.v")

    def test_mid_conversation_system_prepended_to_next_user(self):
        """A reminder arriving after an assistant turn attaches to the FRONT of
        the next user message, preserving its original position."""
        body = {
            "model": "x",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "system", "content": "todo list updated"},
                {"role": "user", "content": "continue"},
            ],
        }
        out = anthropic_to_openai_request(body)
        assert [m["role"] for m in out["messages"]] == ["user", "assistant", "user"]
        last_user = out["messages"][2]["content"]
        assert last_user.startswith("<system-reminder>")
        assert "todo list updated" in last_user
        assert last_user.endswith("continue")

    def test_multiple_consecutive_reminders_kept_in_order(self):
        body = {
            "model": "x",
            "messages": [
                {"role": "user", "content": "A"},
                {"role": "system", "content": "first reminder"},
                {"role": "system", "content": "second reminder"},
                {"role": "assistant", "content": "ok"},
            ],
        }
        out = anthropic_to_openai_request(body)
        assert [m["role"] for m in out["messages"]] == ["user", "assistant"]
        user_content = out["messages"][0]["content"]
        assert user_content.index("A") < user_content.index("first reminder")
        assert user_content.index("first reminder") < user_content.index("second reminder")

    def test_reminder_already_tagged_not_double_wrapped(self):
        body = {
            "model": "x",
            "messages": [
                {"role": "user", "content": "A"},
                {
                    "role": "system",
                    "content": "<system-reminder>stay on task</system-reminder>",
                },
                {"role": "assistant", "content": "ok"},
            ],
        }
        out = anthropic_to_openai_request(body)
        user_content = out["messages"][0]["content"]
        assert user_content.count("<system-reminder>") == 1

    def test_reminder_after_tool_result_attaches_to_next_user(self):
        """A reminder following a tool_result-only user turn (which emits only
        `role: "tool"` messages) is buffered and prepended to the next real
        user message instead of becoming a mid-conversation system message."""
        body = {
            "model": "x",
            "messages": [
                {"role": "user", "content": "run it"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "call_1", "name": "bash", "input": {}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "call_1", "content": "done"},
                    ],
                },
                {"role": "system", "content": "The user opened JAM.v in the IDE."},
                {"role": "user", "content": "what next?"},
            ],
        }
        out = anthropic_to_openai_request(body)
        roles = [m["role"] for m in out["messages"]]
        assert "system" not in roles
        assert roles == ["user", "assistant", "tool", "user"]
        # Tool output untouched by the reminder.
        assert out["messages"][2]["content"] == "done"
        last_user = out["messages"][3]["content"]
        assert last_user.startswith("<system-reminder>")
        assert "JAM.v" in last_user
        assert last_user.endswith("what next?")

    def test_developer_message_merged_like_system(self):
        """Mid-conversation `role: "developer"` messages get the same in-place
        merge treatment as system reminders."""
        body = {
            "model": "x",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "developer", "content": "be concise"},
            ],
        }
        out = anthropic_to_openai_request(body)
        assert [m["role"] for m in out["messages"]] == ["user"]
        user_content = out["messages"][0]["content"]
        assert user_content.startswith("hi")
        assert "be concise" in user_content
        assert "<system-reminder>" in user_content

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
        # Pure tool_use assistant messages must use empty-string content,
        # not null. Some vLLM tool parsers (hermes / mistral / llama3) reject
        # `content: null` with a 400, which appears to Claude Code clients
        # as a hung/empty response.
        assert assistant_msg["content"] == ""
        tool_msg = out["messages"][2]
        assert tool_msg["tool_call_id"] == "toolu_01"
        assert tool_msg["content"] == "72F sunny"

    def test_thinking_block_preserved_as_reasoning_content(self):
        """An assistant turn's `thinking` block round-trips back as
        `reasoning_content` on the OpenAI message so reasoning-aware chat
        templates (e.g. Qwen3 preserve_thinking) can re-inject it."""
        body = {
            "model": "x",
            "messages": [
                {"role": "user", "content": "solve it"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "first I consider...", "signature": ""},
                        {"type": "text", "text": "The answer is 42."},
                    ],
                },
                {"role": "user", "content": "explain more"},
            ],
        }
        out = anthropic_to_openai_request(body)
        assistant_msg = out["messages"][1]
        assert assistant_msg["role"] == "assistant"
        assert assistant_msg["content"] == "The answer is 42."
        assert assistant_msg["reasoning_content"] == "first I consider..."

    def test_effort_string_mapped(self):
        for eff, expected in [
            ("low", "low"), ("medium", "medium"), ("high", "high"),
            ("max", "high"), ("extra-high", "high"), ("minimal", "low"),
        ]:
            body = {
                "model": "x",
                "messages": [{"role": "user", "content": "hi"}],
                "effort": eff,
            }
            out = anthropic_to_openai_request(body, is_reasoning=True)
            assert out["reasoning_effort"] == expected, f"effort={eff}"

    def test_thinking_budget_bucketed_to_effort(self):
        for budget, expected in [(1024, "low"), (4096, "low"),
                                 (8192, "medium"), (16384, "medium"),
                                 (32000, "high")]:
            body = {
                "model": "x",
                "messages": [{"role": "user", "content": "hi"}],
                "thinking": {"type": "enabled", "budget_tokens": budget},
            }
            out = anthropic_to_openai_request(body, is_reasoning=True)
            assert out["reasoning_effort"] == expected, f"budget={budget}"

    def test_no_effort_when_unset(self):
        body = {"model": "x", "messages": [{"role": "user", "content": "hi"}]}
        out = anthropic_to_openai_request(body, is_reasoning=True)
        assert "reasoning_effort" not in out

    def test_effort_not_emitted_for_non_reasoning_model(self):
        """A non-reasoning model (is_reasoning=False, the default) never gets
        reasoning_effort even when the client sends effort/thinking — avoids
        400s from downstreams that reject the parameter."""
        body = {
            "model": "x",
            "messages": [{"role": "user", "content": "hi"}],
            "effort": "high",
            "thinking": {"type": "enabled", "budget_tokens": 32000},
        }
        out = anthropic_to_openai_request(body)  # is_reasoning defaults to False
        assert "reasoning_effort" not in out

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

    def test_reasoning_content_response(self):
        """vLLM/DeepSeek-style `reasoning_content` becomes an Anthropic
        `thinking` content block that precedes the answer's text block."""
        openai_resp = {
            "id": "chatcmpl-3",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "Let me think step by step about this...",
                        "content": "The answer is 42.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 20},
        }
        out = openai_to_anthropic_response(openai_resp, "qwen3-thinking")
        # thinking block first, text block second
        assert out["content"][0]["type"] == "thinking"
        assert out["content"][0]["thinking"] == "Let me think step by step about this..."
        assert out["content"][1] == {"type": "text", "text": "The answer is 42."}

    def test_reasoning_alias_response(self):
        """Some vLLM builds emit the field as `reasoning` instead of
        `reasoning_content`; we accept either."""
        openai_resp = {
            "id": "chatcmpl-4",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "reasoning": "Here's a thinking process...",
                        "content": "9.9 is larger.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 20},
        }
        out = openai_to_anthropic_response(openai_resp, "radllm-code")
        assert out["content"][0]["type"] == "thinking"
        assert out["content"][0]["thinking"] == "Here's a thinking process..."
        assert out["content"][1] == {"type": "text", "text": "9.9 is larger."}

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

    def test_reasoning_then_text_stream(self):
        """Reasoning chunks open a thinking block; transitioning to content
        chunks closes the thinking block and opens a separate text block.
        Indices are sequential. No signature_delta is emitted — see
        _close_current_block for rationale (LiteLLM does the same)."""
        t = AnthropicStreamTranslator("qwen3-thinking")
        events: list[str] = []
        events.extend(t.start())
        # Two reasoning chunks — single thinking block
        events.extend(t.handle_chunk({
            "choices": [{"index": 0, "delta": {"reasoning_content": "Let me think..."}, "finish_reason": None}]
        }))
        events.extend(t.handle_chunk({
            "choices": [{"index": 0, "delta": {"reasoning_content": " step by step"}, "finish_reason": None}]
        }))
        # Then content — should close thinking, open text
        events.extend(t.handle_chunk({
            "choices": [{"index": 0, "delta": {"content": "The answer is 42."}, "finish_reason": None}]
        }))
        events.extend(t.handle_chunk({
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 20},
        }))
        events.extend(t.finish())

        joined = "".join(events)
        # Both block types appear
        assert '"type": "thinking"' in joined
        assert '"type": "text"' in joined
        # thinking_delta carries the reasoning text
        assert '"type": "thinking_delta"' in joined
        assert "Let me think..." in joined
        assert " step by step" in joined
        # text_delta carries the answer
        assert '"type": "text_delta"' in joined
        assert "The answer is 42." in joined
        # We do NOT emit signature_delta (vLLM has no signature to forward,
        # and strict Claude Code builds reject empty values).
        assert '"type": "signature_delta"' not in joined
        # Indices: thinking at 0, text at 1
        assert '"index": 0' in joined
        assert '"index": 1' in joined

    def test_reasoning_alias_stream(self):
        """Stream path also accepts `reasoning` as an alias for
        `reasoning_content` so older / alternate vLLM builds work."""
        t = AnthropicStreamTranslator("radllm-code")
        events: list[str] = []
        events.extend(t.start())
        events.extend(t.handle_chunk({
            "choices": [{"index": 0, "delta": {"reasoning": "Thinking..."}, "finish_reason": None}]
        }))
        events.extend(t.handle_chunk({
            "choices": [{"index": 0, "delta": {"content": "Answer."}, "finish_reason": None}]
        }))
        events.extend(t.handle_chunk({
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }))
        events.extend(t.finish())

        joined = "".join(events)
        assert '"type": "thinking"' in joined
        assert '"type": "thinking_delta"' in joined
        assert "Thinking..." in joined
        assert '"type": "text_delta"' in joined
        assert "Answer." in joined

    def test_fail_emits_error_and_closes_block(self):
        """When the downstream drops mid-stream (no finish_reason), the proxy
        calls translator.fail() — it must close the open content block and
        emit an `error` event, NOT a normal message_stop."""
        t = AnthropicStreamTranslator("claude-x")
        events: list[str] = []
        events.extend(t.start())
        events.extend(t.handle_chunk({
            "choices": [{"index": 0, "delta": {"content": "partial ans"}, "finish_reason": None}]
        }))
        # downstream dropped — no finish_reason ever arrived
        assert t.stop_reason is None
        events.extend(t.fail("Downstream stream ended prematurely."))

        joined = "".join(events)
        # open text block is closed
        assert "event: content_block_stop" in joined
        # an error event is emitted — overloaded_error so Claude Code retries
        assert "event: error" in joined
        assert "overloaded_error" in joined
        # and crucially NOT a normal completion
        assert "event: message_stop" not in joined

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

    def test_valid_x_api_key_wins_over_invalid_bearer(self, client, test_user):
        """Claude Code may send both ``Authorization: Bearer <something>``
        (e.g. from ANTHROPIC_AUTH_TOKEN or a corporate proxy) and a valid
        ``x-api-key``. We should let the request through as long as *any*
        of the supplied credentials is valid — otherwise users get a
        confusing 401 even though they configured the gateway key
        correctly."""
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
            headers={
                "Authorization": "Bearer sk-ant-not-our-key",
                "x-api-key": "sk-testkey123",
            },
        )
        assert resp.status_code == 200

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

    def test_stream_out1_logs_empty_turn_warning(self, client, test_user):
        """A clean finish (finish_reason=stop) that produced only 1 output
        token and no visible content logs the empty-turn diagnostic."""
        from loguru import logger

        sse_lines = [
            'data: {"id":"c1","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
            'data: {"id":"c1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1200,"completion_tokens":1}}',
            "data: [DONE]",
        ]
        fake = FakeStreamResponse(sse_lines)
        mock = client.__httpx_mock__
        mock.build_request.return_value = httpx.Request("POST", "http://mock-llm:8000/v1/chat/completions")
        mock.send.return_value = fake

        captured: list[str] = []
        sink_id = logger.add(captured.append, level="WARNING")
        try:
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
            _ = resp.text  # drain the stream so the generator finishes + logs
        finally:
            logger.remove(sink_id)

        warnings = "".join(captured)
        assert "Empty/near-empty turn" in warnings
        assert "out=1" in warnings
        assert "text_chars=0" in warnings


# ---------------------------------------------------------------------------
# Empty / near-empty turn diagnostics
#
# A clean finish (finish_reason present) with output_tokens <= 1 is the
# "silent stop" Claude Code shows: a successful-but-empty turn. We instrument
# it so the frequency and request shape are visible in the logs without
# enabling full per-user monitoring.
# ---------------------------------------------------------------------------

class TestEmptyTurnDiagnostics:

    def test_translator_tracks_text_and_thinking_chars(self):
        """The translator counts visible text vs reasoning chars so the
        diagnostic can tell a truly-empty turn from a thinking-only one."""
        t = AnthropicStreamTranslator("qwen3-thinking")
        list(t.start())
        list(t.handle_chunk({
            "choices": [{"index": 0, "delta": {"reasoning_content": "abc"}, "finish_reason": None}]
        }))
        list(t.handle_chunk({
            "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}]
        }))
        assert t.thinking_chars == 3
        assert t.text_chars == 5

    def test_translator_chars_zero_when_only_eos(self):
        """A turn that produced no visible content keeps both counters at 0."""
        t = AnthropicStreamTranslator("qwen3-thinking")
        list(t.start())
        list(t.handle_chunk({
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 1},
        }))
        assert t.text_chars == 0
        assert t.thinking_chars == 0

    def test_empty_turn_warning_fires_on_single_token(self):
        msg = empty_turn_warning(
            "qwen3.6-27b", input_tokens=1200, output_tokens=1,
            stop_reason="end_turn", text_chars=0, thinking_chars=0,
            req_shape="msgs=40 asst=20 asst_thinking=18 asst_empty=2",
        )
        assert msg is not None
        assert "qwen3.6-27b" in msg
        assert "out=1" in msg
        assert "asst_empty=2" in msg

    def test_empty_turn_warning_fires_on_zero_tokens(self):
        msg = empty_turn_warning(
            "qwen3.6-27b", input_tokens=900, output_tokens=0,
            stop_reason="end_turn", text_chars=0, thinking_chars=0,
            req_shape="msgs=10 asst=4 asst_thinking=4 asst_empty=0",
        )
        assert msg is not None
        assert "out=0" in msg

    def test_empty_turn_warning_silent_on_normal_turn(self):
        """A turn with real output (>1 token) must not warn."""
        assert empty_turn_warning(
            "qwen3.6-27b", input_tokens=1200, output_tokens=57,
            stop_reason="end_turn", text_chars=210, thinking_chars=40,
            req_shape="msgs=40 asst=20 asst_thinking=18 asst_empty=0",
        ) is None

    def test_summarize_request_shape_counts_thinking_and_empty_assistant(self):
        body = {
            "messages": [
                {"role": "user", "content": "hi"},
                # thinking + text → has visible text, not empty; has thinking
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "hello"},
                ]},
                # thinking only → no text, no tool → EMPTY (the degenerate kind)
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "hmm2"},
                ]},
                # tool_use only → no text but has a tool call → NOT empty
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "t1", "name": "f", "input": {}},
                ]},
            ]
        }
        s = summarize_request_shape(body)
        assert "msgs=4" in s
        assert "asst=3" in s
        assert "asst_thinking=2" in s
        assert "asst_empty=1" in s

    def test_summarize_request_shape_plain_string_assistant(self):
        """A normal assistant string answer is neither empty nor thinking."""
        body = {
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello there"},
            ]
        }
        s = summarize_request_shape(body)
        assert "asst=1" in s
        assert "asst_thinking=0" in s
        assert "asst_empty=0" in s


class TestObservabilityIOShape:
    """The Anthropic endpoints must record Langfuse I/O in Anthropic shape (the
    original content blocks), NOT the gateway's internal OpenAI pivot."""

    def test_request_io_bare_messages_when_no_system_or_tools(self):
        messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        body = {"model": "claude", "messages": messages}
        # No system/tools -> bare messages list (renders as a conversation).
        assert anthropic_request_io(body) == messages

    def test_request_io_keeps_system_and_tools(self):
        messages = [{"role": "user", "content": "hi"}]
        tools = [{"name": "get_weather", "input_schema": {"type": "object"}}]
        body = {
            "model": "claude",
            "system": "You are helpful.",
            "messages": messages,
            "tools": tools,
        }
        io = anthropic_request_io(body)
        assert io["messages"] == messages
        assert io["system"] == "You are helpful."
        assert io["tools"] == tools
        # The OpenAI pivot would have folded system into a system message — the
        # Anthropic capture keeps it as the top-level Anthropic field instead.
        assert io["messages"] == messages

    def test_message_to_anthropic_text_and_tool_use(self):
        # An accumulated OpenAI assistant message (StreamingChatOutput.as_message
        # shape) is translated to Anthropic content blocks.
        openai_msg = {
            "role": "assistant",
            "content": "Let me check.",
            "reasoning_content": "thinking...",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "SF"}'},
                }
            ],
        }
        out = openai_message_to_anthropic(openai_msg, "claude")
        assert out["role"] == "assistant"
        types = [b["type"] for b in out["content"]]
        # thinking block first, then text, then tool_use (Anthropic ordering).
        assert types == ["thinking", "text", "tool_use"]
        tool_block = out["content"][-1]
        assert tool_block["name"] == "get_weather"
        assert tool_block["input"] == {"city": "SF"}

    def test_message_to_anthropic_none_when_empty(self):
        assert openai_message_to_anthropic(None, "claude") is None
