"""
Tests for GET /v1/models and admin PUT /admin/api/config metadata handling.

The endpoint surfaces optional per-model metadata declared in config.toml
(context_window, max_output_tokens, supports_*, display_name, ...) so that
OpenAI-compatible clients (Cursor, Cline, Continue.dev, future Claude Code
versions with dynamic discovery, ...) can show accurate capabilities in
their model pickers without us having to hard-code them client-side.
"""

from __future__ import annotations

from unittest.mock import patch

from tests.conftest import TEST_MODEL_ROUTING, auth_header, web_auth_header


class TestListModels:

    def test_only_llm_and_vlm_returned(self, client, test_user):
        resp = client.get("/v1/models", headers=auth_header())
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        types = {m["type"] for m in data["data"]}
        assert types <= {"llm", "vlm"}
        # embedding / reranker / vision_* entries must be filtered out
        assert "embedding" not in types
        assert "reranker" not in types

    def test_base_fields_present(self, client, test_user):
        resp = client.get("/v1/models", headers=auth_header())
        data = resp.json()
        for m in data["data"]:
            assert "id" in m
            assert m["object"] == "model"
            # Azure/Bedrock entries get their own owned_by since the unified
            # `/v1/models` merges cloud aliases for users with the matching
            # access flag (test_user has both True by default).
            assert m["owned_by"] in ("llm-gateway", "azure-openai", "aws-bedrock")
            assert "type" in m
            assert "capability" in m

    def test_metadata_surfaced_when_set(self, client, test_user):
        """test-vlm in the fixture declares full metadata — the endpoint
        should pass every optional field straight through."""
        resp = client.get("/v1/models", headers=auth_header())
        data = resp.json()
        vlm = next(m for m in data["data"] if m["id"] == "test-vlm")
        assert vlm["display_name"] == "Test VLM"
        assert vlm["context_window"] == 32768
        assert vlm["max_output_tokens"] == 4096
        assert vlm["supports_tools"] is True
        assert vlm["supports_vision"] is True

    def test_metadata_omitted_when_not_set(self, client, test_user):
        """test-llm in the fixture declares no metadata — none of the
        optional keys should appear (backward compatibility guarantee:
        existing configs get the exact same response shape they did
        before the feature was added)."""
        resp = client.get("/v1/models", headers=auth_header())
        data = resp.json()
        llm = next(m for m in data["data"] if m["id"] == "test-llm")
        for key in (
            "display_name",
            "context_window",
            "max_output_tokens",
            "supports_tools",
            "supports_vision",
            "supports_prompt_caching",
        ):
            assert key not in llm, f"{key} should be absent when unset in config"

    def test_401_without_auth(self, client):
        resp = client.get("/v1/models")
        assert resp.status_code in (401, 403)


class TestAdminConfigMetadataValidation:
    """PUT /admin/api/config rejects obviously-wrong metadata shapes.

    The save-config endpoint is strict about metadata types because a bad
    value in config.toml could crash the /v1/models handler for everyone
    the next time it reloads. Catch it at write time with a clear 400.
    """

    BASE_MODEL = {
        "real_model": "rm",
        "base_url": "http://mock:8000/v1",
        "api_key": "",
        "type": "llm",
    }

    def _put(self, client, admin_user, model_extra: dict):
        body = {
            "models": {"m1": {**self.BASE_MODEL, **model_extra}},
            "pricing": {"_default": {"input_price_per_1m": 0.1, "output_price_per_1m": 0.1}},
            "fallback": {},
        }
        # Patch save_config so a happy-path 200 doesn't actually write the
        # project's config.toml and (via reload_config) pop our test-fixture
        # AZURE_MODELS entries — that pollution silently breaks any later
        # test that relies on the Azure fixture being intact.
        with patch("app.routers.admin.save_config"):
            return client.put(
                "/admin/api/config",
                json=body,
                headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
            )

    def test_rejects_context_window_as_bool(self, client, admin_user):
        # True would otherwise slip through because bool is a subclass of
        # int in Python — the endpoint rejects it explicitly.
        resp = self._put(client, admin_user, {"context_window": True})
        assert resp.status_code == 400
        assert "context_window" in resp.json()["detail"]

    def test_rejects_negative_context_window(self, client, admin_user):
        resp = self._put(client, admin_user, {"context_window": -1})
        assert resp.status_code == 400

    def test_rejects_supports_tools_as_string(self, client, admin_user):
        resp = self._put(client, admin_user, {"supports_tools": "yes"})
        assert resp.status_code == 400

    def test_rejects_display_name_as_int(self, client, admin_user):
        resp = self._put(client, admin_user, {"display_name": 42})
        assert resp.status_code == 400

    def test_rejects_max_output_tokens_as_float(self, client, admin_user):
        resp = self._put(client, admin_user, {"max_output_tokens": 8192.0})
        assert resp.status_code == 400

    def test_accepts_hidden_as_bool(self, client, admin_user):
        resp = self._put(client, admin_user, {"hidden": True})
        assert resp.status_code == 200

    def test_rejects_hidden_as_string(self, client, admin_user):
        resp = self._put(client, admin_user, {"hidden": "yes"})
        assert resp.status_code == 400
        assert "hidden" in resp.json()["detail"]


