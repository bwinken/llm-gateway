"""Tests for the unified `/v1/*` dispatch (vLLM + Azure under one base URL).

`/v1/models`, `/v1/messages`, `/v1/chat/completions`, `/v1/responses`, and
`/v1/messages/count_tokens` route by alias: when the requested ``model`` is configured under
``[azure_models.*]`` AND the user has ``can_use_azure`` (or is admin), the
request goes to the Azure path. Otherwise it stays on the vLLM path —
Azure-only aliases requested by users without access quietly fall back
through ``_resolve_model`` to the configured vLLM default, matching the
gateway's "be liberal with unknown aliases" stance. Azure existence is
still hidden via ``GET /v1/models`` (no entries appear for users without
permission), but a client that forces the alias gets a friendly fallback,
not a 404.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tests.conftest import (
    TEST_AZURE_MODELS,
    TEST_MODEL_ROUTING,
    auth_header,
    make_httpx_response,
)


def _responses_payload(text: str = "ok", in_tk: int = 1, out_tk: int = 1) -> dict:
    """Minimal Azure Responses API non-stream body."""
    return {
        "id": "resp_test",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {
            "input_tokens": in_tk,
            "output_tokens": out_tk,
            "total_tokens": in_tk + out_tk,
        },
    }


def _fake_response(status_code: int, json_body: dict):
    class _Resp:
        def __init__(self, code, body):
            self.status_code = code
            self._body = body
            self.text = str(body)

        def json(self):
            return self._body

    return _Resp(status_code, json_body)


# ---------------------------------------------------------------------------
# /v1/models — per-user filter
# ---------------------------------------------------------------------------


class TestUnifiedModelsList:
    def test_azure_models_visible_when_can_use_azure(self, client, test_user):
        # test_user has can_use_azure=True by default in the conftest fixture.
        resp = client.get("/v1/models", headers=auth_header())
        assert resp.status_code == 200
        ids = {m["id"] for m in resp.json()["data"]}
        # vLLM aliases always present
        assert "test-llm" in ids
        assert "test-vlm" in ids
        # Azure llm alias merged in
        assert "azure-gpt-4" in ids

    def test_azure_models_hidden_when_no_permission(
        self, client, db_session, test_user
    ):
        test_user.can_use_azure = False
        db_session.add(test_user)
        db_session.commit()

        resp = client.get("/v1/models", headers=auth_header())
        assert resp.status_code == 200
        ids = {m["id"] for m in resp.json()["data"]}
        assert "test-llm" in ids
        # Azure entries must NOT appear
        assert "azure-gpt-4" not in ids
        # And no entry is marked as azure-owned
        owners = {m["owned_by"] for m in resp.json()["data"]}
        assert owners == {"llm-gateway"}

    def test_azure_embedding_excluded_from_chat_list(self, client, test_user):
        """The shared `/v1/models` endpoint serves chat clients; only llm/vlm
        Azure entries get merged in (mirrors the vLLM filter)."""
        resp = client.get("/v1/models", headers=auth_header())
        ids = {m["id"] for m in resp.json()["data"]}
        # azure-embed is type=embedding in the fixture
        assert "azure-embed" not in ids

    def test_azure_models_owned_by_label(self, client, test_user):
        resp = client.get("/v1/models", headers=auth_header())
        entries = {m["id"]: m for m in resp.json()["data"]}
        assert entries["test-llm"]["owned_by"] == "llm-gateway"
        assert entries["azure-gpt-4"]["owned_by"] == "azure-openai"


# ---------------------------------------------------------------------------
# /v1/chat/completions dispatch
# ---------------------------------------------------------------------------


class TestUnifiedChatCompletions:
    def test_azure_alias_dispatched_to_responses_api(self, client, test_user):
        """Azure-configured alias should hit the Azure Responses URL, not vLLM."""
        captured: dict = {}

        async def fake_post(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers", {})
            captured["json"] = kwargs.get("json", {})
            return _fake_response(200, _responses_payload("hi", 3, 2))

        client.__httpx_mock__.post = fake_post

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "azure-gpt-4",
                "messages": [{"role": "user", "content": "hello"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        # Azure URL fingerprint
        assert captured["url"].endswith("/openai/v1/responses")
        assert captured["headers"].get("api-key") == "azure-test-key"
        # body.model rewritten to deployment name
        assert captured["json"]["model"] == "gpt-4-deploy"

    def test_vllm_alias_still_goes_to_vllm(self, client, test_user):
        """A regular vLLM alias must NOT be hijacked by the Azure dispatcher."""
        captured: dict = {}

        async def fake_post(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers", {})
            return make_httpx_response(
                200,
                {
                    "id": "cmpl-1",
                    "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop", "index": 0}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        client.__httpx_mock__.post = fake_post

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-llm",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        # vLLM path uses Bearer auth and goes to the vLLM base_url, not Azure.
        assert "Bearer" in captured["headers"].get("Authorization", "")
        assert "/openai/v1/responses" not in captured["url"]
        assert "mock-llm" in captured["url"]

    def test_azure_alias_falls_back_to_vllm_without_permission(
        self, client, db_session, test_user
    ):
        """User without can_use_azure typing an Azure alias should NOT 404 —
        it should be treated like any other unknown alias on ``/v1/*`` and
        fall back through ``_resolve_model`` to the vLLM default. Preserves
        the gateway's longstanding liberal alias handling."""
        test_user.can_use_azure = False
        db_session.add(test_user)
        db_session.commit()

        captured: dict = {}

        async def fake_post(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers", {})
            return make_httpx_response(
                200,
                {
                    "id": "cmpl-1",
                    "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop", "index": 0}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        client.__httpx_mock__.post = fake_post

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "azure-gpt-4",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )
        # vLLM fallback served it. Fallback header surfaces the rewrite.
        assert resp.status_code == 200
        assert "/openai/v1/responses" not in captured["url"]
        assert "mock-llm" in captured["url"] or "mock-vlm" in captured["url"]
        assert resp.headers.get("X-Model-Fallback")


# ---------------------------------------------------------------------------
# /v1/responses dispatch
# ---------------------------------------------------------------------------


class TestUnifiedResponses:
    def test_azure_alias_dispatched_to_responses_api(self, client, test_user):
        """Azure-configured alias on /v1/responses must hit the Azure Responses
        pass-through, rewriting body.model to the deployment name."""
        captured: dict = {}

        async def fake_post(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers", {})
            captured["json"] = kwargs.get("json", {})
            return _fake_response(200, _responses_payload("hi", 3, 2))

        client.__httpx_mock__.post = fake_post

        resp = client.post(
            "/v1/responses",
            json={
                "model": "azure-gpt-4",
                "input": [{"role": "user", "content": "hello"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert captured["url"].endswith("/openai/v1/responses")
        assert captured["headers"].get("api-key") == "azure-test-key"
        # pure pass-through only rewrites the model alias → deployment name
        assert captured["json"]["model"] == "gpt-4-deploy"

    def test_vllm_alias_still_goes_to_vllm(self, client, test_user):
        """A regular vLLM alias must NOT be hijacked by the Azure dispatcher."""
        captured: dict = {}

        async def fake_post(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers", {})
            return make_httpx_response(
                200,
                {
                    "id": "resp-1",
                    "status": "completed",
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                },
            )

        client.__httpx_mock__.post = fake_post

        resp = client.post(
            "/v1/responses",
            json={"model": "test-llm", "input": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert "/openai/v1/responses" not in captured["url"]
        assert "mock-llm" in captured["url"]

    def test_azure_alias_falls_back_to_vllm_without_permission(
        self, client, db_session, test_user
    ):
        """Without can_use_azure an Azure alias on /v1/responses falls back to
        the vLLM default rather than 404ing — same stance as the other routes."""
        test_user.can_use_azure = False
        db_session.add(test_user)
        db_session.commit()

        captured: dict = {}

        async def fake_post(url, **kwargs):
            captured["url"] = url
            return make_httpx_response(
                200,
                {
                    "id": "resp-1",
                    "status": "completed",
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                },
            )

        client.__httpx_mock__.post = fake_post

        resp = client.post(
            "/v1/responses",
            json={"model": "azure-gpt-4", "input": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert "/openai/v1/responses" not in captured["url"]
        assert "mock-llm" in captured["url"] or "mock-vlm" in captured["url"]


# ---------------------------------------------------------------------------
# /v1/messages dispatch
# ---------------------------------------------------------------------------


class TestUnifiedMessages:
    def test_azure_alias_dispatched(self, client, test_user):
        captured: dict = {}

        async def fake_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json", {})
            return _fake_response(200, _responses_payload("ok", 1, 1))

        client.__httpx_mock__.post = fake_post

        resp = client.post(
            "/v1/messages",
            json={
                "model": "azure-gpt-4",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert captured["url"].endswith("/openai/v1/responses")

    def test_azure_alias_falls_back_to_vllm_without_permission(
        self, client, db_session, test_user
    ):
        """Same fallback behavior as /v1/chat/completions — Azure alias from
        a non-Azure user goes to vLLM's default rather than 404ing."""
        test_user.can_use_azure = False
        db_session.add(test_user)
        db_session.commit()

        captured: dict = {}

        async def fake_post(url, **kwargs):
            captured["url"] = url
            return make_httpx_response(
                200,
                {
                    "id": "cmpl-1",
                    "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop", "index": 0}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        client.__httpx_mock__.post = fake_post

        resp = client.post(
            "/v1/messages",
            json={
                "model": "azure-gpt-4",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert "/openai/v1/responses" not in captured["url"]

    def test_admin_bypasses_azure_permission(
        self, client, db_session, test_user
    ):
        """Admins should reach Azure on /v1/messages even with can_use_azure=False."""
        test_user.can_use_azure = False
        test_user.is_admin = True
        db_session.add(test_user)
        db_session.commit()

        async def fake_post(url, **kwargs):
            return _fake_response(200, _responses_payload("ok", 1, 1))

        client.__httpx_mock__.post = fake_post

        resp = client.post(
            "/v1/messages",
            json={
                "model": "azure-gpt-4",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /v1/messages/count_tokens dispatch
# ---------------------------------------------------------------------------


class TestUnifiedCountTokens:
    def test_azure_alias_uses_chars_estimate(self, client, test_user):
        """Azure has no tokenize endpoint, so the unified count_tokens
        dispatch must use the chars/4 estimate rather than reaching out
        to a downstream tokenizer."""
        # If dispatch incorrectly went to the vLLM path it would attempt
        # to call the downstream `/tokenize`. Wire a sentinel that would
        # blow up so we catch that regression.
        async def boom(*args, **kwargs):
            raise AssertionError("vLLM tokenize must not be called for Azure alias")

        client.__httpx_mock__.post = boom

        resp = client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "azure-gpt-4",
                "messages": [{"role": "user", "content": "hello world"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert "input_tokens" in resp.json()
        assert resp.json()["input_tokens"] > 0

    def test_azure_alias_falls_back_to_vllm_tokenize_without_permission(
        self, client, db_session, test_user
    ):
        """Without can_use_azure the call goes down the vLLM path, which
        forwards to the downstream tokenizer endpoint (and falls back to a
        chars/4 estimate if that's unreachable). Either is a 200 with
        ``input_tokens`` — what we must NOT do is 404."""
        test_user.can_use_azure = False
        db_session.add(test_user)
        db_session.commit()

        # vLLM tokenize downstream — return a plausible token count.
        async def fake_post(url, **kwargs):
            return make_httpx_response(200, {"count": 3, "tokens": [1, 2, 3]})

        client.__httpx_mock__.post = fake_post

        resp = client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "azure-gpt-4",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert "input_tokens" in resp.json()


# ---------------------------------------------------------------------------
# /v1-prefix-optional aliases — every endpoint also accepts the bare path
# ---------------------------------------------------------------------------


class TestNoV1PrefixAliases:
    """Clients whose base URL omits ``/v1`` should still reach the same
    handler. Anthropic-shaped routes already had this alias; the rest
    (chat/completions, models, embeddings, rerank/score) were added so a
    Roo Code / Cline / Cursor base URL like ``https://gw.example.com``
    works the same as ``https://gw.example.com/v1``.
    """

    def test_models_without_v1(self, client, test_user):
        r1 = client.get("/v1/models", headers=auth_header())
        r2 = client.get("/models", headers=auth_header())
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json() == r2.json()

    def test_chat_completions_without_v1(self, client, test_user):
        async def fake_post(url, **kwargs):
            return make_httpx_response(
                200,
                {
                    "id": "cmpl-1",
                    "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop", "index": 0}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        client.__httpx_mock__.post = fake_post

        resp = client.post(
            "/chat/completions",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "hi"

    def test_embeddings_without_v1(self, client, test_user):
        async def fake_post(url, **kwargs):
            return make_httpx_response(
                200,
                {
                    "data": [{"embedding": [0.1, 0.2], "index": 0}],
                    "usage": {"prompt_tokens": 3, "total_tokens": 3},
                },
            )

        client.__httpx_mock__.post = fake_post

        resp = client.post(
            "/embeddings",
            json={"model": "test-embedding", "input": "hello"},
            headers=auth_header(),
        )
        assert resp.status_code == 200

    def test_rerank_without_v1(self, client, test_user):
        async def fake_post(url, **kwargs):
            return make_httpx_response(
                200,
                {"results": [{"index": 0, "relevance_score": 0.99}]},
            )

        client.__httpx_mock__.post = fake_post

        resp = client.post(
            "/rerank",
            json={
                "model": "test-reranker",
                "query": "q",
                "documents": ["a"],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200

    def test_score_without_v1(self, client, test_user):
        async def fake_post(url, **kwargs):
            return make_httpx_response(
                200,
                {"results": [{"index": 0, "relevance_score": 0.99}]},
            )

        client.__httpx_mock__.post = fake_post

        resp = client.post(
            "/score",
            json={
                "model": "test-reranker",
                "query": "q",
                "documents": ["a"],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Startup duplicate-alias guard
# ---------------------------------------------------------------------------


class TestDuplicateAliasGuard:
    def test_collision_raises_at_config_build(self):
        """Same alias under [models.<type>] AND [azure_models.*] must fail
        config load — operators have to disambiguate."""
        from app.core.config import _build_config

        raw = {
            "models": {
                "llm": {
                    "claude-opus": {
                        "base_url": "http://vllm:8000/v1",
                        "real_model": "real",
                        "api_key": "k",
                    }
                }
            },
            "azure_models": {
                "claude-opus": {
                    "type": "llm",
                    "endpoint": "https://x.openai.azure.com",
                    "deployment": "d",
                    "api_key": "k",
                }
            },
        }
        with pytest.raises(ValueError, match="claude-opus"):
            _build_config(raw)

    def test_no_collision_loads_cleanly(self):
        from app.core.config import _build_config

        raw = {
            "models": {
                "llm": {
                    "vllm-only": {
                        "base_url": "http://vllm:8000/v1",
                        "real_model": "real",
                        "api_key": "k",
                    }
                }
            },
            "azure_models": {
                "azure-only": {
                    "type": "llm",
                    "endpoint": "https://x.openai.azure.com",
                    "deployment": "d",
                    "api_key": "k",
                }
            },
        }
        # No exception, both maps populated.
        _, routing, _, _, azure, _ = _build_config(raw)
        assert "vllm-only" in routing
        assert "azure-only" in azure
