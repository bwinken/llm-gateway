"""Tests for the AWS Bedrock backend (/aws/v1/* + unified /v1/* dispatch).

The gateway forwards every Bedrock LLM/VLM call to the Converse API
(``{endpoint}/model/{modelId}/converse[-stream]``) and translates back to
the public surface (OpenAI chat completions or Anthropic Messages). Mocked
downstream responses therefore use the Converse shape; streams are binary
AWS event-stream frames built by ``FakeBedrockStreamResponse``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from tests.conftest import (
    FakeBedrockStreamResponse,
    auth_header,
    make_httpx_response,
)


def _converse_payload(text: str, input_tk: int, output_tk: int, stop_reason: str = "end_turn"):
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": input_tk, "outputTokens": output_tk,
                  "totalTokens": input_tk + output_tk},
    }


def _stream_events(text_pieces: list[str], input_tk: int = 5, output_tk: int = 3):
    events: list[tuple[str, dict]] = [("messageStart", {"role": "assistant"})]
    for piece in text_pieces:
        events.append(("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": piece}}))
    events.append(("contentBlockStop", {"contentBlockIndex": 0}))
    events.append(("messageStop", {"stopReason": "end_turn"}))
    events.append(("metadata", {"usage": {"inputTokens": input_tk, "outputTokens": output_tk,
                                          "totalTokens": input_tk + output_tk}}))
    return events


# ---------------------------------------------------------------------------
# Event-stream codec
# ---------------------------------------------------------------------------

class TestEventStreamDecoder:
    def test_roundtrip_single_message(self):
        from app.services.aws_eventstream import EventStreamDecoder, encode_event

        frame = encode_event(
            {":message-type": "event", ":event-type": "messageStart"},
            b'{"role": "assistant"}',
        )
        msgs = list(EventStreamDecoder().feed(frame))
        assert len(msgs) == 1
        assert msgs[0].event_type == "messageStart"
        assert json.loads(msgs[0].payload) == {"role": "assistant"}

    def test_partial_feed_reassembles(self):
        # A frame split across arbitrary chunk boundaries must decode once
        # the last byte arrives — the aiter_bytes chunking is not aligned.
        from app.services.aws_eventstream import EventStreamDecoder, encode_event

        frame = encode_event(
            {":message-type": "event", ":event-type": "contentBlockDelta"},
            json.dumps({"delta": {"text": "hello"}}).encode(),
        )
        dec = EventStreamDecoder()
        out = []
        for i in range(0, len(frame), 7):
            out.extend(dec.feed(frame[i:i + 7]))
        assert len(out) == 1
        assert out[0].event_type == "contentBlockDelta"

    def test_multiple_messages_in_one_chunk(self):
        from app.services.aws_eventstream import EventStreamDecoder, encode_event

        f1 = encode_event({":event-type": "a"}, b"{}")
        f2 = encode_event({":event-type": "b"}, b"{}")
        msgs = list(EventStreamDecoder().feed(f1 + f2))
        assert [m.event_type for m in msgs] == ["a", "b"]

    def test_corrupt_crc_raises(self):
        import pytest

        from app.services.aws_eventstream import (
            EventStreamDecoder,
            EventStreamError,
            encode_event,
        )

        frame = bytearray(encode_event({":event-type": "a"}, b"{}"))
        frame[-1] ^= 0xFF  # corrupt message CRC
        with pytest.raises(EventStreamError):
            list(EventStreamDecoder().feed(bytes(frame)))

    def test_exception_headers_exposed(self):
        from app.services.aws_eventstream import EventStreamDecoder, encode_event

        frame = encode_event(
            {":message-type": "exception", ":exception-type": "throttlingException"},
            b'{"message": "slow down"}',
        )
        msg = next(iter(EventStreamDecoder().feed(frame)))
        assert msg.message_type == "exception"
        assert msg.exception_type == "throttlingException"


# ---------------------------------------------------------------------------
# Converse adapter (request direction)
# ---------------------------------------------------------------------------

class TestConverseRequestTranslation:
    def _xlate(self, body, model_id="anthropic.claude-x"):
        from app.services.converse_adapter import openai_chat_to_converse_request
        return openai_chat_to_converse_request(body, model_id=model_id)

    def test_system_hoisted_to_system_field(self):
        out = self._xlate({
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hi"},
            ],
        })
        assert out["system"] == [{"text": "be brief"}]
        assert out["messages"] == [{"role": "user", "content": [{"text": "hi"}]}]

    def test_tool_result_becomes_user_tool_result_block(self):
        out = self._xlate({
            "messages": [
                {"role": "user", "content": "weather?"},
                {"role": "assistant", "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "SF"}'},
                }]},
                {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
            ],
        })
        assert out["messages"][1]["content"] == [{
            "toolUse": {"toolUseId": "call_1", "name": "get_weather", "input": {"city": "SF"}},
        }]
        assert out["messages"][2]["role"] == "user"
        assert out["messages"][2]["content"][0]["toolResult"]["toolUseId"] == "call_1"

    def test_consecutive_same_role_messages_merged(self):
        # Converse enforces strict user/assistant alternation.
        out = self._xlate({
            "messages": [
                {"role": "user", "content": "a"},
                {"role": "user", "content": "b"},
            ],
        })
        assert len(out["messages"]) == 1
        assert out["messages"][0]["content"] == [{"text": "a"}, {"text": "b"}]

    def test_empty_translation_gets_probe_placeholder(self):
        # System-only probe (Roo Code connection check) must not send an
        # empty messages list — Bedrock 400s on it.
        out = self._xlate({"messages": [{"role": "system", "content": "probe"}]})
        assert out["messages"] == [{"role": "user", "content": [{"text": "."}]}]

    def test_tools_translated_to_tool_config(self):
        out = self._xlate({
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {
                "name": "f", "description": "d", "parameters": {"type": "object"},
            }}],
            "tool_choice": "required",
        })
        spec = out["toolConfig"]["tools"][0]["toolSpec"]
        assert spec["name"] == "f"
        assert spec["inputSchema"] == {"json": {"type": "object"}}
        assert out["toolConfig"]["toolChoice"] == {"any": {}}

    def test_sampling_params_forwarded_in_inference_config(self):
        out = self._xlate({
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 100, "temperature": 0.5, "top_p": 0.9, "stop": "END",
        })
        assert out["inferenceConfig"] == {
            "maxTokens": 100, "temperature": 0.5, "topP": 0.9, "stopSequences": ["END"],
        }

    def test_reasoning_effort_maps_to_claude_thinking(self):
        out = self._xlate({
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "high", "temperature": 0.7,
        }, model_id="anthropic.claude-sonnet-4-20250514-v1:0")
        assert out["additionalModelRequestFields"]["thinking"]["type"] == "enabled"
        assert out["additionalModelRequestFields"]["thinking"]["budget_tokens"] == 16384
        # Anthropic rejects sampling overrides alongside thinking.
        assert "inferenceConfig" not in out or "temperature" not in out.get("inferenceConfig", {})

    def test_reasoning_effort_dropped_for_unknown_family(self):
        out = self._xlate({
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "high",
        }, model_id="amazon.nova-pro-v1:0")
        assert "additionalModelRequestFields" not in out

    def test_xhigh_effort_gets_larger_claude_budget(self):
        out = self._xlate({
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "xhigh",
        }, model_id="anthropic.claude-sonnet-4-20250514-v1:0")
        assert out["additionalModelRequestFields"]["thinking"]["budget_tokens"] == 32768

    def test_none_effort_disables_claude_thinking(self):
        out = self._xlate({
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "none", "temperature": 0.7,
        }, model_id="anthropic.claude-sonnet-4-20250514-v1:0")
        # thinking off entirely — and sampling overrides stay usable
        assert "additionalModelRequestFields" not in out
        assert out["inferenceConfig"]["temperature"] == 0.7

    def test_effort_passthrough_for_openai_family(self):
        out = self._xlate({
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "xhigh",
        }, model_id="openai.gpt-oss-120b-1:0")
        assert out["additionalModelRequestFields"]["reasoning_effort"] == "xhigh"

    def test_assistant_reasoning_content_not_replayed(self):
        # Bedrock validates thinking signatures the gateway can't produce.
        out = self._xlate({
            "messages": [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a", "reasoning_content": "hmm"},
                {"role": "user", "content": "q2"},
            ],
        })
        assistant = out["messages"][1]
        assert assistant["content"] == [{"text": "a"}]


# ---------------------------------------------------------------------------
# Converse adapter (response direction)
# ---------------------------------------------------------------------------

class TestConverseResponseTranslation:
    def test_text_and_usage(self):
        from app.services.converse_adapter import converse_to_openai_chat_response

        out = converse_to_openai_chat_response(_converse_payload("hi", 5, 2), "bedrock-claude")
        msg = out["choices"][0]["message"]
        assert msg["content"] == "hi"
        assert out["choices"][0]["finish_reason"] == "stop"
        assert out["usage"] == {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
        assert out["model"] == "bedrock-claude"

    def test_tool_use_becomes_tool_calls(self):
        from app.services.converse_adapter import converse_to_openai_chat_response

        data = {
            "output": {"message": {"role": "assistant", "content": [
                {"toolUse": {"toolUseId": "t1", "name": "f", "input": {"x": 1}}},
            ]}},
            "stopReason": "tool_use",
            "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        }
        out = converse_to_openai_chat_response(data, "bedrock-claude")
        tc = out["choices"][0]["message"]["tool_calls"][0]
        assert tc["id"] == "t1"
        assert tc["function"]["name"] == "f"
        assert json.loads(tc["function"]["arguments"]) == {"x": 1}
        assert out["choices"][0]["finish_reason"] == "tool_calls"

    def test_reasoning_content_extracted(self):
        from app.services.converse_adapter import converse_to_openai_chat_response

        data = {
            "output": {"message": {"role": "assistant", "content": [
                {"reasoningContent": {"reasoningText": {"text": "think...", "signature": "s"}}},
                {"text": "answer"},
            ]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        }
        msg = converse_to_openai_chat_response(data, "a")["choices"][0]["message"]
        assert msg["reasoning_content"] == "think..."
        assert msg["content"] == "answer"

    def test_cached_tokens_surfaced(self):
        from app.services.converse_adapter import converse_to_openai_chat_response

        data = _converse_payload("x", 100, 1)
        data["usage"]["cacheReadInputTokens"] = 40
        out = converse_to_openai_chat_response(data, "a")
        assert out["usage"]["prompt_tokens_details"] == {"cached_tokens": 40}

    def test_max_tokens_stop_reason(self):
        from app.services.converse_adapter import converse_to_openai_chat_response

        out = converse_to_openai_chat_response(
            _converse_payload("x", 1, 1, stop_reason="max_tokens"), "a",
        )
        assert out["choices"][0]["finish_reason"] == "length"


class TestConverseStreamTranslator:
    def _drain(self, events):
        from app.services.converse_adapter import ConverseToChatStreamTranslator

        t = ConverseToChatStreamTranslator("alias")
        chunks = list(t.start())
        for etype, payload in events:
            chunks.extend(t.handle_event(etype, payload))
        chunks.extend(t.finish())
        return t, chunks

    def test_text_stream(self):
        t, chunks = self._drain(_stream_events(["he", "llo"]))
        text = "".join(
            c["choices"][0]["delta"].get("content") or "" for c in chunks
        )
        assert text == "hello"
        assert t.input_tokens == 5
        assert t.output_tokens == 3
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
        assert chunks[-1]["usage"]["prompt_tokens"] == 5

    def test_tool_call_stream(self):
        events = [
            ("messageStart", {"role": "assistant"}),
            ("contentBlockStart", {"contentBlockIndex": 1, "start": {
                "toolUse": {"toolUseId": "t1", "name": "f"}}}),
            ("contentBlockDelta", {"contentBlockIndex": 1, "delta": {
                "toolUse": {"input": '{"x":'}}}),
            ("contentBlockDelta", {"contentBlockIndex": 1, "delta": {
                "toolUse": {"input": '1}'}}}),
            ("contentBlockStop", {"contentBlockIndex": 1}),
            ("messageStop", {"stopReason": "tool_use"}),
            ("metadata", {"usage": {"inputTokens": 2, "outputTokens": 2}}),
        ]
        t, chunks = self._drain(events)
        tool_chunks = [c for c in chunks if c["choices"][0]["delta"].get("tool_calls")]
        assert tool_chunks[0]["choices"][0]["delta"]["tool_calls"][0]["id"] == "t1"
        args = "".join(
            tc["function"].get("arguments", "")
            for c in tool_chunks for tc in c["choices"][0]["delta"]["tool_calls"]
        )
        assert args == '{"x":1}'
        assert t.finish_reason == "tool_calls"

    def test_throttling_exception_captured(self):
        events = [("throttlingException", {"message": "Too many requests"})]
        t, _ = self._drain(events)
        assert "Too many requests" in t.derive_error_message()
        assert t.derive_error_kind() == "overloaded_error"

    def test_validation_exception_is_invalid_request(self):
        events = [("validationException", {"message": "bad input"})]
        t, _ = self._drain(events)
        assert t.derive_error_kind() == "invalid_request_error"

    def test_reasoning_delta_stream(self):
        events = [
            ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {
                "reasoningContent": {"text": "thinking"}}}),
            ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "answer"}}),
            ("messageStop", {"stopReason": "end_turn"}),
        ]
        t, chunks = self._drain(events)
        reasoning = "".join(
            c["choices"][0]["delta"].get("reasoning_content") or "" for c in chunks
        )
        assert reasoning == "thinking"
        assert t.emitted_reasoning_chars == len("thinking")


# ---------------------------------------------------------------------------
# /aws/v1/* endpoints
# ---------------------------------------------------------------------------

class TestBedrockModelsListing:
    def test_list_returns_configured_aliases(self, client):
        resp = client.get("/aws/v1/models", headers=auth_header())
        assert resp.status_code == 200
        ids = [m["id"] for m in resp.json()["data"]]
        assert "bedrock-claude" in ids
        assert "bedrock-nova" in ids

    def test_models_marked_as_bedrock_owner(self, client):
        resp = client.get("/aws/v1/models", headers=auth_header())
        for entry in resp.json()["data"]:
            assert entry["owned_by"] == "aws-bedrock"

    def test_requires_auth(self, client):
        assert client.get("/aws/v1/models").status_code == 401

    def test_blocked_without_can_use_bedrock(self, client, db_session, test_user):
        test_user.can_use_bedrock = False
        db_session.add(test_user); db_session.commit()
        resp = client.get("/aws/v1/models", headers=auth_header())
        assert resp.status_code == 403

    def test_v1_and_non_v1_aliases_return_same_data(self, client):
        r1 = client.get("/aws/v1/models", headers=auth_header())
        r2 = client.get("/aws/models", headers=auth_header())
        assert r1.json() == r2.json()


class TestBedrockChatCompletions:
    def test_basic_completion(self, client):
        client.__httpx_mock__.post = AsyncMock(
            return_value=make_httpx_response(200, _converse_payload("hi", 5, 2)),
        )
        resp = client.post(
            "/aws/v1/chat/completions",
            json={"model": "bedrock-claude", "messages": [{"role": "user", "content": "hello"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "hi"
        assert resp.json()["usage"]["prompt_tokens"] == 5
        assert resp.json()["usage"]["completion_tokens"] == 2

    def test_converse_url_and_bearer_auth(self, client):
        captured: dict = {}

        async def fake_post(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            captured["json"] = kwargs.get("json")
            return make_httpx_response(200, _converse_payload("ok", 1, 1))

        client.__httpx_mock__.post = AsyncMock(side_effect=fake_post)
        resp = client.post(
            "/aws/v1/chat/completions",
            json={"model": "bedrock-claude", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        # model_id is URL-quoted into the path; ':' must be encoded.
        assert captured["url"] == (
            "https://bedrock-runtime.us-east-1.amazonaws.com/model/"
            "anthropic.claude-sonnet-4-20250514-v1%3A0/converse"
        )
        assert captured["headers"]["Authorization"] == "Bearer bedrock-test-key"
        # Body carries Converse shape, no model field (model is in the URL).
        assert "model" not in captured["json"]
        assert captured["json"]["messages"][0]["content"] == [{"text": "hi"}]

    def test_unknown_alias_falls_back_with_header(self, client):
        client.__httpx_mock__.post = AsyncMock(
            return_value=make_httpx_response(200, _converse_payload("ok", 1, 1)),
        )
        resp = client.post(
            "/aws/v1/chat/completions",
            json={"model": "no-such-model", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert "X-Model-Fallback" in resp.headers

    def test_downstream_error_passthrough(self, client):
        client.__httpx_mock__.post = AsyncMock(
            return_value=make_httpx_response(400, {"message": "validation error"}),
        )
        resp = client.post(
            "/aws/v1/chat/completions",
            json={"model": "bedrock-claude", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 400

    def test_stream_completion(self, client):
        client.__httpx_mock__.send = AsyncMock(
            return_value=FakeBedrockStreamResponse(_stream_events(["he", "llo"])),
        )
        client.__httpx_mock__.build_request = lambda *a, **k: None
        resp = client.post(
            "/aws/v1/chat/completions",
            json={"model": "bedrock-claude", "stream": True,
                  "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        body = resp.text
        assert "data: [DONE]" in body
        text = "".join(
            (json.loads(line[6:])["choices"][0]["delta"].get("content") or "")
            for line in body.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        )
        assert text == "hello"

    def test_stream_preflight_4xx_returns_json_error(self, client):
        client.__httpx_mock__.send = AsyncMock(
            return_value=FakeBedrockStreamResponse(
                [], status_code=429,
                body_bytes=json.dumps({"message": "Too many requests"}).encode(),
            ),
        )
        client.__httpx_mock__.build_request = lambda *a, **k: None
        resp = client.post(
            "/aws/v1/chat/completions",
            json={"model": "bedrock-claude", "stream": True,
                  "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 429


class TestBedrockMessages:
    def test_non_stream_messages(self, client):
        client.__httpx_mock__.post = AsyncMock(
            return_value=make_httpx_response(200, _converse_payload("hello there", 7, 3)),
        )
        resp = client.post(
            "/aws/v1/messages",
            json={"model": "bedrock-claude", "max_tokens": 64,
                  "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "assistant"
        assert data["content"][0]["type"] == "text"
        assert data["content"][0]["text"] == "hello there"
        assert data["usage"] == {"input_tokens": 7, "output_tokens": 3}

    def test_stream_messages_emits_anthropic_events(self, client):
        client.__httpx_mock__.send = AsyncMock(
            return_value=FakeBedrockStreamResponse(_stream_events(["hi"])),
        )
        client.__httpx_mock__.build_request = lambda *a, **k: None
        resp = client.post(
            "/aws/v1/messages",
            json={"model": "bedrock-claude", "max_tokens": 64, "stream": True,
                  "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        body = resp.text
        assert "event: message_start" in body
        assert "event: content_block_delta" in body
        assert "event: message_stop" in body

    def test_messages_alias_without_v1(self, client):
        client.__httpx_mock__.post = AsyncMock(
            return_value=make_httpx_response(200, _converse_payload("ok", 1, 1)),
        )
        resp = client.post(
            "/aws/messages",
            json={"model": "bedrock-claude", "max_tokens": 8,
                  "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200

    def test_count_tokens_estimate(self, client):
        resp = client.post(
            "/aws/v1/messages/count_tokens",
            json={"model": "bedrock-claude",
                  "messages": [{"role": "user", "content": "hello world, four words"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert resp.json()["input_tokens"] > 0


# ---------------------------------------------------------------------------
# Unified /v1/* dispatch
# ---------------------------------------------------------------------------

class TestUnifiedDispatchToBedrock:
    def test_v1_models_includes_bedrock_when_permitted(self, client):
        resp = client.get("/v1/models", headers=auth_header())
        ids = [m["id"] for m in resp.json()["data"]]
        assert "bedrock-claude" in ids
        assert "test-llm" in ids  # vLLM entries still present

    def test_v1_models_hides_bedrock_without_permission(self, client, db_session, test_user):
        test_user.can_use_bedrock = False
        db_session.add(test_user); db_session.commit()
        resp = client.get("/v1/models", headers=auth_header())
        ids = [m["id"] for m in resp.json()["data"]]
        assert "bedrock-claude" not in ids

    def test_v1_chat_dispatches_to_bedrock(self, client):
        client.__httpx_mock__.post = AsyncMock(
            return_value=make_httpx_response(200, _converse_payload("from bedrock", 1, 1)),
        )
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "bedrock-claude", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "from bedrock"
        # Confirm the call actually went to Bedrock (Converse URL).
        url = client.__httpx_mock__.post.call_args.args[0]
        assert "bedrock-runtime" in url

    def test_v1_messages_dispatches_to_bedrock(self, client):
        client.__httpx_mock__.post = AsyncMock(
            return_value=make_httpx_response(200, _converse_payload("ok", 1, 1)),
        )
        resp = client.post(
            "/v1/messages",
            json={"model": "bedrock-claude", "max_tokens": 8,
                  "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "assistant"

    def test_bedrock_alias_without_permission_falls_back_to_vllm(
        self, client, db_session, test_user,
    ):
        # Same "be liberal with unknown aliases" stance as Azure: the alias
        # quietly falls through _resolve_model to the vLLM default.
        test_user.can_use_bedrock = False
        db_session.add(test_user); db_session.commit()
        client.__httpx_mock__.post = AsyncMock(
            return_value=make_httpx_response(200, {
                "id": "x", "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "vllm"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }),
        )
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "bedrock-claude", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        url = client.__httpx_mock__.post.call_args.args[0]
        assert "bedrock-runtime" not in url  # went to the mocked vLLM downstream

    def test_bedrock_sub_limit_429_on_unified_route(self, client, db_session, test_user):
        from datetime import datetime, timezone
        from decimal import Decimal

        from app.models.schema import UsageLog

        test_user.bedrock_daily_limit_usd = 0.5
        db_session.add(test_user)
        db_session.add(UsageLog(
            user_id=test_user.id, model="bedrock-claude", model_type="llm",
            input_tokens=1, output_tokens=1, cost_usd=Decimal("1.0"),
            endpoint="/aws/v1/chat/completions", backend="bedrock",
            created_at=datetime.now(timezone.utc),
        ))
        db_session.commit()

        resp = client.post(
            "/v1/chat/completions",
            json={"model": "bedrock-claude", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 429
        assert "Bedrock" in resp.json()["detail"]

    def test_vllm_not_affected_by_bedrock_sub_limit(self, client, db_session, test_user):
        from datetime import datetime, timezone
        from decimal import Decimal

        from app.models.schema import UsageLog

        test_user.bedrock_daily_limit_usd = 0.5
        db_session.add(test_user)
        db_session.add(UsageLog(
            user_id=test_user.id, model="bedrock-claude", model_type="llm",
            input_tokens=1, output_tokens=1, cost_usd=Decimal("1.0"),
            endpoint="/aws/v1/chat/completions", backend="bedrock",
            created_at=datetime.now(timezone.utc),
        ))
        db_session.commit()

        client.__httpx_mock__.post = AsyncMock(
            return_value=make_httpx_response(200, {
                "id": "x", "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }),
        )
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
