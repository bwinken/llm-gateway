"""Every streaming pre-flight (`client.send(req, stream=True)`) must carry a
bounded timeout on all three backends.

Background: the SSE pump's `_SSE_MAX_IDLE` guard and its client pings only
start once response headers have arrived. The wait for those headers used to
be unbounded — Azure/Bedrock built the request with `timeout=None`, vLLM with
`read=None` — so a downstream that accepted a multi-MB upload and then never
answered parked the coroutine forever: no ping, no log, no 502, the pool
connection held, and the service unable to stop on SIGTERM. Time-to-headers
is now capped at `_NON_STREAM_TIMEOUT`, deliberately under nginx's 300s
`proxy_read_timeout` so the gateway answers first.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx

from app.services import vllm_proxy
from tests.conftest import (
    FakeBedrockStreamResponse,
    FakeStreamResponse,
    auth_header,
)
from tests.test_aws_api import _stream_events


class TestStreamTimeoutValue:
    def test_every_phase_is_bounded(self):
        t = vllm_proxy._STREAM_TIMEOUT
        assert t.connect is not None
        assert t.write is not None
        assert t.pool is not None
        assert t.read is not None, "time-to-headers must have a ceiling"

    def test_headers_ceiling_matches_non_stream_and_beats_nginx(self):
        assert vllm_proxy._STREAM_TIMEOUT.read == vllm_proxy._NON_STREAM_TIMEOUT
        assert vllm_proxy._STREAM_TIMEOUT.read < 300.0  # nginx proxy_read_timeout


def _timeout_passed_to_build_request(mock: MagicMock) -> httpx.Timeout:
    assert mock.build_request.called, "stream path never built a request"
    kwargs = mock.build_request.call_args.kwargs
    assert "timeout" in kwargs, "stream request built without an explicit timeout"
    return kwargs["timeout"]


def _assert_bounded(timeout) -> None:
    assert isinstance(timeout, httpx.Timeout), timeout
    assert timeout.read is not None, "pre-flight wait for headers is unbounded"
    assert timeout.read == vllm_proxy._NON_STREAM_TIMEOUT
    assert timeout.connect is not None and timeout.pool is not None


_AZURE_SSE = [
    'data: {"type":"response.output_text.delta","delta":"Hi"}',
    'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":5,"output_tokens":2,"total_tokens":7}}}',
]
_OPENAI_SSE = [
    'data: {"id":"s1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"Hi"},"finish_reason":null}]}',
    'data: {"id":"s1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}',
    "data: [DONE]",
]


class TestAzurePreflightIsBounded:
    def _arm(self, client):
        mock = client.__httpx_mock__
        # Assign fresh mocks rather than configuring the fixture's: another
        # module replaces `build_request` with a bare lambda on the shared
        # mock, and test order must not decide whether `.called` exists.
        mock.build_request = MagicMock(return_value=httpx.Request(
            "POST", "https://test.openai.azure.com/openai/v1/responses",
        ))
        mock.send = AsyncMock(return_value=FakeStreamResponse(_AZURE_SSE))
        return mock

    def test_chat_completions(self, client):
        mock = self._arm(client)
        resp = client.post(
            "/azure/v1/chat/completions",
            json={"model": "azure-gpt-4", "stream": True,
                  "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        _assert_bounded(_timeout_passed_to_build_request(mock))

    def test_messages(self, client):
        mock = self._arm(client)
        resp = client.post(
            "/azure/v1/messages",
            json={"model": "azure-gpt-4", "stream": True, "max_tokens": 32,
                  "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        _assert_bounded(_timeout_passed_to_build_request(mock))

    def test_responses(self, client):
        mock = self._arm(client)
        resp = client.post(
            "/azure/v1/responses",
            json={"model": "azure-gpt-4", "stream": True, "input": "hi"},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        _assert_bounded(_timeout_passed_to_build_request(mock))


class TestBedrockPreflightIsBounded:
    def _arm(self, client):
        mock = client.__httpx_mock__
        mock.send = AsyncMock(
            return_value=FakeBedrockStreamResponse(_stream_events(["hi"])),
        )
        mock.build_request = MagicMock(return_value=None)
        return mock

    def test_chat_completions(self, client):
        mock = self._arm(client)
        resp = client.post(
            "/aws/v1/chat/completions",
            json={"model": "bedrock-claude", "stream": True,
                  "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        _assert_bounded(_timeout_passed_to_build_request(mock))

    def test_messages(self, client):
        mock = self._arm(client)
        resp = client.post(
            "/aws/v1/messages",
            json={"model": "bedrock-claude", "stream": True, "max_tokens": 32,
                  "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        _assert_bounded(_timeout_passed_to_build_request(mock))


class TestVllmPreflightIsBounded:
    def test_chat_completions(self, client, test_user):
        mock = client.__httpx_mock__
        mock.build_request = MagicMock(return_value=httpx.Request(
            "POST", "http://mock-llm:8000/v1/chat/completions",
        ))
        mock.send = AsyncMock(return_value=FakeStreamResponse(_OPENAI_SSE))
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test-llm", "stream": True,
                  "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        _assert_bounded(_timeout_passed_to_build_request(mock))
