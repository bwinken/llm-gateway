"""Per-model 429 fallback on the cloud backends (`rate_limit_fallback`).

An Azure / Bedrock entry can name a sibling on the same backend that takes
the request when the downstream answers 429. These tests pin: the hop
happens (non-stream and at stream pre-flight), the retried body is rebuilt
for the fallback model, the response says so in X-Model-Fallback, billing
lands on the model that served, and nothing else (other statuses, missing
config, cycles, exhausted chains) changes behavior.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
from sqlmodel import select

from app.models.schema import UsageLog
from tests.conftest import (
    TEST_AZURE_MODELS,
    TEST_BEDROCK_MODELS,
    FakeBedrockStreamResponse,
    FakeStreamResponse,
    auth_header,
    web_auth_header,
)


def _azure_entry(deployment: str, **extra) -> dict:
    return {
        "type": "llm",
        "endpoint": "https://test.openai.azure.com",
        "deployment": deployment,
        "api_key": "azure-test-key",
        "api_version": "2024-08-01-preview",
        **extra,
    }


def _bedrock_entry(model_id: str, **extra) -> dict:
    return {
        "type": "llm",
        "region": "us-east-1",
        "model_id": model_id,
        "api_key": "bedrock-test-key",
        **extra,
    }


class _Resp:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def _responses_payload(text: str = "ok") -> dict:
    return {
        "id": "resp_test",
        "status": "completed",
        "output": [{
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }],
        "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
    }


def _converse_payload(text: str = "ok") -> dict:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 5, "outputTokens": 2, "totalTokens": 7},
    }


_RATE_LIMITED = {"error": {"code": "429", "message": "Rate limit exceeded"}}


def _scripted_post(statuses_by_key: dict[str, int], key_of, ok_body: dict):
    """A fake client.post answering per target (deployment / model_id) and
    recording every call."""
    calls: list[dict] = []

    async def fake_post(url, **kwargs):
        body = kwargs.get("json", {})
        key = key_of(url, body)
        calls.append({"url": url, "json": body, "key": key})
        status = statuses_by_key.get(key, 200)
        return _Resp(status, _RATE_LIMITED if status == 429 else ok_body)

    return fake_post, calls


def _azure_key(url, body):
    return body.get("model")


def _bedrock_key(url, body):
    return url.split("/model/")[1].split("/")[0]


AZURE_CHAIN = {
    "az-big": _azure_entry("big-deploy", rate_limit_fallback="az-mid"),
    "az-mid": _azure_entry("mid-deploy", rate_limit_fallback="az-small"),
    "az-small": _azure_entry("small-deploy"),
}


class TestAzureRateLimitFallback:
    def test_non_stream_chat_falls_back_on_429(self, client, db_session):
        fake_post, calls = _scripted_post({"big-deploy": 429}, _azure_key, _responses_payload("from mid"))
        client.__httpx_mock__.post = fake_post
        with patch.dict(TEST_AZURE_MODELS, AZURE_CHAIN):
            resp = client.post(
                "/azure/v1/chat/completions",
                json={"model": "az-big", "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "from mid"
        assert [c["key"] for c in calls] == ["big-deploy", "mid-deploy"]
        assert "rate limited: az-big (429) -> az-mid" in resp.headers["X-Model-Fallback"]
        # Billed at the model that actually served.
        rows = db_session.exec(select(UsageLog)).all()
        assert [r.model for r in rows] == ["az-mid"]
        assert rows[0].backend == "azure"

    def test_chain_is_followed_and_last_429_surfaces(self, client):
        fake_post, calls = _scripted_post(
            {"big-deploy": 429, "mid-deploy": 429, "small-deploy": 429},
            _azure_key, _responses_payload(),
        )
        client.__httpx_mock__.post = fake_post
        with patch.dict(TEST_AZURE_MODELS, AZURE_CHAIN):
            resp = client.post(
                "/azure/v1/chat/completions",
                json={"model": "az-big", "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )
        assert resp.status_code == 429
        assert resp.json() == _RATE_LIMITED
        assert [c["key"] for c in calls] == ["big-deploy", "mid-deploy", "small-deploy"]

    def test_two_hops(self, client):
        fake_post, calls = _scripted_post(
            {"big-deploy": 429, "mid-deploy": 429}, _azure_key, _responses_payload("small"),
        )
        client.__httpx_mock__.post = fake_post
        with patch.dict(TEST_AZURE_MODELS, AZURE_CHAIN):
            resp = client.post(
                "/azure/v1/chat/completions",
                json={"model": "az-big", "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )
        assert resp.status_code == 200
        assert [c["key"] for c in calls] == ["big-deploy", "mid-deploy", "small-deploy"]
        header = resp.headers["X-Model-Fallback"]
        assert "az-big (429) -> az-mid" in header and "az-mid (429) -> az-small" in header

    def test_no_fallback_configured_returns_429(self, client):
        fake_post, calls = _scripted_post({"gpt-4-deploy": 429}, _azure_key, _responses_payload())
        client.__httpx_mock__.post = fake_post
        resp = client.post(
            "/azure/v1/chat/completions",
            json={"model": "azure-gpt-4", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 429
        assert len(calls) == 1

    def test_other_errors_do_not_fall_back(self, client):
        fake_post, calls = _scripted_post({"big-deploy": 500}, _azure_key, _responses_payload())
        client.__httpx_mock__.post = fake_post
        with patch.dict(TEST_AZURE_MODELS, AZURE_CHAIN):
            resp = client.post(
                "/azure/v1/chat/completions",
                json={"model": "az-big", "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )
        assert resp.status_code == 500
        assert len(calls) == 1

    def test_cycle_tries_each_model_once(self, client):
        cyclic = {
            "az-a": _azure_entry("a-deploy", rate_limit_fallback="az-b"),
            "az-b": _azure_entry("b-deploy", rate_limit_fallback="az-a"),
        }
        fake_post, calls = _scripted_post(
            {"a-deploy": 429, "b-deploy": 429}, _azure_key, _responses_payload(),
        )
        client.__httpx_mock__.post = fake_post
        with patch.dict(TEST_AZURE_MODELS, cyclic):
            resp = client.post(
                "/azure/v1/chat/completions",
                json={"model": "az-a", "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )
        assert resp.status_code == 429
        assert [c["key"] for c in calls] == ["a-deploy", "b-deploy"]

    def test_fallback_to_unusable_type_is_skipped(self, client):
        # azure-embed is an embedding entry — not servable on chat surfaces.
        bad = {"az-x": _azure_entry("x-deploy", rate_limit_fallback="azure-embed")}
        fake_post, calls = _scripted_post({"x-deploy": 429}, _azure_key, _responses_payload())
        client.__httpx_mock__.post = fake_post
        with patch.dict(TEST_AZURE_MODELS, bad):
            resp = client.post(
                "/azure/v1/chat/completions",
                json={"model": "az-x", "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )
        assert resp.status_code == 429
        assert len(calls) == 1

    def test_retry_body_uses_fallback_effort_policy(self, client):
        # The fallback model rejects "high"; the primary accepts it. The
        # retried request must be rebuilt for the fallback entry, and the
        # primary's attempt must not have been affected.
        chain = {
            "az-p": _azure_entry("p-deploy", rate_limit_fallback="az-q"),
            "az-q": _azure_entry(
                "q-deploy",
                reasoning_efforts=["low", "medium"],
                reasoning_effort_map={"high": "medium"},
            ),
        }
        fake_post, calls = _scripted_post({"p-deploy": 429}, _azure_key, _responses_payload())
        client.__httpx_mock__.post = fake_post
        with patch.dict(TEST_AZURE_MODELS, chain):
            resp = client.post(
                "/azure/v1/chat/completions",
                json={
                    "model": "az-p", "reasoning_effort": "high",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers=auth_header(),
            )
        assert resp.status_code == 200
        assert calls[0]["json"]["reasoning"]["effort"] == "high"
        assert calls[1]["json"]["reasoning"]["effort"] == "medium"

    def test_messages_non_stream_falls_back(self, client):
        fake_post, calls = _scripted_post({"big-deploy": 429}, _azure_key, _responses_payload("hey"))
        client.__httpx_mock__.post = fake_post
        with patch.dict(TEST_AZURE_MODELS, AZURE_CHAIN):
            resp = client.post(
                "/azure/v1/messages",
                json={"model": "az-big", "max_tokens": 64,
                      "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )
        assert resp.status_code == 200
        assert resp.json()["content"][0]["text"] == "hey"
        assert [c["key"] for c in calls] == ["big-deploy", "mid-deploy"]

    def test_responses_passthrough_falls_back(self, client):
        fake_post, calls = _scripted_post({"big-deploy": 429}, _azure_key, _responses_payload())
        client.__httpx_mock__.post = fake_post
        with patch.dict(TEST_AZURE_MODELS, AZURE_CHAIN):
            resp = client.post(
                "/azure/v1/responses",
                json={"model": "az-big", "input": "hi"},
                headers=auth_header(),
            )
        assert resp.status_code == 200
        # Each attempt carries its own deployment — the first body wasn't
        # mutated by the retry.
        assert [c["json"]["model"] for c in calls] == ["big-deploy", "mid-deploy"]
        assert "X-Model-Fallback" in resp.headers

    def test_stream_falls_back_at_preflight(self, client):
        sse = [
            'data: {"type":"response.output_text.delta","delta":"Hi"}',
            'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":3,"output_tokens":1}}}',
        ]
        sent: list[str] = []

        def build_request(method, url, **kwargs):
            sent.append(kwargs["json"]["model"])
            return httpx.Request(method, url)

        mock = client.__httpx_mock__
        send = AsyncMock(side_effect=[
            FakeStreamResponse([], status_code=429, body_bytes=b'{"error":{"code":"429"}}'),
            FakeStreamResponse(sse),
        ])
        # patch.object restores the shared mock — reset_mock() between tests
        # does not undo a replaced attribute or a side_effect.
        with patch.object(mock, "build_request", side_effect=build_request), \
                patch.object(mock, "send", send), \
                patch.dict(TEST_AZURE_MODELS, AZURE_CHAIN):
            resp = client.post(
                "/azure/v1/messages",
                json={"model": "az-big", "max_tokens": 64, "stream": True,
                      "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )
        assert resp.status_code == 200
        assert sent == ["big-deploy", "mid-deploy"]
        assert "event: message_stop" in resp.text
        assert "az-big (429) -> az-mid" in resp.headers["X-Model-Fallback"]

    def test_unified_v1_dispatch_falls_back_within_azure(self, client):
        fake_post, calls = _scripted_post({"big-deploy": 429}, _azure_key, _responses_payload())
        client.__httpx_mock__.post = fake_post
        with patch.dict(TEST_AZURE_MODELS, AZURE_CHAIN):
            resp = client.post(
                "/v1/chat/completions",
                json={"model": "az-big", "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )
        assert resp.status_code == 200
        assert [c["key"] for c in calls] == ["big-deploy", "mid-deploy"]


BEDROCK_CHAIN = {
    "br-big": _bedrock_entry("anthropic.big-v1:0", rate_limit_fallback="br-small"),
    "br-small": _bedrock_entry("anthropic.small-v1:0"),
}


class TestBedrockRateLimitFallback:
    def test_non_stream_chat_falls_back_on_429(self, client, db_session):
        fake_post, calls = _scripted_post(
            {"anthropic.big-v1%3A0": 429}, _bedrock_key, _converse_payload("small"),
        )
        client.__httpx_mock__.post = fake_post
        with patch.dict(TEST_BEDROCK_MODELS, BEDROCK_CHAIN):
            resp = client.post(
                "/aws/v1/chat/completions",
                json={"model": "br-big", "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "small"
        assert [c["key"] for c in calls] == ["anthropic.big-v1%3A0", "anthropic.small-v1%3A0"]
        assert "br-big (429) -> br-small" in resp.headers["X-Model-Fallback"]
        rows = db_session.exec(select(UsageLog)).all()
        assert [(r.model, r.backend) for r in rows] == [("br-small", "bedrock")]

    def test_messages_non_stream_exhausted_returns_429(self, client):
        fake_post, calls = _scripted_post(
            {"anthropic.big-v1%3A0": 429, "anthropic.small-v1%3A0": 429},
            _bedrock_key, _converse_payload(),
        )
        client.__httpx_mock__.post = fake_post
        with patch.dict(TEST_BEDROCK_MODELS, BEDROCK_CHAIN):
            resp = client.post(
                "/aws/v1/messages",
                json={"model": "br-big", "max_tokens": 64,
                      "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )
        assert resp.status_code == 429
        assert len(calls) == 2

    def test_stream_falls_back_at_preflight(self, client):
        events = [
            ("messageStart", {"role": "assistant"}),
            ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "yo"}}),
            ("contentBlockStop", {"contentBlockIndex": 0}),
            ("messageStop", {"stopReason": "end_turn"}),
            ("metadata", {"usage": {"inputTokens": 3, "outputTokens": 1, "totalTokens": 4}}),
        ]
        urls: list[str] = []

        def build_request(method, url, **kwargs):
            urls.append(url)
            return httpx.Request(method, url)

        mock = client.__httpx_mock__
        send = AsyncMock(side_effect=[
            FakeBedrockStreamResponse(
                [], status_code=429,
                body_bytes=b'{"message":"Too many requests, please wait before trying again."}',
            ),
            FakeBedrockStreamResponse(events),
        ])
        with patch.object(mock, "build_request", side_effect=build_request), \
                patch.object(mock, "send", send), \
                patch.dict(TEST_BEDROCK_MODELS, BEDROCK_CHAIN):
            resp = client.post(
                "/aws/v1/messages",
                json={"model": "br-big", "max_tokens": 64, "stream": True,
                      "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )
        assert resp.status_code == 200
        assert "big-v1" in urls[0] and "small-v1" in urls[1]
        assert "event: message_stop" in resp.text

    def test_never_crosses_backends(self, client):
        # A Bedrock entry pointing at an Azure alias is not a usable hop.
        bad = {"br-x": _bedrock_entry("anthropic.x-v1:0", rate_limit_fallback="azure-gpt-4")}
        fake_post, calls = _scripted_post(
            {"anthropic.x-v1%3A0": 429}, _bedrock_key, _converse_payload(),
        )
        client.__httpx_mock__.post = fake_post
        with patch.dict(TEST_BEDROCK_MODELS, bad):
            resp = client.post(
                "/aws/v1/chat/completions",
                json={"model": "br-x", "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )
        assert resp.status_code == 429
        assert len(calls) == 1


class TestConfig:
    def test_key_parsed_for_cloud_entries(self):
        from app.core.config import _build_config

        raw = {
            "azure_models": {
                "a1": {**_azure_entry("d1"), "rate_limit_fallback": "a2"},
                "a2": _azure_entry("d2"),
            },
            "bedrock_models": {
                "b1": {**_bedrock_entry("m1"), "rate_limit_fallback": "b2"},
                "b2": _bedrock_entry("m2"),
            },
        }
        _, _, _, _, azure, _, bedrock, _ = _build_config(raw)
        assert azure["a1"]["rate_limit_fallback"] == "a2"
        assert "rate_limit_fallback" not in azure["a2"]
        assert bedrock["b1"]["rate_limit_fallback"] == "b2"

    def test_bad_target_only_warns(self):
        from app.core.config import _build_config

        raw = {"azure_models": {"a1": {**_azure_entry("d1"), "rate_limit_fallback": "nope"}}}
        _, _, _, _, azure, _, _, _ = _build_config(raw)
        assert azure["a1"]["rate_limit_fallback"] == "nope"

    def test_not_surfaced_in_models_listing(self, client):
        with patch.dict(TEST_AZURE_MODELS, AZURE_CHAIN):
            resp = client.get("/azure/v1/models", headers=auth_header())
        assert resp.status_code == 200
        assert "rate_limit_fallback" not in resp.text


class TestAdminValidation:
    def _put(self, client, admin_user, azure_models: dict):
        body = {
            "models": {},
            "pricing": {"_default": {"input_price_per_1m": 0.1, "output_price_per_1m": 0.1}},
            "fallback": {},
            "azure_models": azure_models,
        }
        with patch("app.routers.admin.save_config"):
            return client.put(
                "/admin/api/config",
                json=body,
                headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
            )

    def test_valid_target_accepted(self, client, admin_user):
        resp = self._put(client, admin_user, {
            "a1": _azure_entry("d1", rate_limit_fallback="a2"),
            "a2": _azure_entry("d2"),
        })
        assert resp.status_code == 200

    def test_unknown_target_rejected(self, client, admin_user):
        resp = self._put(client, admin_user, {"a1": _azure_entry("d1", rate_limit_fallback="ghost")})
        assert resp.status_code == 400
        assert "ghost" in resp.json()["detail"]

    def test_self_reference_rejected(self, client, admin_user):
        resp = self._put(client, admin_user, {"a1": _azure_entry("d1", rate_limit_fallback="a1")})
        assert resp.status_code == 400

    def test_embedding_target_rejected(self, client, admin_user):
        resp = self._put(client, admin_user, {
            "a1": _azure_entry("d1", rate_limit_fallback="e1"),
            "e1": {**_azure_entry("e"), "type": "embedding"},
        })
        assert resp.status_code == 400
