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
from decimal import Decimal
from types import SimpleNamespace

from app.services.observability import (
    GenerationRecord,
    StreamingChatOutput,
    build_scores,
    classify_client,
    get_sample_rate,
    record_generation,
    reset_langfuse_cache,
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

    def test_error_scores(self):
        scores = build_scores(
            empty_turn=False, fallback_used=False, client="claude-code",
            output_tokens=0, is_error=True,
        )
        by = self._by_name(scores)
        assert by["request_error"]["value"] == "true"
        assert by["request_error"]["data_type"] == "CATEGORICAL"
        assert by["client"]["value"] == "claude-code"
        # per-response signals are omitted for errors
        assert "empty_turn" not in by and "output_tokens" not in by


class TestLogError:
    """Failed requests must produce a Langfuse error generation (no DB write)."""

    def test_emits_error_generation(self):
        import app.services.vllm_proxy as vp

        captured = {}
        with patch("app.services.vllm_proxy.get_langfuse", return_value=MagicMock()), \
             patch("app.services.vllm_proxy.record_generation", lambda rec: captured.update(rec=rec)):
            vp._log_error(
                SimpleNamespace(id=7, username="bob", display_name="Bob"),
                {"messages": []}, "upstream boom", 502, "qwen", "/v1/messages", "llm",
            )
        rec = captured["rec"]
        assert rec.is_error is True
        assert rec.username == "bob"
        assert "502" in rec.error and "boom" in rec.error
        assert rec.output_tokens == 0

    def test_noop_when_langfuse_unconfigured(self):
        import app.services.vllm_proxy as vp

        with patch("app.services.vllm_proxy.get_langfuse", return_value=None), \
             patch("app.services.vllm_proxy.record_generation") as rg:
            vp._log_error(
                SimpleNamespace(id=7, username="bob", display_name="Bob"),
                {}, "boom", 500, "qwen", "/v1/messages", "llm",
            )
        rg.assert_not_called()


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

    def test_latency_stamps_span_duration(self):
        # With a measured latency the span END is extended so its duration
        # (Langfuse "Latency") equals the latency; metadata carries the raw ms.
        import time

        client = MagicMock()
        gen = client.start_observation.return_value
        with patch("app.services.observability.get_langfuse", return_value=client), \
             patch("langfuse.propagate_attributes", lambda **kw: contextlib.nullcontext()):
            before = time.time_ns()
            record_generation(_make_record(latency_ms=1500.0))
            after = time.time_ns()

        end_time = gen.end.call_args.kwargs["end_time"]
        # end ≈ now + 1500ms; the span start is ≈ now, so duration ≈ 1.5s.
        assert before + 1_500_000_000 <= end_time <= after + 1_500_000_000
        assert client.start_observation.call_args.kwargs["metadata"]["latency_ms"] == 1500.0

    def test_no_latency_plain_end(self):
        # Without a measured latency the span is ended plainly (no end_time) and
        # no latency_ms leaks into metadata.
        client = MagicMock()
        gen = client.start_observation.return_value
        with patch("app.services.observability.get_langfuse", return_value=client), \
             patch("langfuse.propagate_attributes", lambda **kw: contextlib.nullcontext()):
            record_generation(_make_record(latency_ms=None))
        assert gen.end.call_args.kwargs == {}
        assert "latency_ms" not in client.start_observation.call_args.kwargs["metadata"]


class TestStreamingChatOutput:
    """Reassembles streamed OpenAI deltas into one assistant message so a
    tool-call-only turn is captured (the empty-output regression guard)."""

    def test_text_only(self):
        acc = StreamingChatOutput()
        for piece in ("Hel", "lo ", "world"):
            acc.add_delta({"content": piece})
        assert acc.as_message() == {"role": "assistant", "content": "Hello world"}

    def test_tool_call_only_turn_is_captured(self):
        # The exact case that used to produce an empty Langfuse output: no text,
        # only a streamed tool call (id/name once, arguments in fragments).
        acc = StreamingChatOutput()
        acc.add_delta({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                       "function": {"name": "get_weather", "arguments": ""}}]})
        acc.add_delta({"tool_calls": [{"index": 0, "function": {"arguments": '{"city":'}}]})
        acc.add_delta({"tool_calls": [{"index": 0, "function": {"arguments": '"SF"}'}}]})
        msg = acc.as_message()
        assert msg["role"] == "assistant"
        assert msg["content"] is None
        assert msg["tool_calls"] == [
            {"id": "call_1", "type": "function",
             "function": {"name": "get_weather", "arguments": '{"city":"SF"}'}}
        ]

    def test_parallel_tool_calls_kept_in_index_order(self):
        acc = StreamingChatOutput()
        acc.add_delta({"tool_calls": [{"index": 1, "id": "b", "function": {"name": "two", "arguments": "{}"}}]})
        acc.add_delta({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "one", "arguments": "{}"}}]})
        ids = [tc["id"] for tc in acc.as_message()["tool_calls"]]
        assert ids == ["a", "b"]

    def test_reasoning_and_text(self):
        acc = StreamingChatOutput()
        acc.add_delta({"reasoning_content": "think..."})
        acc.add_delta({"content": "answer"})
        msg = acc.as_message()
        assert msg["reasoning_content"] == "think..."
        assert msg["content"] == "answer"

    def test_nothing_captured_returns_none(self):
        # Empty / role-only deltas → None, so an output is omitted only when the
        # turn truly produced nothing.
        acc = StreamingChatOutput()
        acc.add_delta({"role": "assistant"})
        acc.add_delta({})
        acc.add_delta(None)
        assert acc.as_message() is None


