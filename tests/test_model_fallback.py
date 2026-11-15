"""The fallback rule is one function: the router and the pages read it alike."""

from __future__ import annotations

from unittest.mock import patch

from app.services import model_fallback

ROUTING = {
    "a-llm": {"base_url": "http://a", "type": "llm"},
    "b-llm": {"base_url": "http://b", "type": "llm"},
    "c-vlm": {"base_url": "http://c", "type": "vlm"},
    "d-emb": {"base_url": "http://d", "type": "embedding"},
}


def _pick(name, types, up, fallback_map=None):
    return model_fallback.pick(name, types, ROUTING, fallback_map or {}, lambda url: url in up)


class TestPick:
    def test_configured_fallback_first_when_it_is_up(self):
        assert _pick("a-llm", ["llm"], {"http://b", "http://c"}, {"llm": "b-llm"})[::2] == ("b-llm", "configured")

    def test_configured_fallback_down_falls_through_to_the_first_up(self):
        assert _pick("a-llm", ["llm", "vlm"], {"http://c"}, {"llm": "b-llm"})[::2] == ("c-vlm", "auto")

    def test_nothing_up_is_best_effort(self):
        assert _pick("a-llm", ["llm"], set())[::2] == ("a-llm", "best_effort")

    def test_no_model_of_the_type(self):
        assert _pick("x", ["reranker"], {"http://a"}) is None

    def test_allowed_types_follow_the_endpoints(self):
        assert model_fallback.allowed_types_for("vlm") == ["vlm", "llm"]
        assert model_fallback.allowed_types_for("vision_embedding") == ["vision_embedding", "embedding"]
        assert model_fallback.allowed_types_for("systemone") == ["systemone"]


class TestRouterAndPagesAgree:
    def test_resolver_uses_the_shared_rule(self, client):
        from app.services import vllm_proxy

        with (
            patch.object(model_fallback, "pick", wraps=model_fallback.pick) as spy,
            patch("app.services.vllm_proxy.is_alive", side_effect=lambda url: "mock-llm" not in url),
        ):
            alias, _route, reason = vllm_proxy._resolve_model("test-llm", ["llm", "vlm"])
        assert spy.called and reason
        assert alias == "test-vlm"

    def test_dashboard_names_the_stand_in(self, client, test_user):
        from tests.conftest import TEST_MODEL_ROUTING, web_auth_header

        down = TEST_MODEL_ROUTING["test-llm"]["base_url"]
        alive = lambda url: url != down  # noqa: E731
        with (
            patch("app.routers.web_ui.is_alive", side_effect=alive),
            patch("app.services.model_catalog.is_alive", side_effect=alive),
        ):
            page = client.get("/dashboard", headers=web_auth_header(sub=test_user.username)).text
        assert "requests go to <code" in page and ">test-vlm</code>" in page
        assert "falls back to another model" not in page
