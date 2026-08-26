"""
Tests for POST /v1/chat/completions/render and /chat/completions/render.

The gateway forwards the body to the downstream vLLM
``/chat/completions/render`` endpoint (a debug aid: the request is rendered
through the model's chat template but never generated from), swapping the
model alias to its real_model on the way down and back to the alias on the
way up. Rendering is not billable — no row is written to ``usage_logs``.
"""

from __future__ import annotations

from unittest.mock import patch

from sqlmodel import Session, select

from app.models.schema import UsageLog
from tests.conftest import auth_header, make_httpx_response


def _make_post_coro(response):
    async def _post(*args, **kwargs):
        url = args[0] if args else kwargs.get("url")
        if url.endswith("/detokenize"):
            return make_httpx_response(200, {"prompt": "rendered text"})
        return response
    return _post


def _make_post_coro_capture(response, detokenized: str = "rendered text"):
    """Capture the *render* call while still answering the detokenize call that
    ``decode`` (on by default) makes afterwards. ``captured`` therefore always
    describes the render request, whichever calls follow it."""
    captured: dict = {}

    async def _post(*args, **kwargs):
        url = args[0] if args else kwargs.get("url")
        if url.endswith("/detokenize"):
            return make_httpx_response(200, {"prompt": detokenized})
        captured["url"] = url
        captured["body"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        return response

    return _post, captured


def _make_post_router(routes: dict, raises: dict | None = None):
    """Dispatch the mocked POST by URL suffix — the decode path makes two
    downstream calls (/chat/completions/render then /detokenize)."""
    calls: list[dict] = []

    async def _post(*args, **kwargs):
        url = args[0] if args else kwargs.get("url")
        calls.append({"url": url, "body": kwargs.get("json")})
        for suffix, exc in (raises or {}).items():
            if url.endswith(suffix):
                raise exc
        for suffix, response in routes.items():
            if url.endswith(suffix):
                return response
        raise AssertionError(f"unexpected downstream URL: {url}")

    return _post, calls


def _make_post_coro_raise(exc):
    async def _post(*args, **kwargs):
        raise exc
    return _post


class TestChatCompletionsRender:

    def test_basic_render(self, client, test_user, db_session: Session):
        downstream_body = {
            "request_id": "chatcmpl-abc",
            "token_ids": [1, 2, 3, 4],
            "model": "real-llm-v1",
            "sampling_params": {"temperature": 0.7, "max_tokens": 128},
            "stream": False,
        }
        post_coro, captured = _make_post_coro_capture(
            make_httpx_response(200, downstream_body)
        )
        client.__httpx_mock__.post = post_coro

        resp = client.post(
            "/v1/chat/completions/render",
            json={
                "model": "test-llm",
                "messages": [{"role": "user", "content": "hello world"}],
                "temperature": 0.7,
            },
            headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["token_ids"] == [1, 2, 3, 4]
        assert data["sampling_params"]["temperature"] == 0.7
        # real_model is swapped back to the user-facing alias
        assert data["model"] == "test-llm"
        # decode is on by default, so the readable prompt comes for free
        assert data["decoded_prompt"] == "rendered text"

        # Downstream got the real model and the verbatim body
        assert captured["body"]["model"] == "real-llm-v1"
        assert captured["body"]["messages"] == [
            {"role": "user", "content": "hello world"}
        ]
        assert captured["body"]["temperature"] == 0.7
        # Routed to the downstream's own /v1/chat/completions/render — the
        # render endpoint lives under vLLM's /v1 prefix (unlike /tokenize,
        # which sits at the server root), so the configured base_url is used
        # as-is with the suffix appended.
        assert captured["url"] == "http://mock-llm:8000/v1/chat/completions/render"

        # Rendering is a debug/metadata call — never billed
        rows = db_session.exec(
            select(UsageLog).where(UsageLog.user_id == test_user.id)
        ).all()
        assert rows == []

    def test_alias_without_v1_prefix(self, client, test_user):
        """``/chat/completions/render`` and ``/v1/chat/completions/render``
        are the same handler and hit the same downstream URL — the gateway's
        own prefix has no bearing on where the request lands."""
        post_coro, captured = _make_post_coro_capture(
            make_httpx_response(200, {"token_ids": [7, 8]})
        )
        client.__httpx_mock__.post = post_coro

        resp = client.post(
            "/chat/completions/render",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert resp.json()["token_ids"] == [7, 8]
        assert captured["url"] == "http://mock-llm:8000/v1/chat/completions/render"

    def test_reasoning_dialect_aligned(self, client, test_user):
        """Render must reflect what /chat/completions would actually send, so
        the reasoning / reasoning_content aliasing applies here too."""
        post_coro, captured = _make_post_coro_capture(
            make_httpx_response(200, {"token_ids": [1]})
        )
        client.__httpx_mock__.post = post_coro

        resp = client.post(
            "/v1/chat/completions/render",
            json={
                "model": "test-llm",
                "messages": [
                    {"role": "user", "content": "hi"},
                    {
                        "role": "assistant",
                        "content": "hello",
                        "reasoning_content": "thinking hard",
                    },
                ],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assistant = captured["body"]["messages"][1]
        assert assistant["reasoning"] == "thinking hard"
        assert assistant["reasoning_content"] == "thinking hard"

    def test_x_api_key(self, client, test_user):
        client.__httpx_mock__.post = _make_post_coro(
            make_httpx_response(200, {"token_ids": [1, 2]})
        )
        resp = client.post(
            "/v1/chat/completions/render",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
            headers={"x-api-key": "sk-testkey123"},
        )
        assert resp.status_code == 200

    def test_401_without_auth(self, client):
        resp = client.post(
            "/v1/chat/completions/render",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code in (401, 403)

    def test_invalid_json_body(self, client, test_user):
        resp = client.post(
            "/v1/chat/completions/render",
            content=b"not json",
            headers={**auth_header(), "Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_downstream_error_returns_502(self, client, test_user):
        client.__httpx_mock__.post = _make_post_coro_raise(
            Exception("connection refused")
        )
        resp = client.post(
            "/v1/chat/completions/render",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 502

    def test_downstream_404_propagated(self, client, test_user):
        """A vLLM too old to serve /chat/completions/render answers 404 — the
        gateway propagates it instead of pretending the endpoint worked."""
        client.__httpx_mock__.post = _make_post_coro(
            make_httpx_response(404, {"detail": "Not Found"})
        )
        resp = client.post(
            "/v1/chat/completions/render",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 404

    def test_unknown_model_falls_back(self, client, test_user):
        post_coro, captured = _make_post_coro_capture(
            make_httpx_response(200, {"token_ids": [1]})
        )
        client.__httpx_mock__.post = post_coro

        resp = client.post(
            "/v1/chat/completions/render",
            json={
                "model": "does-not-exist",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert "X-Model-Fallback" in resp.headers
        assert captured["body"]["model"] in ("real-llm-v1", "real-vlm-v1")

    def test_undocumented_fields_forwarded_verbatim(self, client, test_user):
        """The OpenAPI schema documents a handful of render-relevant fields,
        but the endpoint is a pass-through: anything else — vLLM extensions the
        schema never names — must reach the downstream untouched. This is the
        guard against someone later "tidying up" the docs by binding a Pydantic
        body model, which would validate these away silently."""
        post_coro, captured = _make_post_coro_capture(
            make_httpx_response(200, {"token_ids": [1]})
        )
        client.__httpx_mock__.post = post_coro

        resp = client.post(
            "/v1/chat/completions/render",
            json={
                "model": "test-llm",
                "messages": [{"role": "user", "content": "hi"}],
                "chat_template_kwargs": {"enable_thinking": False},
                "vllm_xargs": {"custom": "value"},
                "structured_outputs": {"json_object": True},
                "some_field_no_schema_knows_about": 42,
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert captured["body"]["chat_template_kwargs"] == {"enable_thinking": False}
        assert captured["body"]["vllm_xargs"] == {"custom": "value"}
        assert captured["body"]["structured_outputs"] == {"json_object": True}
        assert captured["body"]["some_field_no_schema_knows_about"] == 42

    def test_openapi_documents_the_request_body(self, client):
        """`/docs` must show a usable body for this endpoint — it exists to be
        poked at by hand. The handler takes a raw Request (so the pass-through
        keeps working), so the schema comes from `openapi_extra`."""
        spec = client.get("/openapi.json").json()
        for path in ("/v1/chat/completions/render", "/chat/completions/render"):
            op = spec["paths"][path]["post"]
            schema = op["requestBody"]["content"]["application/json"]["schema"]
            assert schema["required"] == ["model", "messages"]
            assert "messages" in schema["properties"]
            assert "chat_template_kwargs" in schema["properties"]
            # Descriptive, never restrictive — extra fields are forwarded.
            assert schema["additionalProperties"] is True
            assert schema["example"]["messages"]
            assert "token_ids" in str(op["responses"]["200"])

    def test_azure_alias_stays_on_vllm(self, client, test_user):
        """On-prem only: an Azure-configured alias is not dispatched to Azure
        here — it falls back through _resolve_model to a vLLM route, exactly
        like any other alias the vLLM side doesn't know."""
        post_coro, captured = _make_post_coro_capture(
            make_httpx_response(200, {"token_ids": [1]})
        )
        client.__httpx_mock__.post = post_coro

        resp = client.post(
            "/v1/chat/completions/render",
            json={
                "model": "azure-gpt-4",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert captured["body"]["model"] in ("real-llm-v1", "real-vlm-v1")


class TestRenderDecode:
    """``?decode=true`` — the gateway detokenizes the rendered token ids so a
    human can read the prompt (vLLM itself returns ids only)."""

    _RENDER_BODY = {
        "request_id": "chatcmpl-1",
        "model": "real-llm-v1",
        "token_ids": [151644, 8948, 198],
        "sampling_params": {"temperature": 0.7},
    }
    _REQUEST = {
        "model": "test-llm",
        "messages": [{"role": "user", "content": "hi"}],
    }

    def test_decode_adds_prompt_text(self, client, test_user, db_session: Session):
        post, calls = _make_post_router({
            "/chat/completions/render": make_httpx_response(200, dict(self._RENDER_BODY)),
            "/detokenize": make_httpx_response(200, {"prompt": "<|im_start|>user\nhi"}),
        })
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/chat/completions/render?decode=true",
            json=self._REQUEST, headers=auth_header(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["decoded_prompt"] == "<|im_start|>user\nhi"
        assert "decode_error" not in data
        # The render body is otherwise untouched, alias still swapped back
        assert data["token_ids"] == [151644, 8948, 198]
        assert data["model"] == "test-llm"

        # Detokenize goes to the SAME server that rendered (tokenizer must
        # match), at the server root, with the real model and those ids
        assert len(calls) == 2
        detok = calls[1]
        assert detok["url"] == "http://mock-llm:8000/detokenize"
        assert detok["body"] == {"model": "real-llm-v1", "tokens": [151644, 8948, 198]}

        # Still not billable — decoding is two tokenizer calls, no inference
        rows = db_session.exec(
            select(UsageLog).where(UsageLog.user_id == test_user.id)
        ).all()
        assert rows == []

    def test_decode_is_on_by_default(self, client, test_user):
        """No flag at all still yields readable text — token ids alone would
        leave the endpoint one step short of its own purpose."""
        post, calls = _make_post_router({
            "/chat/completions/render": make_httpx_response(200, dict(self._RENDER_BODY)),
            "/detokenize": make_httpx_response(200, {"prompt": "the prompt"}),
        })
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/chat/completions/render",
            json=self._REQUEST, headers=auth_header(),
        )
        assert resp.status_code == 200
        assert resp.json()["decoded_prompt"] == "the prompt"
        assert len(calls) == 2

    def test_decode_false_is_pure_passthrough(self, client, test_user):
        """The opt-out: exactly what vLLM returned, in exactly one call."""
        post, calls = _make_post_router({
            "/chat/completions/render": make_httpx_response(200, dict(self._RENDER_BODY)),
        })
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/chat/completions/render?decode=false",
            json=self._REQUEST, headers=auth_header(),
        )
        assert resp.status_code == 200
        assert "decoded_prompt" not in resp.json()
        assert "decode_error" not in resp.json()
        assert len(calls) == 1

    def test_decode_failure_keeps_the_render(self, client, test_user):
        """A downstream without /detokenize must not cost the caller the render
        they already have — the reason is reported instead."""
        post, _calls = _make_post_router(
            {"/chat/completions/render": make_httpx_response(200, dict(self._RENDER_BODY))},
            raises={"/detokenize": Exception("connection refused")},
        )
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/chat/completions/render?decode=true",
            json=self._REQUEST, headers=auth_header(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["token_ids"] == [151644, 8948, 198]
        assert "decoded_prompt" not in data
        assert "detokenize request failed" in data["decode_error"]

    def test_decode_non_200_reported(self, client, test_user):
        post, _calls = _make_post_router({
            "/chat/completions/render": make_httpx_response(200, dict(self._RENDER_BODY)),
            "/detokenize": make_httpx_response(404, {"detail": "Not Found"}),
        })
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/chat/completions/render?decode=true",
            json=self._REQUEST, headers=auth_header(),
        )
        assert resp.status_code == 200
        assert resp.json()["decode_error"] == "detokenize returned HTTP 404"

    def test_decode_without_token_ids(self, client, test_user):
        post, calls = _make_post_router({
            "/chat/completions/render": make_httpx_response(200, {"request_id": "x"}),
        })
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/chat/completions/render?decode=true",
            json=self._REQUEST, headers=auth_header(),
        )
        assert resp.status_code == 200
        assert "no token_ids" in resp.json()["decode_error"]
        # Nothing to detokenize — no second call is made
        assert len(calls) == 1

    def test_downstream_own_decoded_prompt_wins(self, client, test_user):
        """A newer vLLM that closes vllm#39819 itself keeps its own answer —
        the gateway does not second-guess it or make a redundant call."""
        body = dict(self._RENDER_BODY, decoded_prompt="from downstream")
        post, calls = _make_post_router({
            "/chat/completions/render": make_httpx_response(200, body),
        })
        client.__httpx_mock__.post = post

        resp = client.post(
            "/v1/chat/completions/render?decode=true",
            json=self._REQUEST, headers=auth_header(),
        )
        assert resp.status_code == 200
        assert resp.json()["decoded_prompt"] == "from downstream"
        assert len(calls) == 1

    def test_decode_documented_as_query_param(self, client):
        spec = client.get("/openapi.json").json()
        for path in ("/v1/chat/completions/render", "/chat/completions/render"):
            params = {p["name"]: p for p in spec["paths"][path]["post"]["parameters"]}
            assert params["decode"]["in"] == "query"
            assert params["decode"]["schema"]["default"] is True


class TestRenderIsNotObserved:
    """Rendering is a debug query, not inference: it must stay outside both the
    billing ledger and Langfuse. `_log_usage` is the single seam for both, and
    this path deliberately never reaches it — pinned here so a later refactor
    that "helpfully" routes every forward through it fails loudly."""

    def test_no_usage_row_and_no_langfuse_generation(
        self, client, test_user, db_session: Session
    ):
        client.__httpx_mock__.post = _make_post_coro(
            make_httpx_response(200, {"token_ids": [1, 2, 3], "model": "real-llm-v1"})
        )

        with patch("app.services.vllm_proxy.record_generation") as rec, \
                patch("app.services.vllm_proxy._log_usage") as log_usage:
            resp = client.post(
                "/v1/chat/completions/render",
                json={"model": "test-llm",
                      "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )

        assert resp.status_code == 200
        rec.assert_not_called()
        log_usage.assert_not_called()
        assert db_session.exec(
            select(UsageLog).where(UsageLog.user_id == test_user.id)
        ).all() == []

    def test_downstream_error_is_not_observed(self, client, test_user):
        """Not even the error path — a failed render is not a failed
        generation, so it must not show up as one in Langfuse."""
        client.__httpx_mock__.post = _make_post_coro_raise(Exception("boom"))

        with patch("app.services.vllm_proxy.record_generation") as rec, \
                patch("app.services.vllm_proxy._log_error") as log_error:
            resp = client.post(
                "/v1/chat/completions/render",
                json={"model": "test-llm",
                      "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )

        assert resp.status_code == 502
        rec.assert_not_called()
        log_error.assert_not_called()