class TestRequestLatency:
    """request_latency_ms() reads the request-start marker set by the middleware."""

    def test_returns_elapsed_after_request_start(self):
        from app.services.observability import request_latency_ms, set_request_meta

        set_request_meta(user_agent="claude-cli", x_app=None, session_id=None)
        ms = request_latency_ms()
        assert ms is not None and ms >= 0.0

    def test_none_when_no_start_marker(self):
        from app.services import observability as obs

        # A meta dict without the start marker (e.g. a path that bypassed the
        # middleware) → no latency rather than a crash.
        token = obs._request_meta.set({"user_agent": None})
        try:
            assert obs.request_latency_ms() is None
        finally:
            obs._request_meta.reset(token)


class TestEmitObservationIO:
    """Phase 2: _emit_observation attaches input/output ONLY when capture is on."""

    def _emit(self, *, capture, output_payload, input_payload):
        import app.services.vllm_proxy as vp
        from app.services import observability as obs

        captured = {}
        obs.set_request_meta(user_agent="claude-cli", x_app=None, session_id=None)
        if input_payload is not None:
            obs.set_io_input(input_payload)
        zero = Decimal("0")
        with patch("app.services.vllm_proxy.get_langfuse", return_value=MagicMock()), \
             patch("app.services.vllm_proxy.record_generation", lambda rec: captured.update(rec=rec)), \
             patch("app.services.vllm_proxy.capture_io_enabled", return_value=capture):
            vp._emit_observation(
                SimpleNamespace(id=42, username="alice", display_name="Alice"),
                "qwen", "llm", 100, 5, "/v1/messages",
                {"real_model": "r"}, 0, "vllm",
                {"input": zero, "output": zero, "cache_read_input_tokens": zero, "total": zero},
                output_payload=output_payload,
            )
        return captured["rec"]

    def test_io_captured_when_enabled(self):
        rec = self._emit(
            capture=True,
            output_payload="The answer is 42.",
            input_payload=[{"role": "user", "content": "hi"}],
        )
        assert rec.input_payload == [{"role": "user", "content": "hi"}]
        assert rec.output_payload == "The answer is 42."

    def test_io_omitted_when_disabled(self):
        rec = self._emit(
            capture=False,
            output_payload="The answer is 42.",
            input_payload=[{"role": "user", "content": "hi"}],
        )
        assert rec.input_payload is None
        assert rec.output_payload is None

    def test_latency_threaded_from_request_meta(self):
        # set_request_meta (called in _emit) stamps the request-start marker, so
        # the emitted record carries a measured latency regardless of capture.
        rec = self._emit(capture=False, output_payload=None, input_payload=None)
        assert rec.latency_ms is not None and rec.latency_ms >= 0.0


