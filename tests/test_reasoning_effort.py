"""Per-model reasoning-effort compatibility (app/services/reasoning_effort.py).

A model upgrade can drop an effort level the previous build accepted (the
canonical case: no more "high"), while clients keep sending the old one and
the downstream 400s. A route that declares `reasoning_efforts` gets its
outgoing requests rewritten to a level that model actually takes; a route
that declares nothing keeps the gateway's faithful pass-through.
"""

from __future__ import annotations

import pytest

from app.services.reasoning_effort import (
    adapt_effort,
    apply_to_openai_body,
    apply_to_responses_body,
    declared_efforts,
    normalize_effort,
)
from tests.conftest import (
    TEST_AZURE_MODELS,
    TEST_BEDROCK_MODELS,
    TEST_MODEL_ROUTING,
    auth_header,
    make_httpx_response,
)

# The upgrade this feature exists for: the new build serves everything but
# "high", which is exactly what the pinned clients still send.
UPGRADED = {"reasoning_efforts": ["none", "low", "medium", "xhigh"]}


@pytest.fixture
def reasoning_llm():
    """Declare test-llm as a reasoning model that no longer accepts "high".

    Mutates the canonical TEST_MODEL_ROUTING entry in place (the autouse
    patch points at the same object) and restores it afterwards — same
    contract as the `native_llm` fixture in test_native_messages.py.
    """
    entry = TEST_MODEL_ROUTING["test-llm"]
    entry["is_reasoning"] = True
    entry.update(UPGRADED)
    yield entry
    entry.pop("is_reasoning", None)
    entry.pop("reasoning_efforts", None)
    entry.pop("reasoning_effort_map", None)
    entry.pop("native_messages", None)


class TestNormalizeAndDeclare:
    def test_spelling_variants_normalize(self):
        assert normalize_effort("  HIGH ") == "high"
        assert normalize_effort("MAX") == "xhigh"
        assert normalize_effort("extra-high") == "xhigh"

    def test_unknown_spelling_kept_verbatim(self):
        assert normalize_effort("Ultra") == "ultra"

    def test_blank_and_non_strings_are_none(self):
        assert normalize_effort("") is None
        assert normalize_effort(None) is None
        assert normalize_effort(3) is None

    def test_undeclared_route_returns_none(self):
        assert declared_efforts({"type": "llm"}) is None
        assert declared_efforts(None) is None

    def test_declaration_is_normalized_and_deduped(self):
        route = {"reasoning_efforts": ["MAX", "xhigh", "Low", 7]}
        assert declared_efforts(route) == ["xhigh", "low"]

    def test_empty_declaration_is_meaningful(self):
        """`reasoning_efforts = []` = this model takes no effort knob."""
        assert declared_efforts({"reasoning_efforts": []}) == []

    def test_malformed_declaration_degrades_to_passthrough(self):
        assert declared_efforts({"reasoning_efforts": "high"}) is None


class TestAdaptEffort:
    def test_undeclared_route_passes_through(self):
        assert adapt_effort("high", {"type": "llm"}) == ("high", None)

    def test_supported_level_untouched(self):
        value, note = adapt_effort("medium", UPGRADED)
        assert (value, note) == ("medium", None)

    def test_unsupported_level_steps_down(self):
        """high -> medium, not xhigh: an automatic rewrite must never spend
        more reasoning (and more money) than the caller asked for."""
        value, note = adapt_effort("high", UPGRADED)
        assert value == "medium"
        assert "high -> medium" in note

    def test_rounds_up_only_when_nothing_lower_exists(self):
        value, _ = adapt_effort("low", {"reasoning_efforts": ["high", "xhigh"]})
        assert value == "high"

    def test_empty_declaration_drops_the_field(self):
        value, note = adapt_effort("high", {"reasoning_efforts": []})
        assert value is None
        assert "dropped" in note

    def test_unknown_spelling_dropped_when_declared(self):
        """An off-ladder level can't be placed by distance; the downstream's
        own default beats sending a value it is known not to accept."""
        value, note = adapt_effort("ultra", UPGRADED)
        assert value is None
        assert "dropped" in note

    def test_explicit_map_wins_over_nearest(self):
        route = {**UPGRADED, "reasoning_effort_map": {"high": "XHIGH"}}
        value, note = adapt_effort("high", route)
        assert value == "xhigh"
        assert "reasoning_effort_map" in note

    def test_map_to_blank_drops_the_field(self):
        route = {**UPGRADED, "reasoning_effort_map": {"high": ""}}
        assert adapt_effort("high", route)[0] is None

    def test_malformed_map_ignored(self):
        route = {**UPGRADED, "reasoning_effort_map": ["high"]}
        assert adapt_effort("high", route)[0] == "medium"

    def test_no_request_no_value(self):
        assert adapt_effort(None, UPGRADED) == (None, None)


