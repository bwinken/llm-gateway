"""Unit tests for OpenAI chat completions <-> Azure Responses API translation."""

from __future__ import annotations

import json

from app.services.responses_adapter import (
    ResponsesToChatStreamTranslator,
    openai_chat_to_responses_request,
    responses_to_openai_chat_response,
)


class TestRequestTranslation:
    def test_simple_user_message(self):
        out = openai_chat_to_responses_request({
            "model": "alias",
            "messages": [{"role": "user", "content": "hi"}],
        }, model="real-deployment")
        assert out["model"] == "real-deployment"
        assert out["input"] == [{
            "role": "user",
            "content": [{"type": "input_text", "text": "hi"}],
        }]
        assert "instructions" not in out

    def test_system_hoisted_to_instructions(self):
        out = openai_chat_to_responses_request({
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hi"},
            ],
        })
        assert out["instructions"] == "be brief"
        # No system role in input
        roles = [it.get("role") for it in out["input"] if "role" in it]
        assert "system" not in roles

    def test_multiple_system_messages_concatenated(self):
        out = openai_chat_to_responses_request({
            "messages": [
                {"role": "system", "content": "a"},
                {"role": "system", "content": "b"},
                {"role": "user", "content": "hi"},
            ],
        })
        assert out["instructions"] == "a\n\nb"

    def test_max_tokens_to_max_output_tokens(self):
        out = openai_chat_to_responses_request({
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 128,
        })
        assert out["max_output_tokens"] == 128

    def test_max_completion_tokens_preferred_over_max_tokens(self):
        out = openai_chat_to_responses_request({
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 100,
            "max_completion_tokens": 200,
        })
        assert out["max_output_tokens"] == 200

    def test_reasoning_effort_mapping(self):
        out = openai_chat_to_responses_request({
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "high",
        })
        assert out["reasoning"] == {"effort": "high"}

    def test_tools_flat_form(self):
        out = openai_chat_to_responses_request({
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "weather",
                    "description": "Get the weather",
                    "parameters": {"type": "object", "properties": {}},
                },
            }],
        })
        assert out["tools"] == [{
            "type": "function",
            "name": "weather",
            "description": "Get the weather",
            "parameters": {"type": "object", "properties": {}},
        }]

    def test_tool_call_history_converted_to_function_call_items(self):
        out = openai_chat_to_responses_request({
            "messages": [
                {"role": "user", "content": "what's the weather?"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "weather", "arguments": '{"city":"Paris"}'},
                    }],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
                {"role": "user", "content": "thanks"},
            ],
        })
        types = [it.get("type") for it in out["input"]]
        assert "function_call" in types
        assert "function_call_output" in types
        # Match the tool call by call_id
        fc = next(it for it in out["input"] if it.get("type") == "function_call")
        assert fc["call_id"] == "call_1"
        assert fc["name"] == "weather"
        fco = next(it for it in out["input"] if it.get("type") == "function_call_output")
        assert fco["call_id"] == "call_1"
        assert fco["output"] == "sunny"

    def test_image_url_to_input_image(self):
        out = openai_chat_to_responses_request({
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxxx"}},
                ],
            }],
        })
        parts = out["input"][0]["content"]
        types = [p["type"] for p in parts]
        assert "input_text" in types
        assert "input_image" in types

    def test_sampling_params_always_stripped(self):
        """Azure deployments differ on which sampling knobs they accept
        (gpt-5.4 ok with temperature, gpt-5.4-pro rejects it). To keep
        the adapter free of per-deployment quirks the Azure path drops
        all four knobs unconditionally and lets each deployment use its
        own configured defaults."""
        out = openai_chat_to_responses_request({
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0,
            "top_p": 0.9,
            "presence_penalty": 0.2,
            "frequency_penalty": 0.3,
        })
        for k in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
            assert k not in out, f"{k} should have been stripped"

    def test_reasoning_effort_still_passes_through(self):
        """`reasoning_effort` is the one knob clients can still influence;
        it's not a sampling knob and the underlying model accepts it."""
        out = openai_chat_to_responses_request({
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "medium",
        })
        assert out["reasoning"] == {"effort": "medium"}