class TestAdminAzureFallbackValidation:
    """PUT /admin/api/config validates the new azure_fallback section.

    Each entry must (a) be a string, (b) point to an existing alias in
    azure_models, and (c) match that alias's type. The handler catches
    these client-side so a typo doesn't silently end up in config.toml as
    an unreachable fallback.

    save_config is patched out — these tests only exercise the validator,
    not the disk write. (A successful save would otherwise mutate the
    shared TEST_AZURE_MODELS via reload_config and break test_azure_api.py.)
    """

    BASE_BODY = {
        "models": {},
        "pricing": {"_default": {"input_price_per_1m": 0.1, "output_price_per_1m": 0.1}},
        "fallback": {},
        "azure_models": {
            "az-llm": {
                "type": "llm",
                "endpoint": "https://x.openai.azure.com",
                "deployment": "d",
                "api_key": "k",
                "api_version": "2024-08-01-preview",
            },
            "az-vlm": {
                "type": "vlm",
                "endpoint": "https://x.openai.azure.com",
                "deployment": "d2",
                "api_key": "k",
                "api_version": "2024-08-01-preview",
            },
        },
    }

    def _put(self, client, admin_user, azure_fallback):
        body = {**self.BASE_BODY, "azure_fallback": azure_fallback}
        with patch("app.routers.admin.save_config"):
            return client.put(
                "/admin/api/config",
                json=body,
                headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
            )

    def test_accepts_valid_fallback(self, client, admin_user):
        resp = self._put(client, admin_user, {"llm": "az-llm", "vlm": "az-vlm"})
        assert resp.status_code == 200

    def test_rejects_unknown_alias(self, client, admin_user):
        resp = self._put(client, admin_user, {"llm": "does-not-exist"})
        assert resp.status_code == 400
        assert "unknown alias" in resp.json()["detail"]

    def test_rejects_type_mismatch(self, client, admin_user):
        # az-vlm is type=vlm — pointing the llm fallback at it should 400.
        resp = self._put(client, admin_user, {"llm": "az-vlm"})
        assert resp.status_code == 400
        assert "type" in resp.json()["detail"]

    def test_rejects_non_string_value(self, client, admin_user):
        resp = self._put(client, admin_user, {"llm": 42})
        assert resp.status_code == 400


class TestHiddenModels:
    """Hidden models are only hidden from web pages, NOT from /v1/models API."""

    def _routing_with_hidden(self):
        """Return a copy of TEST_MODEL_ROUTING with test-llm marked hidden."""
        routing = {k: dict(v) for k, v in TEST_MODEL_ROUTING.items()}
        routing["test-llm"]["hidden"] = True
        return routing

    def test_hidden_model_still_in_api_list(self, client, test_user):
        """Hidden models should still appear in /v1/models — the hidden flag
        only affects user-facing web pages (welcome, dashboard)."""
        routing = self._routing_with_hidden()
        with patch("app.routers.v1_api.get_model_routing_snapshot", return_value=routing):
            resp = client.get("/v1/models", headers=auth_header())
        assert resp.status_code == 200
        ids = [m["id"] for m in resp.json()["data"]]
        assert "test-llm" in ids
        assert "test-vlm" in ids

    def test_hidden_field_not_surfaced_in_response(self, client, test_user):
        """The 'hidden' key should never appear in the /v1/models response
        (it's an internal config field, not client-facing metadata)."""
        resp = client.get("/v1/models", headers=auth_header())
        for m in resp.json()["data"]:
            assert "hidden" not in m
