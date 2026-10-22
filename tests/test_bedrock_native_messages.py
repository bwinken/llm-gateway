"""Tests for `native_messages = true` on a Claude [bedrock_models.*] entry.

Anthropic Messages requests for such an entry go to Bedrock's own Anthropic
endpoint (``{endpoint}/anthropic/v1/messages``: first-party body shape, SSE
streaming) instead of being translated to Converse. Chat completions on the
same entry, and every non-Claude entry, keep using Converse.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from tests.conftest import (
    FakeStreamResponse,
    TEST_BEDROCK_MODELS,
    auth_header,
    make_httpx_response,
    web_auth_header,
)

NATIVE_URL = "https://bedrock-runtime.ap-northeast-1.amazonaws.com/anthropic/v1/messages"

NATIVE_ENTRY = {
    "type": "llm", "region": "ap-northeast-1",
    "model_id": "anthropic.claude-sonnet-4-6", "api_key": "bedrock-test-key",
    "native_messages": True,
}


def _message(text: str = "hi", usage: dict | None = None) -> dict:
    return {
        "id": "msg_1", "type": "message", "role": "assistant",
        "model": "anthropic.claude-sonnet-4-6",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": usage or {"input_tokens": 5, "output_tokens": 2},
    }


SSE_LINES = [
    "event: message_start",
    'data: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant",'
    '"model":"anthropic.claude-sonnet-4-6","content":[],"usage":{"input_tokens":4,'
    '"cache_read_input_tokens":100,"cache_creation_input_tokens":20,"output_tokens":1}}}',
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
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},'
    '"usage":{"output_tokens":9}}',
    "",
    "event: message_stop",
    'data: {"type":"message_stop"}',
    "",
]


@pytest.fixture
def native_entry():
    with patch.dict(TEST_BEDROCK_MODELS, {"bedrock-native": dict(NATIVE_ENTRY)}):
        yield TEST_BEDROCK_MODELS["bedrock-native"]


def _capture_post(responses: list[httpx.Response] | httpx.Response):
    """A fake client.post that records every call and answers in order."""
    queue = list(responses) if isinstance(responses, list) else [responses]
    calls: list[dict] = []

    async def fake_post(url, **kwargs):
        calls.append({
            "url": url, "headers": kwargs.get("headers") or {},
            "content": kwargs.get("content"), "json": json.loads(kwargs["content"]),
        })
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return fake_post, calls


def _post_messages(client, body: dict, path: str = "/aws/v1/messages"):
    return client.post(path, json=body, headers=auth_header())


class TestNativeNonStream:
    def test_request_goes_to_anthropic_endpoint_in_first_party_shape(self, client, native_entry):
        fake_post, calls = _capture_post(make_httpx_response(200, _message("hello")))
        with patch.object(client.__httpx_mock__, "post", side_effect=fake_post):
            resp = _post_messages(client, {
                "model": "bedrock-native", "max_tokens": 64,
                "messages": [{"role": "user", "content": "hi"}],
            })
        assert resp.status_code == 200
        (call,) = calls
        assert call["url"] == NATIVE_URL
        assert call["headers"]["anthropic-version"] == "2023-06-01"
        assert call["headers"]["Authorization"] == "Bearer bedrock-test-key"
        # The model travels in the body (the real Bedrock ID), not the URL.
        assert call["json"]["model"] == "anthropic.claude-sonnet-4-6"
        assert call["json"]["messages"] == [{"role": "user", "content": "hi"}]
        assert "anthropic_version" not in call["json"]
        data = resp.json()
        assert data["model"] == "bedrock-native"
        assert data["content"] == [{"type": "text", "text": "hello"}]

    def test_iam_credentials_sign_the_exact_bytes_sent(self, client, native_entry):
        from app.services.aws_sigv4 import sign_request

        native_entry.update(
            api_key="", aws_access_key_id="AKIDEXAMPLE",
            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        )
        fake_post, calls = _capture_post(make_httpx_response(200, _message()))
        with patch.object(client.__httpx_mock__, "post", side_effect=fake_post):
            resp = _post_messages(client, {
                "model": "bedrock-native", "max_tokens": 64,
                "messages": [{"role": "user", "content": "日本語"}],
            })
        assert resp.status_code == 200
        headers = calls[0]["headers"]
        assert "/ap-northeast-1/bedrock/aws4_request" in headers["Authorization"]
        assert native_entry["aws_secret_access_key"] not in json.dumps(headers)
        when = datetime.strptime(headers["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        expected = sign_request(
            "POST", calls[0]["url"], calls[0]["content"],
            access_key="AKIDEXAMPLE", secret_key=native_entry["aws_secret_access_key"],
            region="ap-northeast-1", service="bedrock", now=when,
        )
        assert headers["Authorization"] == expected["Authorization"]

    def test_endpoint_override_is_used(self, client, native_entry):
        native_entry["endpoint"] = "https://vpce-123.bedrock-runtime.ap-northeast-1.vpce.amazonaws.com"
        fake_post, calls = _capture_post(make_httpx_response(200, _message()))
        with patch.object(client.__httpx_mock__, "post", side_effect=fake_post):
            _post_messages(client, {
                "model": "bedrock-native", "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            })
        assert calls[0]["url"] == (
            "https://vpce-123.bedrock-runtime.ap-northeast-1.vpce.amazonaws.com/anthropic/v1/messages"
        )

    def test_caching_and_thinking_replay_pass_through(self, client, native_entry):
        """What Converse loses: cache_control breakpoints and signed thinking
        blocks in the history reach Bedrock byte for byte."""
        history = [
            {"role": "user", "content": [
                {"type": "text", "text": "big context", "cache_control": {"type": "ephemeral"}},
            ]},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "", "signature": "sig-abc"},
                {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"path": "a"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "file body"},
            ]},
        ]
        system = [{"type": "text", "text": "you are helpful", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
        fake_post, calls = _capture_post(make_httpx_response(200, _message()))
        with patch.object(client.__httpx_mock__, "post", side_effect=fake_post):
            resp = _post_messages(client, {
                "model": "bedrock-native", "max_tokens": 64, "system": system,
                "thinking": {"type": "adaptive"}, "output_config": {"effort": "high"},
                "messages": history,
            })
        assert resp.status_code == 200
        sent = calls[0]["json"]
        assert sent["messages"] == history
        assert sent["system"] == system
        assert sent["thinking"] == {"type": "adaptive"}
        assert sent["output_config"] == {"effort": "high"}

    def test_beta_only_fields_dropped(self, client, native_entry):
        fake_post, calls = _capture_post(make_httpx_response(200, _message()))
        with patch.object(client.__httpx_mock__, "post", side_effect=fake_post):
            resp = client.post(
                "/aws/v1/messages",
                json={
                    "model": "bedrock-native", "max_tokens": 64,
                    "messages": [{"role": "user", "content": "hi"}],
                    "metadata": {"user_id": "u-1"},
                    "context_management": {"edits": [{"type": "clear_tool_uses_20250919"}]},
                    "betas": ["context-management-2025-06-27"],
                    "tools": [
                        {"name": "Read", "description": "read", "strict": True, "defer_loading": True,
                         "eager_input_streaming": True,
                         "input_schema": {"type": "object", "properties": {}},
                         "cache_control": {"type": "ephemeral"}},
                        {"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"},
                    ],
                },
                headers={**auth_header(), "anthropic-beta": "context-management-2025-06-27"},
            )
        assert resp.status_code == 200
        sent = calls[0]["json"]
        for key in ("metadata", "context_management", "betas"):
            assert key not in sent
        assert sent["tools"] == [{
            "name": "Read", "description": "read", "strict": True,
            "input_schema": {"type": "object", "properties": {}},
            "cache_control": {"type": "ephemeral"},
        }]
        # The client's beta header is not forwarded either.
        assert not any(k.lower() == "anthropic-beta" for k in calls[0]["headers"])

    @pytest.mark.parametrize("model_id, thinking, kept", [
        ("anthropic.claude-sonnet-4-6", None, True),
        ("anthropic.claude-sonnet-4-6", {"type": "adaptive"}, False),
        ("anthropic.claude-sonnet-4-6", {"type": "enabled", "budget_tokens": 2048}, False),
        ("anthropic.claude-sonnet-4-6", {"type": "disabled"}, True),
        ("anthropic.claude-opus-4-7", None, False),
        ("global.anthropic.claude-haiku-5-5", None, False),
    ])
    def test_sampling_knobs_follow_the_converse_rule(self, client, native_entry, model_id, thinking, kept):
        native_entry["model_id"] = model_id
        body = {
            "model": "bedrock-native", "max_tokens": 64, "temperature": 0.3, "top_p": 0.9, "top_k": 5,
            "messages": [{"role": "user", "content": "hi"}],
        }
        if thinking:
            body["thinking"] = thinking
        fake_post, calls = _capture_post(make_httpx_response(200, _message()))
        with patch.object(client.__httpx_mock__, "post", side_effect=fake_post):
            assert _post_messages(client, body).status_code == 200
        sent = calls[0]["json"]
        for key in ("temperature", "top_p", "top_k"):
            assert (key in sent) is kept

    def test_mid_conversation_system_entry_becomes_reminder(self, client, native_entry):
        fake_post, calls = _capture_post(make_httpx_response(200, _message()))
        with patch.object(client.__httpx_mock__, "post", side_effect=fake_post):
            _post_messages(client, {
                "model": "bedrock-native", "max_tokens": 64,
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "system", "content": "file changed"},
                ],
            })
        messages = calls[0]["json"]["messages"]
        assert [m["role"] for m in messages] == ["user"]
        assert "<system-reminder>" in json.dumps(messages)

    def test_effort_map_applies_to_output_config(self, client, native_entry):
        native_entry.update(reasoning_efforts=["low", "medium", "high"], reasoning_effort_map={"max": "high"})
        fake_post, calls = _capture_post(make_httpx_response(200, _message()))
        with patch.object(client.__httpx_mock__, "post", side_effect=fake_post):
            _post_messages(client, {
                "model": "bedrock-native", "max_tokens": 64,
                "output_config": {"effort": "max"},
                "messages": [{"role": "user", "content": "hi"}],
            })
        assert calls[0]["json"]["output_config"] == {"effort": "high"}

    def test_billing_counts_cache_reads_and_writes_as_input(self, client, native_entry):
        usage = {"input_tokens": 10, "cache_read_input_tokens": 1000,
                 "cache_creation_input_tokens": 50, "output_tokens": 7}
        fake_post, _ = _capture_post(make_httpx_response(200, _message(usage=usage)))
        with patch.object(client.__httpx_mock__, "post", side_effect=fake_post), \
             patch("app.services.bedrock_proxy._log_usage") as log_usage:
            resp = _post_messages(client, {
                "model": "bedrock-native", "max_tokens": 64,
                "messages": [{"role": "user", "content": "hi"}],
            })
        assert resp.status_code == 200
        assert resp.json()["usage"] == usage  # the client sees Anthropic's own counters
        args, kwargs = log_usage.call_args
        assert args[1:6] == ("bedrock-native", "llm", 1060, 7, "/aws/v1/messages")
        assert kwargs["cached_tokens"] == 1000
        assert kwargs["backend"] == "bedrock"

    def test_aws_shaped_error_is_wrapped_in_anthropic_shape(self, client, native_entry):
        fake_post, _ = _capture_post(make_httpx_response(400, {"message": "messages: field required"}))
        with patch.object(client.__httpx_mock__, "post", side_effect=fake_post):
            resp = _post_messages(client, {"model": "bedrock-native", "max_tokens": 8, "messages": []})
        assert resp.status_code == 400
        assert resp.json() == {
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "messages: field required"},
        }

    def test_anthropic_shaped_error_passes_through(self, client, native_entry):
        err = {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
        fake_post, _ = _capture_post(make_httpx_response(529, err))
        with patch.object(client.__httpx_mock__, "post", side_effect=fake_post):
            resp = _post_messages(client, {
                "model": "bedrock-native", "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            })
        assert resp.status_code == 529
        assert resp.json() == err


class TestNativeStream:
    def _stream(self, client, lines, status_code=200, body_bytes=None):
        captured: dict = {}

        def build_request(method, url, **kwargs):
            captured.update(url=url, headers=kwargs.get("headers"), json=json.loads(kwargs["content"]))
            return httpx.Request(method, url)

        mock = client.__httpx_mock__
        with patch.object(mock, "build_request", side_effect=build_request), \
             patch.object(mock, "send", return_value=FakeStreamResponse(
                 lines, status_code=status_code, body_bytes=body_bytes)), \
             patch("app.services.bedrock_proxy._log_usage") as log_usage:
            resp = _post_messages(client, {
                "model": "bedrock-native", "max_tokens": 64, "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            })
        return resp, captured, log_usage

    def test_sse_relayed_with_alias_and_billed(self, client, native_entry):
        resp, captured, log_usage = self._stream(client, SSE_LINES)
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        assert captured["url"] == NATIVE_URL
        assert captured["json"]["stream"] is True
        assert captured["headers"]["Accept"] == "text/event-stream"
        body = resp.text
        assert "Hi there" in body
        assert "event: message_stop" in body
        assert "event: error" not in body
        assert '"model": "bedrock-native"' in body
        assert "anthropic.claude-sonnet-4-6" not in body
        args, kwargs = log_usage.call_args
        # 4 uncached + 100 cache reads + 20 cache writes; output from message_delta.
        assert args[3:5] == (124, 9)
        assert kwargs["cached_tokens"] == 100

    def test_truncated_stream_ends_with_overloaded_error(self, client, native_entry):
        resp, _, _ = self._stream(client, SSE_LINES[:9])
        assert resp.status_code == 200
        assert "event: error" in resp.text
        assert "overloaded_error" in resp.text

    def test_downstream_error_event_is_not_doubled(self, client, native_entry):
        lines = SSE_LINES[:9] + [
            "event: error",
            'data: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
            "",
        ]
        resp, _, _ = self._stream(client, lines)
        assert resp.text.count("event: error") == 1
        assert "Overloaded" in resp.text

    def test_preflight_error_is_json_not_stream(self, client, native_entry):
        resp, _, log_usage = self._stream(
            client, [], status_code=403,
            body_bytes=b'{"message": "not authorized to perform: bedrock:InvokeModelWithResponseStream"}',
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["type"] == "permission_error"
        assert "InvokeModelWithResponseStream" in resp.json()["error"]["message"]
        log_usage.assert_not_called()


class TestRouting:
    def test_chat_completions_on_native_entry_still_use_converse(self, client, native_entry):
        converse = {
            "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
            "stopReason": "end_turn", "usage": {"inputTokens": 1, "outputTokens": 1},
        }
        fake_post, calls = _capture_post(make_httpx_response(200, converse))
        with patch.object(client.__httpx_mock__, "post", side_effect=fake_post):
            resp = client.post(
                "/aws/v1/chat/completions",
                json={"model": "bedrock-native", "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )
        assert resp.status_code == 200
        assert calls[0]["url"].endswith("/model/anthropic.claude-sonnet-4-6/converse")

    def test_flag_on_non_claude_model_is_ignored(self, client):
        nova = {**TEST_BEDROCK_MODELS["bedrock-nova"], "native_messages": True}
        converse = {
            "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
            "stopReason": "end_turn", "usage": {"inputTokens": 1, "outputTokens": 1},
        }
        fake_post, calls = _capture_post(make_httpx_response(200, converse))
        with patch.dict(TEST_BEDROCK_MODELS, {"bedrock-nova": nova}), \
             patch.object(client.__httpx_mock__, "post", side_effect=fake_post):
            resp = _post_messages(client, {
                "model": "bedrock-nova", "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            })
        assert resp.status_code == 200
        assert "/converse" in calls[0]["url"]

    def test_unified_v1_messages_dispatches_natively(self, client, native_entry):
        fake_post, calls = _capture_post(make_httpx_response(200, _message("from bedrock")))
        with patch.object(client.__httpx_mock__, "post", side_effect=fake_post):
            resp = _post_messages(client, {
                "model": "bedrock-native", "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            }, path="/v1/messages")
        assert resp.status_code == 200
        assert calls[0]["url"] == NATIVE_URL
        assert resp.json()["content"][0]["text"] == "from bedrock"

    def test_rate_limit_fallback_can_land_on_a_converse_entry(self, client, native_entry):
        native_entry["rate_limit_fallback"] = "bedrock-claude"
        converse = {
            "output": {"message": {"role": "assistant", "content": [{"text": "fallback"}]}},
            "stopReason": "end_turn", "usage": {"inputTokens": 1, "outputTokens": 1},
        }
        fake_post, calls = _capture_post([
            make_httpx_response(429, {"message": "Too many requests"}),
            make_httpx_response(200, converse),
        ])
        with patch.object(client.__httpx_mock__, "post", side_effect=fake_post):
            resp = _post_messages(client, {
                "model": "bedrock-native", "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            })
        assert resp.status_code == 200
        assert calls[0]["url"] == NATIVE_URL
        assert "/converse" in calls[1]["url"]
        assert resp.json()["content"][0]["text"] == "fallback"
        assert "X-Model-Fallback" in resp.headers


class TestEffortPass:
    def test_undeclared_route_is_untouched(self):
        from app.services.reasoning_effort import apply_to_anthropic_body

        body = {"output_config": {"effort": "extra-high"}}
        apply_to_anthropic_body(body, {"model_id": "x"})
        assert body == {"output_config": {"effort": "extra-high"}}

    def test_dropped_level_removes_empty_output_config_without_mutating_input(self):
        from app.services.reasoning_effort import apply_to_anthropic_body

        original = {"effort": "max"}
        body = {"output_config": original}
        apply_to_anthropic_body(body, {"reasoning_efforts": ["low"]})
        assert "output_config" not in body
        assert original == {"effort": "max"}

    def test_other_output_config_fields_kept(self):
        from app.services.reasoning_effort import apply_to_anthropic_body

        fmt = {"type": "json_schema", "schema": {"type": "object"}}
        body = {"output_config": {"effort": "max", "format": fmt}}
        apply_to_anthropic_body(body, {"reasoning_efforts": ["low"]})
        assert body == {"output_config": {"format": fmt}}


class TestConfigAndAdmin:
    def test_config_load_warns_on_non_claude_model(self):
        from app.core import config as cfg
        from app.core.logger import logger

        lines: list[str] = []
        sink_id = logger.add(lambda m: lines.append(m.record["message"]), level="WARNING")
        try:
            cfg._warn_bedrock_native_messages({
                "nova": {"model_id": "amazon.nova-pro-v1:0", "native_messages": True},
                "claude": {"model_id": "anthropic.claude-sonnet-4-6", "native_messages": True},
                "off": {"model_id": "amazon.nova-pro-v1:0", "native_messages": False},
            })
        finally:
            logger.remove(sink_id)
        assert len(lines) == 1
        assert "[bedrock_models.nova]" in lines[0]

    def _put(self, client, bedrock_models):
        cfg = client.get("/admin/api/config", headers=web_auth_header(scopes=["admin"])).json()
        return client.put(
            "/admin/api/config",
            json={**cfg, "bedrock_models": bedrock_models},
            headers=web_auth_header(scopes=["admin"]),
        )

    def test_admin_rejects_native_on_non_claude_model(self, client):
        models = {
            "bedrock-nova": {**TEST_BEDROCK_MODELS["bedrock-nova"], "native_messages": True},
        }
        save = MagicMock()
        with patch("app.routers.admin.save_config", save):
            resp = self._put(client, models)
        assert resp.status_code == 400
        assert "native_messages needs a Claude model_id" in resp.json()["detail"]
        save.assert_not_called()

    def test_admin_accepts_native_on_claude_model(self, client):
        models = {"bedrock-native": dict(NATIVE_ENTRY)}
        with patch("app.routers.admin.save_config") as save:
            resp = self._put(client, models)
        assert resp.status_code == 200, resp.text
        assert save.call_args.args[5]["bedrock-native"]["native_messages"] is True
