"""Downstream error log lines on the Azure and Bedrock paths must say WHO,
WHICH MODEL, WHICH ENDPOINT and WHAT KIND of error.

They used to be ``logger.error("Azure messages downstream error: {}", exc)``
— and httpx's timeout exceptions stringify to an empty message, so during an
Azure slowdown the journal filled with ``Azure messages downstream error:``
followed by nothing: no model, no user, not even the exception type. Every
such line now carries ``user= model= endpoint= error=<Type>: <msg>``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.core.logger import logger
from tests.conftest import auth_header


@pytest.fixture()
def errors():
    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(m.record["message"]), level="ERROR")
    yield lines
    logger.remove(sink_id)


def _line(lines, prefix):
    hits = [line for line in lines if line.startswith(prefix)]
    assert len(hits) == 1, (prefix, lines)
    return hits[0]


class TestAzure:
    def test_non_stream_messages_timeout(self, client, errors):
        client.__httpx_mock__.post = AsyncMock(side_effect=httpx.ReadTimeout(""))
        resp = client.post(
            "/azure/v1/messages",
            json={"model": "azure-gpt-4", "max_tokens": 16,
                  "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 502
        line = _line(errors, "Azure messages downstream error |")
        for field in ("user=testuser", "model=azure-gpt-4",
                      "endpoint=/azure/v1/messages", "error=ReadTimeout:"):
            assert field in line, line

    def test_stream_connect_failure(self, client, errors):
        mock = client.__httpx_mock__
        mock.build_request = MagicMock(return_value=httpx.Request(
            "POST", "https://test.openai.azure.com/openai/v1/responses"))
        mock.send = AsyncMock(side_effect=httpx.ConnectTimeout(""))
        resp = client.post(
            "/azure/v1/chat/completions",
            json={"model": "azure-gpt-4", "stream": True,
                  "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 502
        line = _line(errors, "Azure stream connect error |")
        for field in ("user=testuser", "model=azure-gpt-4",
                      "endpoint=/azure/v1/chat/completions", "error=ConnectTimeout:"):
            assert field in line, line


class TestBedrock:
    def test_non_stream_chat_timeout(self, client, errors):
        client.__httpx_mock__.post = AsyncMock(side_effect=httpx.ReadTimeout(""))
        resp = client.post(
            "/aws/v1/chat/completions",
            json={"model": "bedrock-claude",
                  "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 502
        line = _line(errors, "Bedrock downstream error |")
        for field in ("user=testuser", "model=bedrock-claude",
                      "endpoint=/aws/v1/chat/completions", "error=ReadTimeout:"):
            assert field in line, line

    def test_messages_stream_connect_failure(self, client, errors):
        mock = client.__httpx_mock__
        mock.build_request = MagicMock(return_value=None)
        mock.send = AsyncMock(side_effect=httpx.ConnectError("refused"))
        resp = client.post(
            "/aws/v1/messages",
            json={"model": "bedrock-claude", "stream": True, "max_tokens": 16,
                  "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 502
        line = _line(errors, "Bedrock messages stream connect error |")
        for field in ("user=testuser", "model=bedrock-claude",
                      "endpoint=/aws/v1/messages", "error=ConnectError: refused"):
            assert field in line, line
