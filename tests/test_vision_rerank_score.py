"""
Tests for vision reranker (Qwen3-VL-Reranker-2B style).

The /v1/rerank and /v1/score endpoints accept multimodal documents
(text, image_url, base64 image) when the model type is vision_reranker.
References:
  - vllm/examples/pooling/score/vision_rerank_api_online.py
  - vllm/examples/pooling/score/vision_score_api_online.py
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


# ── /v1/rerank tests ──────────────────────────────────────────────────────


class TestVisionRerank:
    """Tests for /v1/rerank with vision_reranker model type."""

    def test_text_documents(self, client, test_user):
        """Basic text-only rerank with vision reranker model."""
        downstream_body = {
            "object": "list",
            "results": [
                {"index": 1, "relevance_score": 0.92},
                {"index": 0, "relevance_score": 0.15},
            ],
            "model": "Qwen/Qwen3-VL-Reranker-2B",
            "usage": {"prompt_tokens": 40, "total_tokens": 40},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/rerank",
            json={
                "model": "test-vision-reranker",
                "query": "A woman playing with her dog on a beach",
                "documents": [
                    "A man surfing in the ocean.",
                    "A woman and her golden retriever on a sandy beach at sunset.",
                ],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 2
        assert data["results"][0]["relevance_score"] > data["results"][1]["relevance_score"]

    def test_image_url_documents(self, client, test_user):
        """Rerank with image URL as document content."""
        downstream_body = {
            "object": "list",
            "results": [
                {"index": 0, "relevance_score": 0.88},
            ],
            "model": "Qwen/Qwen3-VL-Reranker-2B",
            "usage": {"prompt_tokens": 80, "total_tokens": 80},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/rerank",
            json={
                "model": "test-vision-reranker",
                "query": "A woman playing with her dog on a beach",
                "documents": [
                    {"type": "image_url", "image_url": {"url": _SAMPLE_IMAGE_URL}},
                ],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 1

    def test_base64_image_documents(self, client, test_user):
        """Rerank with base64-encoded image as document."""
        downstream_body = {
            "object": "list",
            "results": [
                {"index": 0, "relevance_score": 0.75},
            ],
            "model": "Qwen/Qwen3-VL-Reranker-2B",
            "usage": {"prompt_tokens": 60, "total_tokens": 60},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/rerank",
            json={
                "model": "test-vision-reranker",
                "query": "A woman playing with her dog on a beach",
                "documents": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{_SAMPLE_BASE64}"},
                    },
                ],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200

    def test_mixed_text_and_image_documents(self, client, test_user):
        """Rerank with mixed content: text + image in the same document list."""
        downstream_body = {
            "object": "list",
            "results": [
                {"index": 0, "relevance_score": 0.90},
                {"index": 1, "relevance_score": 0.70},
                {"index": 2, "relevance_score": 0.30},
            ],
            "model": "Qwen/Qwen3-VL-Reranker-2B",
            "usage": {"prompt_tokens": 100, "total_tokens": 100},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/rerank",
            json={
                "model": "test-vision-reranker",
                "query": "A woman playing with her dog on a beach",
                "documents": [
                    {"type": "image_url", "image_url": {"url": _SAMPLE_IMAGE_URL}},
                    "A woman and her golden retriever on a sandy beach.",
                    "A city skyline at night with bright lights.",
                ],
                "top_n": 3,
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 3

    def test_multimodal_document_content(self, client, test_user):
        """Rerank with multimodal document containing both text and image."""
        downstream_body = {
            "object": "list",
            "results": [
                {"index": 0, "relevance_score": 0.85},
            ],
            "model": "Qwen/Qwen3-VL-Reranker-2B",
            "usage": {"prompt_tokens": 90, "total_tokens": 90},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/rerank",
            json={
                "model": "test-vision-reranker",
                "query": "A woman playing with her dog on a beach",
                "documents": [
                    [
                        {"type": "text", "text": "A beach scene"},
                        {"type": "image_url", "image_url": {"url": _SAMPLE_IMAGE_URL}},
                    ],
                ],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200

    def test_model_swap(self, client, test_user):
        """Model alias should be swapped to real_model for downstream."""
        captured = {}

        async def capture_post(*args, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return make_httpx_response(200, {
                "object": "list",
                "results": [{"index": 0, "relevance_score": 0.5}],
                "model": "Qwen/Qwen3-VL-Reranker-2B",
                "usage": {"prompt_tokens": 10, "total_tokens": 10},
            })

        mock = client.__httpx_mock__
        mock.post = capture_post

        resp = client.post(
            "/v1/rerank",
            json={
                "model": "test-vision-reranker",
                "query": "test",
                "documents": ["doc1"],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert captured["json"]["model"] == "Qwen/Qwen3-VL-Reranker-2B"

    def test_401_without_auth(self, client):
        resp = client.post(
            "/v1/rerank",
            json={
                "model": "test-vision-reranker",
                "query": "test",
                "documents": ["doc1"],
            },
        )
        assert resp.status_code in (401, 403)

    def test_downstream_error_502(self, client, test_user):
        mock = client.__httpx_mock__
        mock.post = _make_post_coro_raise(Exception("connection refused"))

        resp = client.post(
            "/v1/rerank",
            json={
                "model": "test-vision-reranker",
                "query": "test",
                "documents": ["doc1"],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 502


# ── /v1/score tests ───────────────────────────────────────────────────────


class TestVisionScore:
    """Tests for /v1/score with vision_reranker model type."""

    def test_text_pair_score(self, client, test_user):
        """Score text pairs with vision reranker model."""
        downstream_body = {
            "object": "list",
            "results": [
                {"index": 0, "relevance_score": 0.82},
                {"index": 1, "relevance_score": 0.35},
            ],
            "model": "Qwen/Qwen3-VL-Reranker-2B",
            "usage": {"prompt_tokens": 20, "total_tokens": 20},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/score",
            json={
                "model": "test-vision-reranker",
                "text_1": "A woman playing with her dog on a beach",
                "text_2": [
                    "A woman and her golden retriever on a sandy beach.",
                    "A city skyline at night.",
                ],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 2

    def test_image_url_score(self, client, test_user):
        """Score with image URL as text_2 content."""
        downstream_body = {
            "object": "list",
            "results": [
                {"index": 0, "relevance_score": 0.78},
            ],
            "model": "Qwen/Qwen3-VL-Reranker-2B",
            "usage": {"prompt_tokens": 70, "total_tokens": 70},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/score",
            json={
                "model": "test-vision-reranker",
                "text_1": "A woman playing with her dog on a beach",
                "text_2": [
                    {"type": "image_url", "image_url": {"url": _SAMPLE_IMAGE_URL}},
                ],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 1

    def test_base64_image_score(self, client, test_user):
        """Score with base64-encoded image."""
        downstream_body = {
            "object": "list",
            "results": [
                {"index": 0, "relevance_score": 0.65},
            ],
            "model": "Qwen/Qwen3-VL-Reranker-2B",
            "usage": {"prompt_tokens": 55, "total_tokens": 55},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/score",
            json={
                "model": "test-vision-reranker",
                "text_1": "A woman playing with her dog on a beach",
                "text_2": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{_SAMPLE_BASE64}"},
                    },
                ],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200

    def test_mixed_multimodal_score(self, client, test_user):
        """Score with mixed text and image content in text_2."""
        downstream_body = {
            "object": "list",
            "results": [
                {"index": 0, "relevance_score": 0.90},
                {"index": 1, "relevance_score": 0.45},
            ],
            "model": "Qwen/Qwen3-VL-Reranker-2B",
            "usage": {"prompt_tokens": 85, "total_tokens": 85},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/score",
            json={
                "model": "test-vision-reranker",
                "text_1": "A woman playing with her dog on a beach",
                "text_2": [
                    "A woman and her golden retriever on a sandy beach.",
                    {"type": "image_url", "image_url": {"url": _SAMPLE_IMAGE_URL}},
                ],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 2

    def test_multimodal_content_block(self, client, test_user):
        """Score with multimodal content block (text + image combined)."""
        downstream_body = {
            "object": "list",
            "results": [
                {"index": 0, "relevance_score": 0.72},
            ],
            "model": "Qwen/Qwen3-VL-Reranker-2B",
            "usage": {"prompt_tokens": 95, "total_tokens": 95},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/score",
            json={
                "model": "test-vision-reranker",
                "text_1": "A woman playing with her dog on a beach",
                "text_2": [
                    [
                        {"type": "text", "text": "A beach scene"},
                        {"type": "image_url", "image_url": {"url": _SAMPLE_IMAGE_URL}},
                    ],
                ],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200

    def test_model_swap(self, client, test_user):
        """Model alias should be swapped to real_model."""
        captured = {}

        async def capture_post(*args, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return make_httpx_response(200, {
                "object": "list",
                "results": [{"index": 0, "relevance_score": 0.5}],
                "model": "Qwen/Qwen3-VL-Reranker-2B",
                "usage": {"prompt_tokens": 10, "total_tokens": 10},
            })

        mock = client.__httpx_mock__
        mock.post = capture_post

        resp = client.post(
            "/v1/score",
            json={
                "model": "test-vision-reranker",
                "text_1": ["hello"],
                "text_2": ["world"],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert captured["json"]["model"] == "Qwen/Qwen3-VL-Reranker-2B"

    def test_downstream_url(self, client, test_user):
        """Request should be forwarded to vision_reranker server's /score path."""
        captured = {}

        async def capture_post(url, **kwargs):
            captured["url"] = str(url)
            return make_httpx_response(200, {
                "object": "list",
                "results": [{"index": 0, "relevance_score": 0.5}],
                "model": "Qwen/Qwen3-VL-Reranker-2B",
                "usage": {"prompt_tokens": 10, "total_tokens": 10},
            })

        mock = client.__httpx_mock__
        mock.post = capture_post

        resp = client.post(
            "/v1/score",
            json={
                "model": "test-vision-reranker",
                "text_1": ["hello"],
                "text_2": ["world"],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert captured["url"] == "http://mock-vrerank:8091/v1/score"

    def test_total_tokens_fallback(self, client, test_user):
        """When prompt_tokens=0 but total_tokens>0, should use total_tokens."""
        downstream_body = {
            "object": "list",
            "results": [{"index": 0, "relevance_score": 0.7}],
            "model": "Qwen/Qwen3-VL-Reranker-2B",
            "usage": {"prompt_tokens": 0, "total_tokens": 55, "completion_tokens": 0},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/score",
            json={
                "model": "test-vision-reranker",
                "text_1": "test",
                "text_2": [
                    {"type": "image_url", "image_url": {"url": _SAMPLE_IMAGE_URL}},
                ],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200

    def test_downstream_error_502(self, client, test_user):
        mock = client.__httpx_mock__
        mock.post = _make_post_coro_raise(Exception("connection reset"))

        resp = client.post(
            "/v1/score",
            json={
                "model": "test-vision-reranker",
                "text_1": ["test"],
                "text_2": ["test"],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 502

    def test_downstream_non_200(self, client, test_user):
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(
            make_httpx_response(422, {"error": "invalid input"})
        )

        resp = client.post(
            "/v1/score",
            json={
                "model": "test-vision-reranker",
                "text_1": ["test"],
                "text_2": ["test"],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 422

    def test_401_without_auth(self, client):
        resp = client.post(
            "/v1/score",
            json={
                "model": "test-vision-reranker",
                "text_1": ["test"],
                "text_2": ["test"],
            },
        )
        assert resp.status_code in (401, 403)