class TestOrphanFunctionCallDropping:
    """Roo Code mixes provider-native tool calls (assistant emits
    function_call) with inline XML-tagged tool results (user message
    text). The function_call sits in history without a paired
    function_call_output, and Azure 400s on the orphan. The translator
    drops the orphan to keep the conversation moving."""

    def test_orphan_function_call_dropped(self):
        out = openai_chat_to_responses_request({
            "messages": [
                {"role": "user", "content": "look up X"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_orphan",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{}"},
                    }],
                },
                # No `role: "tool"` here — Roo Code inlined the result
                # into the next user message instead.
                {"role": "user", "content": "<result>foo</result> what next?"},
            ],
        })
        types = [it.get("type", "message") for it in out["input"]]
        assert "function_call" not in types, "orphan should be dropped"
        # Two message items survived (initial user + follow-up user)
        assert types.count("message") == 2

    def test_paired_function_call_preserved(self):
        out = openai_chat_to_responses_request({
            "messages": [
                {"role": "user", "content": "look up X"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_paired",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{}"},
                    }],
                },
                {"role": "tool", "tool_call_id": "call_paired", "content": "result"},
                {"role": "user", "content": "thanks"},
            ],
        })
        types = [it.get("type", "message") for it in out["input"]]
        # function_call + function_call_output both present
        assert "function_call" in types
        assert "function_call_output" in types
        # Walk the pairing
        fc = next(it for it in out["input"] if it.get("type") == "function_call")
        fco = next(it for it in out["input"] if it.get("type") == "function_call_output")
        assert fc["call_id"] == fco["call_id"] == "call_paired"

    def test_partial_pairing_drops_only_orphans(self):
        out = openai_chat_to_responses_request({
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "call_X", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}},
                        {"id": "call_Y", "type": "function",
                         "function": {"name": "g", "arguments": "{}"}},
                    ],
                },
                # Only Y gets a tool result; X is orphaned.
                {"role": "tool", "tool_call_id": "call_Y", "content": "y_result"},
            ],
        })
        fcs = [it for it in out["input"] if it.get("type") == "function_call"]
        assert len(fcs) == 1
        assert fcs[0]["call_id"] == "call_Y"


class TestNonStreamResponseTranslation:
    def test_text_only_response(self):
        data = {
            "id": "resp_1",
            "status": "completed",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello world"}],
            }],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }
        out = responses_to_openai_chat_response(data, "my-alias")
        assert out["id"] == "resp_1"
        assert out["model"] == "my-alias"
        assert out["choices"][0]["message"]["content"] == "Hello world"
        assert out["choices"][0]["message"]["role"] == "assistant"
        assert out["choices"][0]["finish_reason"] == "stop"
        assert out["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    def test_function_call_in_response(self):
        data = {
            "id": "resp_2",
            "status": "completed",
            "output": [{
                "type": "function_call",
                "call_id": "call_42",
                "name": "weather",
                "arguments": '{"city":"NYC"}',
            }],
            "usage": {"input_tokens": 3, "output_tokens": 8, "total_tokens": 11},
        }
        out = responses_to_openai_chat_response(data, "alias")
        msg = out["choices"][0]["message"]
        assert msg["tool_calls"][0]["id"] == "call_42"
        assert msg["tool_calls"][0]["function"]["name"] == "weather"
        assert msg["tool_calls"][0]["function"]["arguments"] == '{"city":"NYC"}'
        assert out["choices"][0]["finish_reason"] == "tool_calls"

    def test_reasoning_summary_in_response(self):
        data = {
            "id": "resp_3",
            "status": "completed",
            "output": [
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "Let me think..."}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Answer"}],
                },
            ],
            "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        }
        out = responses_to_openai_chat_response(data, "alias")
        msg = out["choices"][0]["message"]
        assert msg["reasoning_content"] == "Let me think..."
        assert msg["content"] == "Answer"

    def test_incomplete_max_output_tokens_maps_to_length(self):
        data = {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "trunc..."}],
            }],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        out = responses_to_openai_chat_response(data, "alias")
        assert out["choices"][0]["finish_reason"] == "length"

    def test_cached_tokens_carried_through(self):
        data = {
            "status": "completed",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "x"}],
            }],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 5,
                "input_tokens_details": {"cached_tokens": 60},
            },
        }
        out = responses_to_openai_chat_response(data, "alias")
        assert out["usage"]["prompt_tokens_details"]["cached_tokens"] == 60


