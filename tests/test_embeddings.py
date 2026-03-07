"""
Tests for POST /v1/embeddings

Covers: single input, batch input, model routing, usage logging, error handling.
"""

from __future__ import annotations

from tests.conftest import auth_header, make_httpx_response


def _make_post_coro(response):
    async def _post(*args, **kwargs):
        return response
    return _post


def _make_post_coro_raise(exc):
    async def _post(*args, **kwargs):
        raise exc
    return _post


class TestEmbeddings:

    def test_single_input(self, client, test_user):
        downstream_body = {
            "object": "list",
            "data": [
                {
                    "object": "embedding",
                    "index": 0,
                    "embedding": [0.1, 0.2, 0.3, 0.4, 0.5],
                }
            ],
            "model": "BAAI/bge-m3",
            "usage": {"prompt_tokens": 5, "total_tokens": 5},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/embeddings",
            json={"model": "test-embedding", "input": "Hello world"},
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        assert len(data["data"]) == 1
        assert len(data["data"][0]["embedding"]) == 5

    def test_batch_input(self, client, test_user):
        downstream_body = {
            "object": "list",
            "data": [
                {"object": "embedding", "index": 0, "embedding": [0.1, 0.2]},
                {"object": "embedding", "index": 1, "embedding": [0.3, 0.4]},
                {"object": "embedding", "index": 2, "embedding": [0.5, 0.6]},
            ],
            "model": "BAAI/bge-m3",
            "usage": {"prompt_tokens": 15, "total_tokens": 15},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/embeddings",
            json={
                "model": "test-embedding",
                "input": ["Hello", "World", "Test"],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]) == 3
        assert data["data"][2]["index"] == 2

    def test_model_swap(self, client, test_user):
        """Model alias should be swapped to real_model for downstream."""
        captured = {}

        async def capture_post(*args, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return make_httpx_response(200, {
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
                "model": "BAAI/bge-m3",
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
            })

        mock = client.__httpx_mock__
        mock.post = capture_post

        resp = client.post(
            "/v1/embeddings",
            json={"model": "test-embedding", "input": "test"},
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert captured["json"]["model"] == "BAAI/bge-m3"

    def test_401_without_auth(self, client):
        resp = client.post(
            "/v1/embeddings",
            json={"model": "test-embedding", "input": "test"},
        )
        assert resp.status_code in (401, 403)

    def test_downstream_error_502(self, client, test_user):
        mock = client.__httpx_mock__
        mock.post = _make_post_coro_raise(Exception("timeout"))

        resp = client.post(
            "/v1/embeddings",
            json={"model": "test-embedding", "input": "test"},
            headers=auth_header(),
        )
        assert resp.status_code == 502

    def test_downstream_non_200(self, client, test_user):
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(
            make_httpx_response(500, {"error": "internal"})
        )

        resp = client.post(
            "/v1/embeddings",
            json={"model": "test-embedding", "input": "test"},
            headers=auth_header(),
        )
        assert resp.status_code == 500
