"""Request bodies must not reach the gateway log as prose.

`summarize_body` keeps the schema of a body (what a 400 is actually
diagnosed from) and drops the content. These tests pin both halves: the
shape survives, the text does not.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from app.services.redact import summarize_body

SECRET = "my customer's national id is A123456789 and the deploy key is hunter2"


def _chat_body() -> dict:
    return {
        "model": "azure-gpt-4",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant. " + SECRET},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": SECRET},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            },
        ],
        "stream": True,
        "max_tokens": 4096,
        "tools": [{"type": "function", "function": {"name": "Bash", "description": SECRET}}],
    }


class TestContentIsDropped:
    def test_no_message_text_survives(self):
        out = summarize_body(_chat_body())
        assert SECRET not in out
        assert "A123456789" not in out
        assert "hunter2" not in out

    def test_strings_become_lengths(self):
        out = json.loads(summarize_body({"messages": [{"role": "user", "content": "abcde"}]}))
        assert out["messages"][0]["content"] == "<str:5>"

    def test_anthropic_system_and_thinking_blocks(self):
        body = {
            "system": [{"type": "text", "text": SECRET}],
            "messages": [
                {"role": "assistant", "content": [{"type": "thinking", "thinking": SECRET}]}
            ],
        }
        assert SECRET not in summarize_body(body)

    def test_converse_shape(self):
        body = {"messages": [{"role": "user", "content": [{"text": SECRET}]}]}
        assert SECRET not in summarize_body(body)

    def test_responses_instructions_are_not_prose(self):
        """`instructions` is the hoisted system prompt — a plain string."""
        assert SECRET not in summarize_body({"instructions": SECRET, "input": []})


class TestShapeIsKept:
    def test_schema_fields_survive(self):
        out = json.loads(summarize_body(_chat_body()))
        assert out["model"] == "azure-gpt-4"
        assert out["stream"] is True
        assert out["max_tokens"] == 4096
        assert [m["role"] for m in out["messages"]] == ["system", "user"]
        assert [b["type"] for b in out["messages"][1]["content"]] == ["text", "image_url"]
        assert out["tools"][0]["function"]["name"] == "Bash"

    def test_long_value_on_a_safe_key_is_still_summarised(self):
        """A client stuffing prose into `name` must not get a free pass."""
        out = json.loads(summarize_body({"name": "x" * 500}))
        assert out["name"] == "<str:500>"

    def test_long_lists_are_truncated_with_a_count(self):
        out = summarize_body({"messages": [{"role": "user", "content": "hi"} for _ in range(30)]})
        assert "<+10 more>" in out

    def test_output_is_capped(self):
        body = {"messages": [{"role": "user", "content": "x"} for _ in range(20)]}
        out = summarize_body(body, limit=50)
        assert len(out) < 120
        assert "chars)" in out


class TestEdgeCases:
    def test_non_dict_bodies_do_not_raise(self):
        assert summarize_body("just a string") == '"<str:13>"'
        assert summarize_body(None) == "null"
        assert summarize_body([1, 2, 3]) == "[1, 2, 3]"

    def test_unserialisable_body_never_raises(self):
        class Weird:
            pass

        out = summarize_body({"x": Weird()})
        assert "Weird" in out
        assert SECRET not in out

    def test_bytes_report_length_only(self):
        assert summarize_body({"blob": b"abcdef"}) == '{"blob": "<bytes:6>"}'


class TestEscapeHatch:
    def test_log_request_bodies_restores_raw_output(self):
        with patch.dict("os.environ", {"LOG_REQUEST_BODIES": "true"}):
            out = summarize_body({"messages": [{"role": "user", "content": SECRET}]})
        assert SECRET in out

    def test_off_by_default(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("LOG_REQUEST_BODIES", None)
            assert SECRET not in summarize_body({"a": SECRET})


class TestErrorLogSitesUseIt:
    """The wiring, not just the helper: what the log lines actually contain."""

    @staticmethod
    def _capture(fn) -> str:
        from app.core.logger import logger

        lines: list[str] = []
        sink_id = logger.add(lines.append, level="WARNING", format="{message}")
        try:
            fn()
        finally:
            logger.remove(sink_id)
        return "".join(lines)

    def _bodies(self) -> tuple[dict, dict]:
        incoming = {"model": "m", "messages": [{"role": "user", "content": SECRET}]}
        sent = {"model": "deployment", "input": [{"role": "user", "content": SECRET}]}
        return incoming, sent

    def test_azure_error_log_carries_shape_not_prose(self):
        from app.services.azure_proxy import _log_azure_error

        incoming, sent = self._bodies()
        out = self._capture(
            lambda: _log_azure_error(incoming, sent, "bad request", 400, "gpt-4", "/azure/v1/chat")
        )
        assert "Azure returned 400" in out
        assert SECRET not in out
        assert "incoming_shape=" in out and "<str:" in out

    def test_bedrock_error_log_carries_shape_not_prose(self):
        from app.services.bedrock_proxy import _log_bedrock_error

        incoming, sent = self._bodies()
        out = self._capture(
            lambda: _log_bedrock_error(incoming, sent, "ValidationException", 400, "claude", "/aws/v1/chat")
        )
        assert "Bedrock returned 400" in out
        assert SECRET not in out
        assert "incoming_shape=" in out and "<str:" in out
