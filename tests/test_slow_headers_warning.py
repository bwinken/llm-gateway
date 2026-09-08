"""``Slow response headers`` WARNING — emitted the moment a streaming
pre-flight gets headers back, when that took at least SLOW_REQUEST_WARN_S.

``Slow request`` (test_slow_request_warning.py) fires only when a request
finishes; a downstream that is slow to even *answer* is invisible until then.
This line names the phase — everything before the first byte of the response
— so a slow backend shows up in the file log while it is happening.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.logger import logger
from tests.conftest import FakeBedrockStreamResponse, FakeStreamResponse, auth_header
from tests.test_aws_api import _stream_events

_AZURE_SSE = [
    'data: {"type":"response.output_text.delta","delta":"Hi"}',
    'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":5,"output_tokens":2,"total_tokens":7}}}',
]
_OPENAI_SSE = [
    'data: {"id":"s1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"Hi"},"finish_reason":null}]}',
    'data: {"id":"s1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}',
    "data: [DONE]",
]


@pytest.fixture()
def warnings():
    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(m.record["message"]), level="WARNING")
    yield lines
    logger.remove(sink_id)


def _headers_lines(lines):
    return [line for line in lines if line.startswith("Slow response headers")]


def _arm_azure(client):
    mock = client.__httpx_mock__
    mock.build_request = MagicMock(return_value=httpx.Request(
        "POST", "https://test.openai.azure.com/openai/v1/responses"))
    mock.send = AsyncMock(return_value=FakeStreamResponse(_AZURE_SSE))


def _post_azure_stream(client):
    return client.post(
        "/azure/v1/chat/completions",
        json={"model": "azure-gpt-4", "stream": True,
              "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(),
    )


class TestAzure:
    def test_slow_headers_logged_with_phase_and_fields(self, client, warnings, monkeypatch):
        monkeypatch.setenv("SLOW_REQUEST_WARN_S", "60")
        _arm_azure(client)
        with patch("app.services.vllm_proxy.request_latency_ms", return_value=130_000.0):
            resp = _post_azure_stream(client)
        assert resp.status_code == 200

        lines = _headers_lines(warnings)
        assert len(lines) == 1
        for field in ("user=testuser", "model=azure-gpt-4",
                      "endpoint=/azure/v1/chat/completions", "backend=azure",
                      "waited=130.0s", "threshold=60s"):
            assert field in lines[0], lines[0]
        # The end-of-request line still fires too; together they say "slow
        # to answer" rather than "slow to generate".
        assert any(line.startswith("Slow request") for line in warnings)

    def test_fast_headers_are_silent(self, client, warnings, monkeypatch):
        monkeypatch.delenv("SLOW_REQUEST_WARN_S", raising=False)
        _arm_azure(client)
        resp = _post_azure_stream(client)
        assert resp.status_code == 200
        assert _headers_lines(warnings) == []

    def test_zero_disables(self, client, warnings, monkeypatch):
        monkeypatch.setenv("SLOW_REQUEST_WARN_S", "0")
        _arm_azure(client)
        with patch("app.services.vllm_proxy.request_latency_ms", return_value=999_000.0):
            resp = _post_azure_stream(client)
        assert resp.status_code == 200
        assert _headers_lines(warnings) == []


class TestBedrock:
    def test_messages_stream(self, client, warnings, monkeypatch):
        monkeypatch.setenv("SLOW_REQUEST_WARN_S", "60")
        mock = client.__httpx_mock__
        mock.build_request = MagicMock(return_value=None)
        mock.send = AsyncMock(return_value=FakeBedrockStreamResponse(_stream_events(["hi"])))
        with patch("app.services.vllm_proxy.request_latency_ms", return_value=61_000.0):
            resp = client.post(
                "/aws/v1/messages",
                json={"model": "bedrock-claude", "stream": True, "max_tokens": 32,
                      "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )
        assert resp.status_code == 200
        lines = _headers_lines(warnings)
        assert len(lines) == 1
        for field in ("model=bedrock-claude", "endpoint=/aws/v1/messages", "backend=bedrock", "waited=61.0s"):
            assert field in lines[0], lines[0]


class TestVllm:
    def test_chat_stream(self, client, test_user, warnings, monkeypatch):
        monkeypatch.setenv("SLOW_REQUEST_WARN_S", "60")
        mock = client.__httpx_mock__
        mock.build_request = MagicMock(return_value=httpx.Request(
            "POST", "http://mock-llm:8000/v1/chat/completions"))
        mock.send = AsyncMock(return_value=FakeStreamResponse(_OPENAI_SSE))
        with patch("app.services.vllm_proxy.request_latency_ms", return_value=75_000.0):
            resp = client.post(
                "/v1/chat/completions",
                json={"model": "test-llm", "stream": True,
                      "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )
        assert resp.status_code == 200
        lines = _headers_lines(warnings)
        assert len(lines) == 1
        for field in ("model=test-llm", "endpoint=/v1/chat/completions", "backend=vllm", "waited=75.0s"):
            assert field in lines[0], lines[0]
