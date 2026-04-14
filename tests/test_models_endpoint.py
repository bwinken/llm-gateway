"""
Tests for GET /v1/models.

The endpoint surfaces optional per-model metadata declared in config.toml
(context_window, max_output_tokens, supports_*, display_name, ...) so that
OpenAI-compatible clients (Cursor, Cline, Continue.dev, future Claude Code
versions with dynamic discovery, ...) can show accurate capabilities in
their model pickers without us having to hard-code them client-side.
"""

from __future__ import annotations

from tests.conftest import auth_header


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
            assert m["owned_by"] == "llm-gateway"
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