class TestRequestMetaMiddleware:
    """Header capture MUST live in the pure-ASGI middleware: a contextvar set
    in the sync get_current_user dependency runs in a threadpool copy and is
    lost at the _log_usage seam (regression guard for that bug)."""

    def test_sets_meta_from_scope_headers_visible_to_inner_app(self):
        import asyncio

        from app.services.observability import RequestMetaMiddleware, get_request_meta

        seen: dict = {}

        async def inner(scope, receive, send):
            # Same task as the middleware → contextvar must be visible here.
            seen.update(get_request_meta())

        mw = RequestMetaMiddleware(inner)
        scope = {
            "type": "http",
            "headers": [
                (b"user-agent", b"claude-cli/1.0 (external, cli)"),
                (b"x-app", b"cli"),
                (b"x-session-id", b"sess-123"),
            ],
        }

        async def receive():
            return {"type": "http.request"}

        async def send(msg):
            pass

        asyncio.run(mw(scope, receive, send))
        assert seen.get("user_agent") == "claude-cli/1.0 (external, cli)"
        assert seen.get("x_app") == "cli"
        assert seen.get("session_id") == "sess-123"

    def test_non_http_scope_is_passthrough(self):
        import asyncio

        from app.services.observability import RequestMetaMiddleware

        called = {}

        async def inner(scope, receive, send):
            called["ok"] = True

        mw = RequestMetaMiddleware(inner)

        async def receive():
            return {}

        async def send(msg):
            pass

        asyncio.run(mw({"type": "lifespan"}, receive, send))
        assert called.get("ok") is True


class TestSampleRate:
    """LANGFUSE_SAMPLE_RATE — fraction of requests recorded (0.0–1.0)."""

    def setup_method(self):
        reset_langfuse_cache()

    def teardown_method(self):
        reset_langfuse_cache()

    def test_default_is_record_everything(self, monkeypatch):
        monkeypatch.delenv("LANGFUSE_SAMPLE_RATE", raising=False)
        assert get_sample_rate() == 1.0

    def test_parses_valid_value(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_SAMPLE_RATE", "0.25")
        assert get_sample_rate() == 0.25

    def test_invalid_value_falls_back_to_one(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_SAMPLE_RATE", "banana")
        assert get_sample_rate() == 1.0

    def test_out_of_range_is_clamped(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_SAMPLE_RATE", "1.5")
        assert get_sample_rate() == 1.0
        reset_langfuse_cache()
        monkeypatch.setenv("LANGFUSE_SAMPLE_RATE", "-0.3")
        assert get_sample_rate() == 0.0

    def test_cached_until_reset(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_SAMPLE_RATE", "0.5")
        assert get_sample_rate() == 0.5
        monkeypatch.setenv("LANGFUSE_SAMPLE_RATE", "0.9")
        assert get_sample_rate() == 0.5  # still the cached parse
        reset_langfuse_cache()
        assert get_sample_rate() == 0.9

    def test_rate_zero_records_nothing(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_SAMPLE_RATE", "0.0")
        client = MagicMock()
        with patch("app.services.observability.get_langfuse", return_value=client):
            record_generation(_make_record())
        client.start_observation.assert_not_called()

    def test_rate_one_always_records(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_SAMPLE_RATE", "1.0")
        client = MagicMock()
        with patch("app.services.observability.get_langfuse", return_value=client), \
             patch("langfuse.propagate_attributes", lambda **kw: contextlib.nullcontext()), \
             patch("app.services.observability.random.random", side_effect=AssertionError("RNG must not be consulted at rate 1.0")):
            record_generation(_make_record())
        client.start_observation.assert_called_once()

    def test_fractional_rate_follows_rng(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_SAMPLE_RATE", "0.4")
        client = MagicMock()
        with patch("app.services.observability.get_langfuse", return_value=client), \
             patch("langfuse.propagate_attributes", lambda **kw: contextlib.nullcontext()):
            # draw below the rate -> recorded
            with patch("app.services.observability.random.random", return_value=0.39):
                record_generation(_make_record())
            assert client.start_observation.call_count == 1
            # draw at/above the rate -> skipped
            with patch("app.services.observability.random.random", return_value=0.4):
                record_generation(_make_record())
            assert client.start_observation.call_count == 1
