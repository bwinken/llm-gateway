"""
Tests for POST /v1/responses (pass-through endpoint).

Covers: non-streaming, streaming, model routing, error handling.
"""

from __future__ import annotations

import json

import httpx
from sqlmodel import select

from app.models.schema import UsageLog
from tests.conftest import FakeStreamResponse, auth_header, make_httpx_response


def _make_post_coro(response):
    async def _post(*args, **kwargs):
        return response
    return _post


def _make_content_post_coro(response):
    """For vllm_forward_responses which uses client.post(url, content=..., ...)."""
    async def _post(*args, **kwargs):
        return response
    return _post


def _make_post_coro_raise(exc):
    async def _post(*args, **kwargs):
        raise exc
    return _post


class TestResponsesNonStream:

    def test_basic_response(self, client, test_user):
        downstream_body = {
            "id": "resp-abc",
            "object": "response",
            "model": "real-llm-v1",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hello from responses!"}],
                }
            ],
            "usage": {"input_tokens": 20, "output_tokens": 8},
        }
        mock = client.__httpx_mock__
        mock.post = _make_content_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/responses",
            json={
                "model": "test-llm",
                "input": "Say hello",
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "resp-abc"
        assert data["output"][0]["content"][0]["text"] == "Hello from responses!"

    def test_model_swap(self, client, test_user):
        """Model alias should be replaced with real_model in the forwarded body."""
        captured = {}

        async def capture_post(*args, **kwargs):
            raw = kwargs.get("content", b"")
            captured["body"] = json.loads(raw) if raw else {}
            return make_httpx_response(200, {
                "id": "resp-swap",
                "object": "response",
                "model": "real-llm-v1",
                "output": [],
                "usage": {"input_tokens": 5, "output_tokens": 0},
            })

        mock = client.__httpx_mock__
        mock.post = capture_post

        resp = client.post(
            "/v1/responses",
            json={"model": "test-llm", "input": "test"},
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert captured["body"]["model"] == "real-llm-v1"

    def test_vlm_via_responses(self, client, test_user):
        """VLM models should also work through /v1/responses."""
        downstream_body = {
            "id": "resp-vlm",
            "object": "response",
            "model": "real-vlm-v1",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "I see an image."}],
                }
            ],
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }
        mock = client.__httpx_mock__
        mock.post = _make_content_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/responses",
            json={
                "model": "test-vlm",
                "input": [
                    {"type": "message", "role": "user", "content": [
                        {"type": "input_text", "text": "What is in this image?"},
                        {"type": "input_image", "image_url": "https://example.com/img.png"},
                    ]},
                ],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200

    def test_401_without_auth(self, client):
        resp = client.post(
            "/v1/responses",
            json={"model": "test-llm", "input": "test"},
        )
        assert resp.status_code in (401, 403)

    def test_invalid_json_400(self, client, test_user):
        resp = client.post(
            "/v1/responses",
            content=b"not valid json",
            headers={**auth_header(), "Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_downstream_error_502(self, client, test_user):
        mock = client.__httpx_mock__
        mock.post = _make_post_coro_raise(Exception("refused"))

        resp = client.post(
            "/v1/responses",
            json={"model": "test-llm", "input": "test"},
            headers=auth_header(),
        )
        assert resp.status_code == 502

    def test_downstream_non_200(self, client, test_user):
        mock = client.__httpx_mock__
        mock.post = _make_content_post_coro(
            make_httpx_response(500, {"error": "internal server error"})
        )

        resp = client.post(
            "/v1/responses",
            json={"model": "test-llm", "input": "test"},
            headers=auth_header(),
        )
        assert resp.status_code == 500


class TestResponsesStream:

    def test_stream_basic(self, client, test_user):
        sse_lines = [
            'data: {"type":"response.created","response":{"id":"resp-s1","object":"response","status":"in_progress"}}',
            'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"message","role":"assistant"}}',
            'data: {"type":"response.content_part.added","output_index":0,"content_index":0,"part":{"type":"output_text","text":""}}',
            'data: {"type":"response.output_text.delta","output_index":0,"content_index":0,"delta":"Hello!"}',
            'data: {"type":"response.completed","response":{"id":"resp-s1","usage":{"input_tokens":10,"output_tokens":5}}}',
            "data: [DONE]",
        ]
        fake_stream = FakeStreamResponse(sse_lines)
        mock = client.__httpx_mock__
        mock.build_request.return_value = httpx.Request("POST", "http://mock-llm:8000/v1/responses")
        mock.send.return_value = fake_stream

        resp = client.post(
            "/v1/responses",
            json={
                "model": "test-llm",
                "input": "Hi",
                "stream": True,
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        body = resp.text
        assert "Hello!" in body
        assert "response.completed" in body

    def test_stream_usage_logged(self, client, test_user, db_session):
        """Responses API stream nests usage inside `response.completed.response.usage`
        (not at the chunk top level like chat completions). Verify the
        pass-through pump extracts it so /v1/responses requests are billed
        instead of logged as 0/0 tokens — this is what Roo Code's "OpenAI"
        provider hits.
        """
        sse_lines = [
            'data: {"type":"response.created","response":{"id":"resp-u1","status":"in_progress"}}',
            'data: {"type":"response.output_text.delta","delta":"Hi"}',
            'data: {"type":"response.completed","response":{"id":"resp-u1","usage":{"input_tokens":42,"output_tokens":17,"input_tokens_details":{"cached_tokens":8}}}}',
            "data: [DONE]",
        ]
        fake_stream = FakeStreamResponse(sse_lines)
        mock = client.__httpx_mock__
        mock.build_request.return_value = httpx.Request("POST", "http://mock-llm:8000/v1/responses")
        mock.send.return_value = fake_stream

        resp = client.post(
            "/v1/responses",
            json={"model": "test-llm", "input": "Hi", "stream": True},
            headers=auth_header(),
        )
        # Drain the stream so the event_generator's finally-block runs
        # (which is where _log_usage gets called).
        _ = resp.text
        assert resp.status_code == 200

        rows = db_session.exec(
            select(UsageLog).where(UsageLog.user_id == test_user.id)
        ).all()
        assert len(rows) == 1
        assert rows[0].input_tokens == 42
        assert rows[0].output_tokens == 17
        assert rows[0].endpoint == "/responses"
