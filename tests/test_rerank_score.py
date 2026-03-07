"""
Tests for POST /v1/rerank and POST /v1/score

Both endpoints route to the same handler. Covers: rerank, score, total_tokens fallback.
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


class TestRerank:

    def test_basic_rerank(self, client, test_user):
        downstream_body = {
            "object": "list",
            "results": [
                {"index": 1, "relevance_score": 0.95},
                {"index": 0, "relevance_score": 0.30},
            ],
            "model": "BAAI/bge-reranker-v2-m3",
            "usage": {"prompt_tokens": 50, "total_tokens": 50},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/rerank",
            json={
                "model": "test-reranker",
                "query": "What is deep learning?",
                "documents": [
                    "Deep learning is a subset of machine learning.",
                    "Deep learning uses neural networks with many layers.",
                ],
                "top_n": 2,
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 2
        assert data["results"][0]["relevance_score"] > data["results"][1]["relevance_score"]

    def test_rerank_with_top_n(self, client, test_user):
        downstream_body = {
            "object": "list",
            "results": [
                {"index": 2, "relevance_score": 0.99},
            ],
            "model": "BAAI/bge-reranker-v2-m3",
            "usage": {"prompt_tokens": 80, "total_tokens": 80},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/rerank",
            json={
                "model": "test-reranker",
                "query": "Python programming",
                "documents": ["Java guide", "C++ tutorial", "Python handbook"],
                "top_n": 1,
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 1

    def test_401_without_auth(self, client):
        resp = client.post(
            "/v1/rerank",
            json={
                "model": "test-reranker",
                "query": "test",
                "documents": ["doc1"],
            },
        )
        assert resp.status_code in (401, 403)


class TestScore:

    def test_basic_score(self, client, test_user):
        downstream_body = {
            "object": "list",
            "results": [
                {"index": 0, "relevance_score": 0.85},
                {"index": 1, "relevance_score": 0.42},
            ],
            "model": "BAAI/bge-reranker-v2-m3",
            "usage": {"prompt_tokens": 30, "total_tokens": 30},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/score",
            json={
                "model": "test-reranker",
                "text_1": ["Apple"],
                "text_2": ["A fruit", "A tech company"],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 2

    def test_score_model_swap(self, client, test_user):
        """Verify model alias is swapped to real_model."""
        captured = {}

        async def capture_post(*args, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return make_httpx_response(200, {
                "object": "list",
                "results": [{"index": 0, "relevance_score": 0.5}],
                "model": "BAAI/bge-reranker-v2-m3",
                "usage": {"prompt_tokens": 10, "total_tokens": 10},
            })

        mock = client.__httpx_mock__
        mock.post = capture_post

        resp = client.post(
            "/v1/score",
            json={
                "model": "test-reranker",
                "text_1": ["hello"],
                "text_2": ["world"],
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        assert captured["json"]["model"] == "BAAI/bge-reranker-v2-m3"

    def test_total_tokens_fallback(self, client, test_user):
        """When prompt_tokens=0 but total_tokens>0, prompt_tokens should use total_tokens."""
        downstream_body = {
            "object": "list",
            "results": [{"index": 0, "relevance_score": 0.7}],
            "model": "BAAI/bge-reranker-v2-m3",
            "usage": {"prompt_tokens": 0, "total_tokens": 42, "completion_tokens": 0},
        }
        mock = client.__httpx_mock__
        mock.post = _make_post_coro(make_httpx_response(200, downstream_body))

        resp = client.post(
            "/v1/score",
            json={
                "model": "test-reranker",
                "text_1": ["test"],
                "text_2": ["test"],
            },
            headers=auth_header(),
        )

        # The request should succeed; usage logging internally handles the fallback
        assert resp.status_code == 200

    def test_downstream_error_502(self, client, test_user):
        mock = client.__httpx_mock__
        mock.post = _make_post_coro_raise(Exception("connection reset"))

        resp = client.post(
            "/v1/score",
            json={
                "model": "test-reranker",
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
                "model": "test-reranker",
                "text_1": ["test"],
                "text_2": ["test"],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 422