class TestApplyHelpers:
    def test_openai_body_rewritten(self):
        body = {"reasoning_effort": "high"}
        apply_to_openai_body(body, UPGRADED)
        assert body["reasoning_effort"] == "medium"

    def test_undeclared_route_leaves_even_an_odd_value_alone(self):
        body = {"reasoning_effort": None}
        apply_to_openai_body(body, {"type": "llm"})
        assert body == {"reasoning_effort": None}

    def test_openai_body_without_the_field_untouched(self):
        body = {"messages": []}
        apply_to_openai_body(body, {"reasoning_efforts": []})
        assert body == {"messages": []}

    def test_responses_body_rewritten(self):
        body = {"reasoning": {"effort": "high", "summary": "auto"}}
        apply_to_responses_body(body, UPGRADED)
        assert body["reasoning"] == {"effort": "medium", "summary": "auto"}

    def test_responses_body_drops_empty_reasoning_object(self):
        body = {"reasoning": {"effort": "high"}}
        apply_to_responses_body(body, {"reasoning_efforts": []})
        assert "reasoning" not in body


class TestVllmChatCompletions:
    def _capture(self, client):
        captured: dict = {}

        async def fake_post(url, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return make_httpx_response(200, {
                "id": "x", "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            })

        client.__httpx_mock__.post = fake_post
        return captured

    def test_unsupported_effort_adapted(self, client, test_user, reasoning_llm):
        captured = self._capture(client)
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-llm",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "high",
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert captured["json"]["reasoning_effort"] == "medium"

    def test_supported_effort_forwarded_unchanged(self, client, test_user, reasoning_llm):
        captured = self._capture(client)
        client.post(
            "/v1/chat/completions",
            json={
                "model": "test-llm",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "low",
            },
            headers=auth_header(),
        )
        assert captured["json"]["reasoning_effort"] == "low"

    def test_undeclared_route_still_passes_effort_through(self, client, test_user):
        """No `reasoning_efforts` on the route = pre-feature behavior."""
        captured = self._capture(client)
        client.post(
            "/v1/chat/completions",
            json={
                "model": "test-llm",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "high",
            },
            headers=auth_header(),
        )
        assert captured["json"]["reasoning_effort"] == "high"


class TestVllmMessages:
    def _capture(self, client):
        captured: dict = {}

        async def fake_post(url, **kwargs):
            captured["url"] = str(url)
            captured["json"] = kwargs.get("json", {})
            if str(url).endswith("/messages"):
                return make_httpx_response(200, {
                    "id": "msg_1", "type": "message", "role": "assistant",
                    "model": "real-llm-v1",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                })
            return make_httpx_response(200, {
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": "ok"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            })

        client.__httpx_mock__.post = fake_post
        return captured

    def test_translated_path_adapts_effort(self, client, test_user, reasoning_llm):
        captured = self._capture(client)
        resp = client.post(
            "/v1/messages",
            json={
                "model": "test-llm", "max_tokens": 50,
                "messages": [{"role": "user", "content": "hi"}],
                "effort": "high",
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert captured["json"]["reasoning_effort"] == "medium"

    def test_thinking_budget_bucket_also_adapted(self, client, test_user, reasoning_llm):
        """A 32k budget buckets to "high", which this model no longer takes —
        the compatibility pass catches it after the bucketing."""
        captured = self._capture(client)
        client.post(
            "/v1/messages",
            json={
                "model": "test-llm", "max_tokens": 50,
                "messages": [{"role": "user", "content": "hi"}],
                "thinking": {"type": "enabled", "budget_tokens": 32000},
            },
            headers=auth_header(),
        )
        assert captured["json"]["reasoning_effort"] == "medium"

    def test_native_path_carries_no_effort_to_adapt(self, client, test_user, reasoning_llm):
        """The native Anthropic pass-through needs no compatibility pass: the
        body sanitizer already strips `effort` (not in vLLM's schema), and a
        `thinking` budget is clamped downstream rather than rejected."""
        reasoning_llm["native_messages"] = True
        captured = self._capture(client)
        resp = client.post(
            "/v1/messages",
            json={
                "model": "test-llm", "max_tokens": 50,
                "messages": [{"role": "user", "content": "hi"}],
                "effort": "max",
                "thinking": {"type": "enabled", "budget_tokens": 32000},
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert captured["url"].endswith("/v1/messages")
        assert "effort" not in captured["json"]
        assert captured["json"]["thinking"] == {"type": "enabled", "budget_tokens": 32000}


class TestAzureAdaptation:
    @pytest.fixture
    def azure_upgraded(self):
        entry = TEST_AZURE_MODELS["azure-gpt-4"]
        entry["is_reasoning"] = True
        entry.update(UPGRADED)
        yield entry
        entry.pop("is_reasoning", None)
        entry.pop("reasoning_efforts", None)

    def _capture(self, client):
        captured: dict = {}

        async def fake_post(url, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return make_httpx_response(200, {
                "id": "resp_test", "status": "completed",
                "output": [{"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": "ok"}]}],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            })

        client.__httpx_mock__.post = fake_post
        return captured

    def test_chat_completions_effort_adapted(self, client, azure_upgraded):
        captured = self._capture(client)
        resp = client.post(
            "/azure/v1/chat/completions",
            json={
                "model": "azure-gpt-4",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "high",
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert captured["json"]["reasoning"]["effort"] == "medium"

    def test_responses_passthrough_adapted_only_when_declared(self, client, azure_upgraded):
        captured = self._capture(client)
        client.post(
            "/azure/v1/responses",
            json={
                "model": "azure-gpt-4",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
                "reasoning": {"effort": "high"},
            },
            headers=auth_header(),
        )
        assert captured["json"]["reasoning"]["effort"] == "medium"

    def test_responses_passthrough_untouched_without_declaration(self, client):
        captured = self._capture(client)
        client.post(
            "/azure/v1/responses",
            json={
                "model": "azure-gpt-4",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
                "reasoning": {"effort": "high"},
            },
            headers=auth_header(),
        )
        assert captured["json"]["reasoning"]["effort"] == "high"


class TestBedrockAdaptation:
    @pytest.fixture
    def bedrock_upgraded(self):
        entry = TEST_BEDROCK_MODELS["bedrock-claude"]
        entry["is_reasoning"] = True
        entry["reasoning_efforts"] = ["low", "medium"]
        yield entry
        entry.pop("is_reasoning", None)
        entry.pop("reasoning_efforts", None)

    def test_effort_adapted_before_thinking_budget_expansion(self, client, bedrock_upgraded):
        captured: dict = {}

        async def fake_post(url, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return make_httpx_response(200, {
                "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
            })

        client.__httpx_mock__.post = fake_post
        resp = client.post(
            "/aws/v1/chat/completions",
            json={
                "model": "bedrock-claude",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "xhigh",
            },
            headers=auth_header(),
        )
        assert resp.status_code == 200
        thinking = captured["json"]["additionalModelRequestFields"]["thinking"]
        # xhigh (32768 budget) clamped to the declared medium (8192).
        assert thinking["budget_tokens"] == 8192


class TestConfigPlumbing:
    """The two keys must survive the config loader on all three backends —
    they are policy the proxies read off the resolved route entry."""

    def _build(self, raw):
        from app.core import config as cfg

        return cfg._build_config(raw)

    def test_declared_on_every_backend(self):
        raw = {
            "models": {"llm": {"local": {
                "real_model": "m", "base_url": "http://x/v1",
                "reasoning_efforts": ["low", "medium"],
                "reasoning_effort_map": {"high": "medium"},
            }}},
            "azure_models": {"az": {
                "type": "llm", "endpoint": "https://e", "deployment": "d", "api_key": "k",
                "reasoning_efforts": ["medium"],
            }},
            "bedrock_models": {"br": {
                "type": "llm", "model_id": "anthropic.x", "api_key": "k",
                "reasoning_efforts": [],
            }},
        }
        _, models, _, _, azure, _, bedrock, _ = self._build(raw)
        assert models["local"]["reasoning_efforts"] == ["low", "medium"]
        assert models["local"]["reasoning_effort_map"] == {"high": "medium"}
        assert azure["az"]["reasoning_efforts"] == ["medium"]
        assert bedrock["br"]["reasoning_efforts"] == []

    def test_absent_keys_stay_absent(self):
        raw = {"models": {"llm": {"local": {"real_model": "m", "base_url": "http://x/v1"}}}}
        _, models, _, _, _, _, _, _ = self._build(raw)
        assert "reasoning_efforts" not in models["local"]
        assert "reasoning_effort_map" not in models["local"]


class TestAdminConfigValidation:
    def test_rejects_non_list_reasoning_efforts(self, client, admin_user):
        from tests.conftest import web_auth_header

        resp = client.put(
            "/admin/api/config",
            json={
                "models": {
                    "m": {"type": "llm", "base_url": "http://x/v1",
                          "real_model": "m", "reasoning_efforts": "high"},
                },
                "pricing": {},
            },
            headers=web_auth_header(scopes=["admin"]),
        )
        assert resp.status_code == 400
        assert "reasoning_efforts" in resp.json()["detail"]

    def test_rejects_non_table_effort_map(self, client, admin_user):
        from tests.conftest import web_auth_header

        resp = client.put(
            "/admin/api/config",
            json={
                "models": {
                    "m": {"type": "llm", "base_url": "http://x/v1",
                          "real_model": "m", "reasoning_effort_map": ["high"]},
                },
                "pricing": {},
            },
            headers=web_auth_header(scopes=["admin"]),
        )
        assert resp.status_code == 400
        assert "reasoning_effort_map" in resp.json()["detail"]

    def test_valid_reasoning_policy_reaches_save_config(self, client, admin_user):
        from unittest.mock import patch

        from tests.conftest import web_auth_header

        payload = {
            "models": {
                "m": {"type": "llm", "base_url": "http://x/v1", "real_model": "m",
                      "reasoning_efforts": ["none", "low", "medium", "xhigh"],
                      "reasoning_effort_map": {"high": ""}},
            },
            "pricing": {},
        }
        with patch("app.routers.admin.save_config") as saved:
            resp = client.put(
                "/admin/api/config", json=payload,
                headers=web_auth_header(scopes=["admin"]),
            )
        assert resp.status_code == 200
        # The admin UI edits these keys directly on the config object rather
        # than through data-field inputs — this pins that the server keeps them.
        entry = saved.call_args[0][0]["m"]
        assert entry["reasoning_efforts"] == ["none", "low", "medium", "xhigh"]
        assert entry["reasoning_effort_map"] == {"high": ""}
