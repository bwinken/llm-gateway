"""
Tests for vision embedding (Qwen3-VL-Embedding-2B style).

The /v1/embeddings endpoint accepts chat-style messages with image content
when the model type is vision_embedding.
Reference: vllm/examples/pooling/embed/vision_embedding_online.py
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


_SAMPLE_IMAGE_URL = "https://upload.wikimedia.org/wikipedia/commons/thumb/4/47/PNG_transparency_demonstration_1.png/300px-PNG_transparency_demonstration_1.png"

_SAMPLE_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


class TestVisionEmbedding:
    """Tests for /v1/embeddings with vision_embedding model type."""

    def test_text_only_input(self, client, test_user):
        """Vision embedding model should still handle plain text input."""
        downstream_body = {
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
            "model": "Qwen/Qwen3-VL-Embedding-2B",
            "usage": {"prompt_tokens": 8, "total_tokens": 8},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/embeddings",
            json={
                "model": "test-vision-embedding",
                "input": [{"role": "user", "content": "Represent the query: What is deep learning?"}],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        assert len(data["data"]) == 1
        assert len(data["data"][0]["embedding"]) == 3

    def test_image_url_input(self, client, test_user):
        """Chat-style messages with image_url content type."""
        downstream_body = {
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.5, 0.6, 0.7]}],
            "model": "Qwen/Qwen3-VL-Embedding-2B",
            "usage": {"prompt_tokens": 50, "total_tokens": 50},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/embeddings",
            json={
                "model": "test-vision-embedding",
                "input": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": _SAMPLE_IMAGE_URL}},
                        {"type": "text", "text": "Represent the image."},
                    ],
                }],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]) == 1

    def test_base64_image_input(self, client, test_user):
        """Chat-style messages with base64-encoded image."""
        downstream_body = {
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.8, 0.9]}],
            "model": "Qwen/Qwen3-VL-Embedding-2B",
            "usage": {"prompt_tokens": 40, "total_tokens": 40},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/embeddings",
            json={
                "model": "test-vision-embedding",
                "input": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{_SAMPLE_BASE64}"},
                        },
                        {"type": "text", "text": "Describe this image."},
                    ],
                }],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"][0]["embedding"]) == 2

    def test_batch_multimodal_input(self, client, test_user):
        """Multiple messages (batch) with mixed text and image."""
        downstream_body = {
            "object": "list",
            "data": [
                {"object": "embedding", "index": 0, "embedding": [0.1, 0.2]},
                {"object": "embedding", "index": 1, "embedding": [0.3, 0.4]},
            ],
            "model": "Qwen/Qwen3-VL-Embedding-2B",
            "usage": {"prompt_tokens": 60, "total_tokens": 60},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/embeddings",
            json={
                "model": "test-vision-embedding",
                "input": [
                    {"role": "user", "content": "A text-only query"},
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": _SAMPLE_IMAGE_URL}},
                            {"type": "text", "text": "An image query"},
                        ],
                    },
                ],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]) == 2

    def test_model_swap(self, client, test_user):
        """Model alias should be swapped to real_model for downstream."""
        captured = {}

        async def capture_post(*args, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return make_httpx_response(200, {
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
                "model": "Qwen/Qwen3-VL-Embedding-2B",
                "usage": {"prompt_tokens": 5, "total_tokens": 5},
            })

        mock = client.__httpx_mock__
        mock.post = capture_post

        resp = client.post(
            "/v1/embeddings",
            json={
                "model": "test-vision-embedding",
                "input": [{"role": "user", "content": "test"}],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert captured["json"]["model"] == "Qwen/Qwen3-VL-Embedding-2B"

    def test_downstream_url(self, client, test_user):
        """Request should be forwarded to the vision_embedding server's /embeddings path."""
        captured = {}

        async def capture_post(url, **kwargs):
            captured["url"] = str(url)
            return make_httpx_response(200, {
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
                "model": "Qwen/Qwen3-VL-Embedding-2B",
                "usage": {"prompt_tokens": 5, "total_tokens": 5},
            })

        mock = client.__httpx_mock__
        mock.post = capture_post

        resp = client.post(
            "/v1/embeddings",
            json={
                "model": "test-vision-embedding",
                "input": [{"role": "user", "content": "test"}],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert captured["url"] == "http://mock-vembed:8090/v1/embeddings"

    def test_downstream_error_502(self, client, test_user):
        mock = client.__httpx_mock__
        mock.post = _make_post_coro_raise(Exception("connection refused"))

        resp = client.post(
            "/v1/embeddings",
            json={
                "model": "test-vision-embedding",
                "input": [{"role": "user", "content": "test"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 502

    def test_downstream_non_200(self, client, test_user):
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(500, {"error": "internal"}))

        resp = client.post(
            "/v1/embeddings",
            json={
                "model": "test-vision-embedding",
                "input": [{"role": "user", "content": "test"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 500

    def test_401_without_auth(self, client):
        resp = client.post(
            "/v1/embeddings",
            json={
                "model": "test-vision-embedding",
                "input": [{"role": "user", "content": "test"}],
            },
        )
        assert resp.status_code in (401, 403)