class TestStreamingTranslation:
    def test_text_delta_to_content_delta(self):
        t = ResponsesToChatStreamTranslator("alias")
        # Drain the initial start() chunk
        list(t.start())
        chunks = list(t.handle_event({
            "type": "response.output_text.delta",
            "delta": "Hello",
        }))
        assert len(chunks) == 1
        assert chunks[0]["choices"][0]["delta"]["content"] == "Hello"

    def test_completed_records_usage_and_finish(self):
        t = ResponsesToChatStreamTranslator("alias")
        list(t.start())
        list(t.handle_event({
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 8,
                    "input_tokens_details": {"cached_tokens": 5},
                },
            },
        }))
        final = list(t.finish())
        assert final[-1]["choices"][0]["finish_reason"] == "stop"
        assert final[-1]["usage"]["prompt_tokens"] == 12
        assert final[-1]["usage"]["completion_tokens"] == 8
        assert final[-1]["usage"]["prompt_tokens_details"]["cached_tokens"] == 5

    def test_function_call_streaming(self):
        t = ResponsesToChatStreamTranslator("alias")
        list(t.start())

        # Tool call shell announced
        chunks = list(t.handle_event({
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "weather",
                "arguments": "",
            },
        }))
        assert len(chunks) == 1
        tc = chunks[0]["choices"][0]["delta"]["tool_calls"][0]
        assert tc["id"] == "call_1"
        assert tc["function"]["name"] == "weather"
        assert tc["function"]["arguments"] == ""
        assert tc["index"] == 0

        # Argument delta
        chunks = list(t.handle_event({
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "delta": '{"city":',
        }))
        assert len(chunks) == 1
        tc = chunks[0]["choices"][0]["delta"]["tool_calls"][0]
        assert tc["index"] == 0
        assert tc["function"]["arguments"] == '{"city":'

        # Completion
        list(t.handle_event({
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        }))
        final = list(t.finish())
        assert final[-1]["choices"][0]["finish_reason"] == "tool_calls"

    def test_reasoning_summary_delta_to_reasoning_content(self):
        t = ResponsesToChatStreamTranslator("alias")
        list(t.start())
        chunks = list(t.handle_event({
            "type": "response.reasoning_summary_text.delta",
            "delta": "Thinking...",
        }))
        assert chunks[0]["choices"][0]["delta"]["reasoning_content"] == "Thinking..."

    def test_incomplete_max_tokens_finish_reason(self):
        t = ResponsesToChatStreamTranslator("alias")
        list(t.start())
        list(t.handle_event({
            "type": "response.completed",
            "response": {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }))
        final = list(t.finish())
        assert final[-1]["choices"][0]["finish_reason"] == "length"


class TestArgumentJsonRoundTrip:
    """Tool call arguments are JSON strings; chunked deltas concatenate correctly."""
    def test_arguments_concatenate(self):
        t = ResponsesToChatStreamTranslator("alias")
        list(t.start())
        list(t.handle_event({
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_x", "call_id": "call_x",
                "name": "f", "arguments": "",
            },
        }))
        deltas = ['{"a":', ' 1, ', '"b": 2}']
        collected = ""
        for d in deltas:
            chunks = list(t.handle_event({
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_x",
                "delta": d,
            }))
            collected += chunks[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
        assert json.loads(collected) == {"a": 1, "b": 2}
