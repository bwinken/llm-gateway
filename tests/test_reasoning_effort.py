"""Per-model reasoning-effort compatibility (app/services/reasoning_effort.py).

A model entry declaring `reasoning_efforts` gets outgoing requests reconciled
with what that model actually accepts (the case it exists for: an upgrade that
dropped `high`). Policy is deliberately explicit: an accepted level is
forwarded, an unaccepted one is rewritten only where `reasoning_effort_map`
says so, and anything else is dropped. A route declaring nothing keeps the
faithful pass-through.
"""

from __future__ import annotations

import pytest

from app.services.reasoning_effort import (
    adapt_effort,
    canonical_effort,
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
# The same model, with the operator saying where "high" should land instead.
MAPPED = {**UPGRADED, "reasoning_effort_map": {"high": "xhigh"}}


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


class TestCanonicalSpelling:
    """Spelling folding is unconditional; level substitution never is."""

    def test_claude_code_xhigh_spellings_fold(self):
        for spelling in ("max", "MAX", " Extra-High ", "x-high", "extra_high"):
            assert canonical_effort(spelling) == "xhigh"

    def test_known_level_gets_the_canonical_casing(self):
        assert canonical_effort("  HIGH ") == "high"

    def test_unknown_level_and_non_strings_come_back_verbatim(self):
        assert canonical_effort("Ultra") == "Ultra"
        assert canonical_effort(None) is None
        assert canonical_effort(3) == 3


class TestAdaptEffort:
    def test_undeclared_route_passes_through(self):
        assert adapt_effort("high", {"type": "llm"}) == ("high", None)

    def test_accepted_level_untouched(self):
        value, note = adapt_effort("medium", UPGRADED)
        assert (value, note) == ("medium", None)

    def test_unaccepted_and_unmapped_is_dropped(self):
        """Nothing is guessed at: without a rule the field just goes away and
        the downstream applies its own default."""
        value, note = adapt_effort("high", UPGRADED)
        assert value is None
        assert "dropped" in note

    def test_map_sends_the_operators_target(self):
        value, note = adapt_effort("high", MAPPED)
        assert value == "xhigh"
        assert "reasoning_effort_map" in note

    def test_map_target_outside_the_declared_list_is_honored(self):
        """The operator is the authority on their own downstream."""
        route = {**UPGRADED, "reasoning_effort_map": {"high": "high"}}
        assert adapt_effort("high", route)[0] == "high"

    def test_map_never_touches_an_accepted_level(self):
        """A rule for a level the model accepts is dead config, not a knob for
        rewriting perfectly valid requests."""
        route = {**UPGRADED, "reasoning_effort_map": {"medium": "low"}}
        assert adapt_effort("medium", route) == ("medium", None)

    def test_map_to_blank_drops_the_field(self):
        route = {**UPGRADED, "reasoning_effort_map": {"high": ""}}
        assert adapt_effort("high", route)[0] is None

    def test_map_normalizes_spellings_on_both_sides(self):
        route = {**UPGRADED, "reasoning_efforts": ["low"],
                 "reasoning_effort_map": {"MAX": "Low"}}
        assert adapt_effort("xhigh", route)[0] == "low"

    def test_empty_declaration_drops_everything_unmapped(self):
        value, note = adapt_effort("high", {"reasoning_efforts": []})
        assert value is None
        assert "dropped" in note

    def test_unknown_spelling_dropped_unless_mapped(self):
        assert adapt_effort("ultra", UPGRADED)[0] is None
        route = {**UPGRADED, "reasoning_effort_map": {"ultra": "medium"}}
        assert adapt_effort("ultra", route)[0] == "medium"

    def test_malformed_map_ignored(self):
        route = {**UPGRADED, "reasoning_effort_map": ["high"]}
        assert adapt_effort("high", route)[0] is None

    def test_no_request_no_value(self):
        assert adapt_effort(None, UPGRADED) == (None, None)


class TestApplyHelpers:
    def test_openai_body_mapped(self):
        body = {"reasoning_effort": "high"}
        apply_to_openai_body(body, MAPPED)
        assert body["reasoning_effort"] == "xhigh"

    def test_openai_body_stripped_when_unmapped(self):
        body = {"model": "m", "reasoning_effort": "high"}
        apply_to_openai_body(body, UPGRADED)
        assert body == {"model": "m"}

    def test_undeclared_route_leaves_even_an_odd_value_alone(self):
        body = {"reasoning_effort": None}
        apply_to_openai_body(body, {"type": "llm"})
        assert body == {"reasoning_effort": None}

    def test_undeclared_route_still_folds_the_spelling(self):
        """"max" and "xhigh" must mean one thing on every surface: the
        Anthropic path folds them in translation, so this one does too."""
        body = {"reasoning_effort": "max"}
        apply_to_openai_body(body, {"type": "llm"})
        assert body["reasoning_effort"] == "xhigh"

    def test_undeclared_route_leaves_an_unknown_level_alone(self):
        body = {"reasoning_effort": "Ultra"}
        apply_to_openai_body(body, {"type": "llm"})
        assert body == {"reasoning_effort": "Ultra"}

    def test_declared_route_accepts_a_variant_spelling(self):
        """UPGRADED declares "xhigh"; a client spelling it "max" is asking
        for a level this model accepts, not for one to strip."""
        body = {"reasoning_effort": "max"}
        apply_to_openai_body(body, UPGRADED)
        assert body["reasoning_effort"] == "xhigh"

    def test_responses_body_folds_without_a_declaration(self):
        body = {"reasoning": {"effort": "extra-high"}}
        apply_to_responses_body(body, {"type": "llm"})
        assert body["reasoning"] == {"effort": "xhigh"}

    def test_openai_body_without_the_field_untouched(self):
        body = {"messages": []}
        apply_to_openai_body(body, {"reasoning_efforts": []})
        assert body == {"messages": []}

    def test_responses_body_mapped(self):
        body = {"reasoning": {"effort": "high", "summary": "auto"}}
        apply_to_responses_body(body, MAPPED)
        assert body["reasoning"] == {"effort": "xhigh", "summary": "auto"}

    def test_responses_body_drops_empty_reasoning_object(self):
        body = {"reasoning": {"effort": "high"}}
        apply_to_responses_body(body, UPGRADED)
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

    def _post(self, client, effort):
        return client.post(
            "/v1/chat/completions",
            json={
                "model": "test-llm",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": effort,
            },
            headers=auth_header(),
        )

    def test_unaccepted_effort_stripped(self, client, test_user, reasoning_llm):
        captured = self._capture(client)
        resp = self._post(client, "high")
        assert resp.status_code == 200
        assert "reasoning_effort" not in captured["json"]

    def test_mapped_effort_rewritten(self, client, test_user, reasoning_llm):
        reasoning_llm["reasoning_effort_map"] = {"high": "xhigh"}
        captured = self._capture(client)
        self._post(client, "high")
        assert captured["json"]["reasoning_effort"] == "xhigh"

    def test_accepted_effort_forwarded_unchanged(self, client, test_user, reasoning_llm):
        captured = self._capture(client)
        self._post(client, "low")
        assert captured["json"]["reasoning_effort"] == "low"

    def test_variant_spelling_forwarded_canonically(self, client, test_user):
        """No declaration needed: the spelling fold is what makes "max" the
        same request as "xhigh" for every downstream."""
        captured = self._capture(client)
        self._post(client, "max")
        assert captured["json"]["reasoning_effort"] == "xhigh"

    def test_undeclared_route_still_passes_effort_through(self, client, test_user):
        """No `reasoning_efforts` on the route = pre-feature behavior."""
        captured = self._capture(client)
        self._post(client, "high")
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

    def test_translated_path_maps_effort(self, client, test_user, reasoning_llm):
        reasoning_llm["reasoning_effort_map"] = {"high": "xhigh"}
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
        assert captured["json"]["reasoning_effort"] == "xhigh"

    def test_thinking_budget_bucket_also_reconciled(self, client, test_user, reasoning_llm):
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
        assert "reasoning_effort" not in captured["json"]

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
        entry.update(MAPPED)
        yield entry
        entry.pop("is_reasoning", None)
        entry.pop("reasoning_efforts", None)
        entry.pop("reasoning_effort_map", None)

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

    def test_chat_completions_effort_mapped(self, client, azure_upgraded):
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
        assert captured["json"]["reasoning"]["effort"] == "xhigh"

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
        assert captured["json"]["reasoning"]["effort"] == "xhigh"

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
        entry["reasoning_effort_map"] = {"xhigh": "medium"}
        yield entry
        entry.pop("is_reasoning", None)
        entry.pop("reasoning_efforts", None)
        entry.pop("reasoning_effort_map", None)

    def _capture(self, client):
        captured: dict = {}

        async def fake_post(url, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return make_httpx_response(200, {
                "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
            })

        client.__httpx_mock__.post = fake_post
        return captured

    def _post(self, client, effort):
        return client.post(
            "/aws/v1/chat/completions",
            json={
                "model": "bedrock-claude",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": effort,
            },
            headers=auth_header(),
        )

    def test_mapped_before_thinking_budget_expansion(self, client, bedrock_upgraded):
        captured = self._capture(client)
        resp = self._post(client, "xhigh")
        assert resp.status_code == 200
        thinking = captured["json"]["additionalModelRequestFields"]["thinking"]
        # xhigh (32768 budget) mapped to medium, which Converse expands to 8192.
        assert thinking["budget_tokens"] == 8192

    def test_variant_spelling_buys_the_xhigh_budget(self, client, test_user):
        """Without the fold "max" missed the bucket table and landed on the
        medium default (8192) — quietly less thinking than "xhigh" asked for.
        No declaration on the entry, so this is the fold alone."""
        captured = self._capture(client)
        resp = self._post(client, "max")
        assert resp.status_code == 200
        thinking = captured["json"]["additionalModelRequestFields"]["thinking"]
        assert thinking["budget_tokens"] == 32768

    def test_unmapped_level_leaves_thinking_to_the_model(self, client, bedrock_upgraded):
        captured = self._capture(client)
        self._post(client, "high")
        assert "additionalModelRequestFields" not in captured["json"]


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
                      "reasoning_effort_map": {"high": "xhigh"}},
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
        assert entry["reasoning_effort_map"] == {"high": "xhigh"}
