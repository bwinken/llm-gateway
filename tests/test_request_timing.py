"""Per-request phase timing (observability.request_timing) and its Langfuse
emission: where a slow request's wall time went.

The send / headers marks come from httpx event hooks on the user-traffic
clients, so these tests use real ``httpx.AsyncClient``s over a
``MockTransport`` (the shared mock client of conftest has no hooks).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from app.services import observability as obs
from tests.conftest import auth_header


def _hooked_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), event_hooks=obs.UPSTREAM_EVENT_HOOKS,
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestRequestTiming:
    def test_phases_from_marks(self):
        obs.set_request_meta()
        meta = obs.get_request_meta()
        t0 = meta["start_monotonic"]
        meta.update(
            upstream_first_sent=t0 + 0.010, upstream_sent=t0 + 0.010,
            upstream_attempts=1, upstream_headers=t0 + 0.510,
            first_output=time.monotonic() - 1.0,
            upstream_host="x.openai.azure.com", upstream_request_id="req-1",
        )
        t = obs.request_timing(output_tokens=100)
        assert t["gateway_overhead_ms"] == 10.0
        assert t["upstream_ttfb_ms"] == 500.0
        assert t["upstream_attempts"] == 1
        assert "retry_ms" not in t
        assert t["generation_ms"] >= 1000.0
        assert 50.0 <= t["output_tps"] <= 100.0
        assert t["upstream_host"] == "x.openai.azure.com"
        assert t["upstream_request_id"] == "req-1"

    def test_retry_and_unanswered_last_attempt(self):
        obs.set_request_meta()
        meta = obs.get_request_meta()
        t0 = meta["start_monotonic"]
        # Attempt 1 answered (429), attempt 2 never got headers.
        meta.update(
            upstream_first_sent=t0 + 0.01, upstream_headers=t0 + 0.2,
            upstream_sent=t0 + 0.3, upstream_attempts=2,
        )
        t = obs.request_timing()
        assert t["upstream_attempts"] == 2
        assert t["retry_ms"] == 290.0
        assert "upstream_ttfb_ms" not in t

    def test_nothing_observed_yields_nothing(self):
        obs.set_request_meta()
        assert obs.request_timing() == {}

    def test_tiny_generation_has_no_rate(self):
        obs.set_request_meta()
        obs.get_request_meta()["first_output"] = time.monotonic()
        t = obs.request_timing(output_tokens=5)
        assert "ttft_ms" in t and "output_tps" not in t

    def test_outside_a_request_is_empty_and_never_mutates_the_default(self):
        default = {}
        token = obs._request_meta.set(default)
        try:
            obs.mark_first_output()
            _run(_hooked_client(lambda r: httpx.Response(200)).get("http://h/"))
            assert obs.request_timing() == {}
            assert default == {}
        finally:
            obs._request_meta.reset(token)


class TestUpstreamHooks:
    def test_marks_each_attempt(self):
        async def go():
            obs.set_request_meta()
            client = _hooked_client(
                lambda r: httpx.Response(200, headers={"apim-request-id": "abc"}),
            )
            await client.post("https://res.example.com/openai/v1/responses", json={})
            await client.post("https://u:secret@res.example.com:8443/openai/v1/responses", json={})
            return obs.request_timing()

        t = _run(go())
        assert t["upstream_attempts"] == 2
        assert t["upstream_host"] == "res.example.com:8443"
        assert t["upstream_request_id"] == "abc"
        assert "upstream_ttfb_ms" in t and "retry_ms" in t

    def test_request_id_header_preference_and_reset(self):
        async def go(headers):
            obs.set_request_meta()
            await _hooked_client(lambda r: httpx.Response(200, headers=headers)).get("http://h/")
            return obs.request_timing().get("upstream_request_id")

        assert _run(go({"x-amzn-requestid": "aws-1", "x-request-id": "oai-1"})) == "oai-1"
        assert _run(go({"x-amzn-requestid": "aws-1"})) == "aws-1"
        assert _run(go({})) is None


class TestOutputDetection:
    def test_sse_lines(self):
        f = obs.is_output_sse_line
        assert f('data: {"choices":[{"delta":{"content":"hi"}}]}')
        assert f('data: {"type":"response.output_text.delta","delta":"x"}')
        assert f('data: {"type":"content_block_start"}')
        assert not f('data: {"type":"response.created"}')
        assert not f('data: {"type":"response.in_progress"}')
        assert not f('data: {"type":"message_start"}')
        assert not f("data: [DONE]")
        assert not f("event: message_start")
        assert not f(": ping")
        assert not f("")
        assert not f("data: not json")

    def test_event_types(self):
        assert not obs.is_output_event_type("messageStart")
        assert obs.is_output_event_type("contentBlockDelta")


class TestLangfuseEmission:
    def _record(self, **kw):
        from tests.test_observability import _make_record

        client = MagicMock()
        with patch("app.services.observability.get_langfuse", return_value=client), \
             patch("langfuse.propagate_attributes", lambda **k: contextlib.nullcontext()):
            before = time.time_ns()
            obs.record_generation(_make_record(**kw))
        return client, before

    def test_timing_in_metadata_completion_start_and_scores(self):
        timing = {"gateway_overhead_ms": 12.0, "upstream_ttfb_ms": 800.0,
                  "ttft_ms": 900.0, "generation_ms": 2000.0, "output_tps": 50.0,
                  "upstream_attempts": 1}
        client, before = self._record(latency_ms=2900.0, timing=timing)
        kwargs = client.start_observation.call_args.kwargs
        for k, v in timing.items():
            assert kwargs["metadata"][k] == v
        cst_ns = kwargs["completion_start_time"].timestamp() * 1e9
        end_ns = client.start_observation.return_value.end.call_args.kwargs["end_time"]
        # completion start sits ttft after the span start, end latency after it.
        assert abs((end_ns - cst_ns) - 2000.0 * 1e6) < 1e6
        assert cst_ns >= before + 900.0 * 1e6 - 1e6
        scores = {c.kwargs["name"]: c.kwargs for c in
                  client.start_observation.return_value.score.call_args_list}
        for name in ("gateway_overhead_ms", "upstream_ttfb_ms", "output_tps"):
            assert scores[name]["value"] == timing[name]
            assert scores[name]["data_type"] == "NUMERIC"
        assert "ttft_ms" not in scores

    def test_no_timing_no_completion_start(self):
        client, _ = self._record(latency_ms=100.0)
        kwargs = client.start_observation.call_args.kwargs
        assert kwargs["completion_start_time"] is None
        names = {c.kwargs["name"] for c in client.start_observation.return_value.score.call_args_list}
        assert names == {"empty_turn", "fallback_used", "client", "output_tokens"}


async def _sse_body(lines, delay=0.05):
    for i, line in enumerate(lines):
        if i == 1:
            await asyncio.sleep(delay)  # the "prefill": headers first, output later
        yield (line + "\n\n").encode()


class TestThroughTheProxies:
    """End to end: middleware → proxy → hooked client → pump → _log_usage."""

    def _capture(self, client, path, body, upstream_client, getter):
        from app.services.observability import RequestMetaMiddleware

        captured = []
        app = RequestMetaMiddleware(client.app)
        with patch(getter, return_value=upstream_client), \
             patch("app.services.vllm_proxy.get_langfuse", return_value=MagicMock()), \
             patch("app.services.vllm_proxy.record_generation", captured.append), \
             TestClient(app) as tc:
            resp = tc.post(path, json=body, headers=auth_header())
            resp.read()
        assert resp.status_code == 200, resp.text
        assert captured, "no generation recorded"
        return captured[-1]

    def test_vllm_stream(self, client, test_user):
        chunks = [
            'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"he"}}]}',
            'data: {"choices":[{"index":0,"delta":{"content":"llo"},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":5,"completion_tokens":2}}',
            "data: [DONE]",
        ]

        def handler(request):
            return httpx.Response(200, headers={"x-request-id": "vllm-1"},
                                  content=_sse_body(["", *chunks]))

        rec = self._capture(
            client, "/v1/chat/completions",
            {"model": "test-llm", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            _hooked_client(handler), "app.services.vllm_proxy.get_client",
        )
        t = rec.timing
        assert t["upstream_host"] == "mock-llm:8000"
        assert t["upstream_request_id"] == "vllm-1"
        assert t["upstream_attempts"] == 1
        assert t["gateway_overhead_ms"] >= 0
        assert t["ttft_ms"] >= t["gateway_overhead_ms"] + t["upstream_ttfb_ms"] + 40

    def test_bedrock_stream(self, client, test_user):
        from app.services.aws_eventstream import encode_event
        from tests.test_aws_api import _stream_events

        frames = [
            encode_event({":message-type": "event", ":event-type": et,
                          ":content-type": "application/json"}, json.dumps(p).encode())
            for et, p in _stream_events(["he", "llo"])
        ]

        async def body():
            yield frames[0]  # messageStart: not output
            await asyncio.sleep(0.05)
            for f in frames[1:]:
                yield f

        def handler(request):
            return httpx.Response(200, headers={"x-amzn-requestid": "aws-1"}, content=body())

        rec = self._capture(
            client, "/aws/v1/chat/completions",
            {"model": "bedrock-claude", "stream": True,
             "messages": [{"role": "user", "content": "hi"}]},
            _hooked_client(handler), "app.services.bedrock_proxy.get_bedrock_client",
        )
        t = rec.timing
        assert t["upstream_request_id"] == "aws-1"
        assert t["upstream_host"].startswith("bedrock-runtime.us-east-1")
        assert t["ttft_ms"] >= t["gateway_overhead_ms"] + t["upstream_ttfb_ms"] + 40

    def test_azure_stream_skips_lifecycle_events(self, client, test_user):
        events = [
            {"type": "response.created", "response": {"status": "in_progress"}},
            {"type": "response.output_text.delta", "delta": "hi"},
            {"type": "response.completed",
             "response": {"status": "completed", "usage": {"input_tokens": 5, "output_tokens": 2}}},
        ]

        def handler(request):
            return httpx.Response(200, headers={"apim-request-id": "az-1"},
                                  content=_sse_body([f"data: {json.dumps(e)}" for e in events]))

        rec = self._capture(
            client, "/azure/v1/chat/completions",
            {"model": "azure-gpt-4", "stream": True,
             "messages": [{"role": "user", "content": "hi"}]},
            _hooked_client(handler), "app.services.azure_proxy.get_azure_client",
        )
        t = rec.timing
        assert t["upstream_host"] == "test.openai.azure.com"
        assert t["upstream_request_id"] == "az-1"
        # response.created arrived with the headers; output came 50 ms later.
        assert t["ttft_ms"] >= t["gateway_overhead_ms"] + t["upstream_ttfb_ms"] + 40

    def test_non_stream_has_no_ttft(self, client, test_user):
        def handler(request):
            return httpx.Response(200, json={
                "id": "c1",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8},
            })

        rec = self._capture(
            client, "/v1/chat/completions",
            {"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
            _hooked_client(handler), "app.services.vllm_proxy.get_client",
        )
        assert "upstream_ttfb_ms" in rec.timing
        assert "ttft_ms" not in rec.timing


class TestClientWiring:
    def test_user_traffic_clients_carry_the_hooks_and_health_does_not(self, monkeypatch):
        from app.core import server_state

        monkeypatch.setenv("AZURE_HTTP_PROXY", "http://proxy.example:3128")
        monkeypatch.setenv("BEDROCK_INSECURE", "true")

        async def go():
            await server_state.init_client()
            try:
                for c in (server_state._client, server_state._azure_client,
                          server_state._bedrock_client):
                    assert c.event_hooks["request"] == obs.UPSTREAM_EVENT_HOOKS["request"]
                    assert c.event_hooks["response"] == obs.UPSTREAM_EVENT_HOOKS["response"]
                assert server_state._health_client.event_hooks == {"request": [], "response": []}
            finally:
                await server_state.close_client()

        _run(go())

    def test_cloud_clients_are_dedicated_and_keep_idle_connections(self, monkeypatch):
        """Even without a proxy, Azure and Bedrock get their own clients whose
        idle connections outlive httpx's 5 s default: a pause between agent
        turns must not cost a fresh TLS handshake to the cloud. The shared
        (vLLM) client keeps the default, below uvicorn's keep-alive."""
        from app.core import server_state

        for name in ("AZURE_HTTP_PROXY", "AZURE_INSECURE", "BEDROCK_HTTP_PROXY", "BEDROCK_INSECURE"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("CLOUD_KEEPALIVE_EXPIRY_S", "45")

        async def go():
            await server_state.init_client()
            try:
                pools = {
                    name: getattr(server_state, name)._transport._pool
                    for name in ("_client", "_azure_client", "_bedrock_client")
                }
                assert server_state._azure_client is not server_state._client
                assert server_state._bedrock_client is not server_state._client
                assert pools["_azure_client"]._keepalive_expiry == 45.0
                assert pools["_bedrock_client"]._keepalive_expiry == 45.0
                assert pools["_client"]._keepalive_expiry == 5.0
            finally:
                await server_state.close_client()

        _run(go())
