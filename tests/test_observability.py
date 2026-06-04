"""
Tests for app/services/observability.py — Langfuse integration.

The pure helpers (client classification, cost breakdown, payload redaction,
generation-kwargs mapping) are tested in isolation here; the Langfuse SDK is
never imported by these tests (the module must import cleanly without the
dependency, and the pure helpers must not touch the SDK).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import contextlib

from app.services.observability import (
    GenerationRecord,
    build_scores,
    classify_client,
    record_generation,
)


def _make_record(**overrides):
    base = dict(
        username="alice",
        user_id="42",
        endpoint="/v1/messages",
        backend="vllm",
        model_alias="qwen3.6-27b",
        real_model="real-qwen",
        model_type="llm",
        usage={"input": 1200, "output": 1},
        cost={"input": 0.0012, "output": 0.0, "total": 0.0012},
        output_tokens=1,
        empty_turn=True,
        fallback_reason=None,
        user_agent="claude-cli/1.0 (external, cli)",
    )
    base.update(overrides)
    return GenerationRecord(**base)


class TestClassifyClient:
    """Best-effort mapping of (User-Agent, endpoint, x-app) -> client label."""

    def test_claude_code_from_user_agent(self):
        assert classify_client("claude-cli/1.2.3 (external, cli)", "/v1/messages") == "claude-code"
        assert classify_client("claude-code/0.9", "/v1/messages") == "claude-code"

    def test_claude_code_from_x_app_header(self):
        # Even with an unrecognised UA, x-app:cli pins it to Claude Code.
        assert classify_client("python-httpx/0.27", "/v1/messages", x_app="cli") == "claude-code"

    def test_roo_code_from_user_agent(self):
        assert classify_client("Roo-Code/3.1 vscode", "/v1/chat/completions") == "roo-code"

    def test_anthropic_sdk_when_messages_endpoint_but_unknown_ua(self):
        # /v1/messages with no Claude marker => some Anthropic-style client.
        assert classify_client("OpenAI/Python 1.0", "/v1/messages") == "anthropic-sdk"

    def test_openai_compatible_for_openai_style_endpoints(self):
        assert classify_client("OpenAI/Python 1.0", "/v1/chat/completions") == "openai-compatible"
        assert classify_client(None, "/v1/responses") == "openai-compatible"
        assert classify_client(None, "/v1/embeddings") == "openai-compatible"

    def test_other_when_nothing_matches(self):
        assert classify_client(None, "/v1/unknown") == "other"

    def test_none_user_agent_does_not_crash(self):
        assert classify_client(None, "/v1/messages") == "anthropic-sdk"


class TestBuildScores:
    """Scores are the statisticable channel (scores-categorical / -numeric)."""

    def _by_name(self, scores):
        return {s["name"]: s for s in scores}

    def test_categorical_booleans_and_client(self):
        scores = build_scores(
            empty_turn=True, fallback_used=False, client="claude-code", output_tokens=42
        )
        by = self._by_name(scores)
        assert by["empty_turn"]["value"] == "true"
        assert by["empty_turn"]["data_type"] == "CATEGORICAL"
        assert by["fallback_used"]["value"] == "false"
        assert by["fallback_used"]["data_type"] == "CATEGORICAL"
        assert by["client"]["value"] == "claude-code"
        assert by["client"]["data_type"] == "CATEGORICAL"

    def test_output_tokens_is_numeric(self):
        by = self._by_name(build_scores(
            empty_turn=False, fallback_used=False, client="other", output_tokens=42
        ))
        assert by["output_tokens"]["value"] == 42
        assert by["output_tokens"]["data_type"] == "NUMERIC"

    def test_all_four_scores_present(self):
        scores = build_scores(
            empty_turn=False, fallback_used=True, client="roo-code", output_tokens=0
        )
        assert {s["name"] for s in scores} == {"empty_turn", "fallback_used", "client", "output_tokens"}


class TestRecordGeneration:
    """The two critical safety properties: no-op when unconfigured, never raises."""

    def test_noop_when_unconfigured(self):
        # get_langfuse() returns None when LANGFUSE_* unset → must do nothing, no raise.
        with patch("app.services.observability.get_langfuse", return_value=None):
            record_generation(_make_record())  # must not raise

    def test_swallows_client_errors(self):
        boom = MagicMock()
        boom.start_observation.side_effect = RuntimeError("langfuse down")
        with patch("app.services.observability.get_langfuse", return_value=boom), \
             patch("langfuse.propagate_attributes", lambda **kw: contextlib.nullcontext()):
            record_generation(_make_record())  # must not raise — error is swallowed

    def test_emits_generation_and_scores_when_enabled(self):
        client = MagicMock()
        gen = client.start_observation.return_value
        with patch("app.services.observability.get_langfuse", return_value=client), \
             patch("langfuse.propagate_attributes", lambda **kw: contextlib.nullcontext()):
            record_generation(_make_record(model_alias="qwen3.6-27b"))

        # generation carries model alias, usage and the gateway's cost breakdown
        kwargs = client.start_observation.call_args.kwargs
        assert kwargs["as_type"] == "generation"
        assert kwargs["model"] == "qwen3.6-27b"
        assert kwargs["usage_details"] == {"input": 1200, "output": 1}
        assert kwargs["cost_details"]["total"] == 0.0012
        # four scores attached (empty_turn / fallback_used / client / output_tokens)
        assert gen.score.call_count == 4
        score_names = {c.kwargs["name"] for c in gen.score.call_args_list}
        assert score_names == {"empty_turn", "fallback_used", "client", "output_tokens"}

    def test_client_score_value_reflects_user_agent(self):
        client = MagicMock()
        gen = client.start_observation.return_value
        with patch("app.services.observability.get_langfuse", return_value=client), \
             patch("langfuse.propagate_attributes", lambda **kw: contextlib.nullcontext()):
            record_generation(_make_record(user_agent="claude-cli/1.0", endpoint="/v1/messages"))
        client_score = next(c for c in gen.score.call_args_list if c.kwargs["name"] == "client")
        assert client_score.kwargs["value"] == "claude-code"
