"""Converse fields a Bedrock model does not take.

Converse answers 400 "This model doesn't support the <field> field" instead
of ignoring a sampling knob the model lacks (OpenAI GPT, gpt-oss, Kimi, GLM
and others each reject a different subset). The gateway drops exactly the
field Bedrock names, retries, and remembers it per model ID.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tests.conftest import (
    TEST_BEDROCK_MODELS,
    FakeBedrockStreamResponse,
    auth_header,
    make_httpx_response,
)
from tests.test_aws_api import _converse_payload, _stream_events

GPT = {"type": "llm", "region": "us-east-1", "model_id": "us.openai.gpt-5.6-terra",
       "api_key": "bedrock-test-key"}

_FIELD_NAMES = {"temperature": "temperature", "topP": "topP", "stopSequences": "stopSequences"}


def _rejection(field: str) -> dict:
    name = _FIELD_NAMES[field]
    return {"message": f"This model doesn't support the {name} field. Remove {name} and try again."}


def _first_rejected(body: dict, unsupported: tuple[str, ...]) -> str | None:
    inference = body.get("inferenceConfig") or {}
    return next((f for f in unsupported if f in inference), None)


@pytest.fixture(autouse=True)
def _gpt_entry():
    from app.services import bedrock_proxy

    with patch.dict(TEST_BEDROCK_MODELS, {"bedrock-gpt": dict(GPT)}), \
            patch.dict(bedrock_proxy._unsupported_fields, {}, clear=True):
        yield


def _post_mock(sent: list[dict], unsupported=("temperature", "topP", "stopSequences")):
    """A Bedrock that rejects one unsupported field per request, like the real one."""
    async def fake_post(url, **kwargs):
        body = json.loads(kwargs["content"])
        sent.append(body)
        field = _first_rejected(body, unsupported)
        if field:
            return make_httpx_response(400, _rejection(field))
        return make_httpx_response(200, _converse_payload("ok", 3, 1))
    return fake_post


CHAT = {"model": "bedrock-gpt", "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 50, "temperature": 0.2, "top_p": 0.9, "stop": ["END"]}


class TestRejectedFieldIsDroppedAndRetried:
    def test_chat_completion_succeeds_without_the_rejected_fields(self, client):
        sent: list[dict] = []
        with patch.object(client.__httpx_mock__, "post", side_effect=_post_mock(sent)):
            resp = client.post("/aws/v1/chat/completions", json=CHAT, headers=auth_header())
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "ok"
        # One field named per 400, so three retries; maxTokens is never dropped.
        assert len(sent) == 4
        assert sent[-1]["inferenceConfig"] == {"maxTokens": 50}

    def test_only_the_named_field_is_dropped(self, client):
        # gpt-oss shape: stop sequences rejected, sampling accepted.
        sent: list[dict] = []
        with patch.object(client.__httpx_mock__, "post",
                          side_effect=_post_mock(sent, unsupported=("stopSequences",))):
            resp = client.post("/aws/v1/chat/completions", json=CHAT, headers=auth_header())
        assert resp.status_code == 200
        assert sent[-1]["inferenceConfig"] == {"maxTokens": 50, "temperature": 0.2, "topP": 0.9}

    def test_learned_fields_are_omitted_on_the_next_request(self, client):
        sent: list[dict] = []
        with patch.object(client.__httpx_mock__, "post", side_effect=_post_mock(sent)):
            client.post("/aws/v1/chat/completions", json=CHAT, headers=auth_header())
            first = len(sent)
            resp = client.post("/aws/v1/chat/completions", json=CHAT, headers=auth_header())
        assert resp.status_code == 200
        assert len(sent) == first + 1
        assert sent[-1]["inferenceConfig"] == {"maxTokens": 50}

    def test_what_is_learned_stays_with_that_model(self, client):
        sent: list[dict] = []
        with patch.object(client.__httpx_mock__, "post", side_effect=_post_mock(sent)):
            client.post("/aws/v1/chat/completions", json=CHAT, headers=auth_header())
        sent.clear()
        with patch.object(client.__httpx_mock__, "post",
                          side_effect=_post_mock(sent, unsupported=())):
            client.post("/aws/v1/chat/completions",
                        json={**CHAT, "model": "bedrock-nova"}, headers=auth_header())
        assert sent[0]["inferenceConfig"]["temperature"] == 0.2

    def test_messages_endpoint_too(self, client):
        sent: list[dict] = []
        with patch.object(client.__httpx_mock__, "post", side_effect=_post_mock(sent)):
            resp = client.post(
                "/aws/v1/messages",
                json={"model": "bedrock-gpt", "max_tokens": 50, "temperature": 1,
                      "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )
        assert resp.status_code == 200
        assert resp.json()["content"][0]["text"] == "ok"
        assert "temperature" not in sent[-1]["inferenceConfig"]

    @pytest.mark.parametrize("path", ["/aws/v1/chat/completions", "/aws/v1/messages"])
    def test_stream_retries_before_the_first_byte(self, client, path):
        sent: list[dict] = []

        def build_request(method, url, **kwargs):
            return json.loads(kwargs["content"])

        async def fake_send(body, **kwargs):
            sent.append(body)
            field = _first_rejected(body, ("temperature",))
            if field:
                return FakeBedrockStreamResponse(
                    [], status_code=400, body_bytes=json.dumps(_rejection(field)).encode())
            return FakeBedrockStreamResponse(_stream_events(["he", "llo"]))

        with patch.object(client.__httpx_mock__, "build_request", build_request), \
                patch.object(client.__httpx_mock__, "send", side_effect=fake_send):
            resp = client.post(
                path,
                json={"model": "bedrock-gpt", "stream": True, "max_tokens": 50,
                      "temperature": 0.5, "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )
        assert resp.status_code == 200
        assert "he" in resp.text and "llo" in resp.text
        assert len(sent) == 2
        assert "temperature" not in sent[1]["inferenceConfig"]


class TestOtherErrorsStillReachTheClient:
    def test_unrelated_400_is_not_retried(self, client):
        calls: list[dict] = []

        async def fake_post(url, **kwargs):
            calls.append(json.loads(kwargs["content"]))
            return make_httpx_response(400, {"message": "The provided model identifier is invalid."})

        with patch.object(client.__httpx_mock__, "post", side_effect=fake_post):
            resp = client.post("/aws/v1/chat/completions", json=CHAT, headers=auth_header())
        assert resp.status_code == 400
        assert len(calls) == 1

    def test_a_field_outside_the_sampling_knobs_is_not_dropped(self, client):
        # Dropping a document or tools would change what the request means.
        calls: list[dict] = []

        async def fake_post(url, **kwargs):
            calls.append(json.loads(kwargs["content"]))
            return make_httpx_response(400, {
                "message": "This model doesn't support the document field for user messages."})

        with patch.object(client.__httpx_mock__, "post", side_effect=fake_post):
            resp = client.post("/aws/v1/chat/completions", json=CHAT, headers=auth_header())
        assert resp.status_code == 400
        assert len(calls) == 1
        assert calls[0]["inferenceConfig"]["temperature"] == 0.2

    def test_rejection_of_a_field_that_was_not_sent_does_not_loop(self, client):
        calls: list[dict] = []

        async def fake_post(url, **kwargs):
            calls.append(json.loads(kwargs["content"]))
            return make_httpx_response(400, _rejection("temperature"))

        with patch.object(client.__httpx_mock__, "post", side_effect=fake_post):
            resp = client.post(
                "/aws/v1/chat/completions",
                json={"model": "bedrock-gpt", "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )
        assert resp.status_code == 400
        assert len(calls) == 1


class TestOpenAiReasoningShape:
    @staticmethod
    def _xlate(model_id: str) -> dict:
        from app.services.converse_adapter import openai_chat_to_converse_request

        return openai_chat_to_converse_request({
            "messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "high",
        }, model_id=model_id)

    @pytest.mark.parametrize("model_id", [
        "us.openai.gpt-5.6-terra", "global.openai.gpt-5.5", "us.openai.gpt-6-sol",
    ])
    def test_gpt_models_get_the_nested_field(self, model_id):
        # The flat key is a 400 (unknown parameter) on these.
        assert self._xlate(model_id)["additionalModelRequestFields"] == {
            "reasoning": {"effort": "high"}}

    def test_gpt_oss_keeps_the_flat_key(self):
        # gpt-oss ignores the nested field silently.
        assert self._xlate("openai.gpt-oss-120b-1:0")["additionalModelRequestFields"] == {
            "reasoning_effort": "high"}
