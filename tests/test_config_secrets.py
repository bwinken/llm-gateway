"""GET /admin/api/config never sends model secrets to the browser.

A set ``api_key`` / ``aws_secret_access_key`` goes out as a reference
(``<stored:section/alias/field>``); PUT swaps references back for the stored
values, so an unchanged editor round trip keeps every secret, a typed value
replaces it, and a rename or duplicate (which copies the reference) keeps
the secret it pointed at.
"""

from __future__ import annotations

import copy
import json
from unittest.mock import patch

import pytest

from tests.conftest import TEST_MODEL_ROUTING, web_auth_header

SECRETS = ("azure-test-key", "bedrock-test-key", "VLLM_API_KEY")


@pytest.fixture(autouse=True)
def _vllm_models():
    """GET /admin/api/config reads app.core.config.MODEL_ROUTING, which the
    conftest leaves on the example config. Use a copy of the test models and
    no auto-reload (a reload refills the dict in place)."""
    with (
        patch("app.core.config.MODEL_ROUTING", copy.deepcopy(TEST_MODEL_ROUTING)),
        patch("app.core.config._check_auto_reload"),
    ):
        yield


def _hdr(admin):
    return web_auth_header(sub=admin.username, scopes=["admin"])


def _get(client, admin) -> dict:
    resp = client.get("/admin/api/config", headers=_hdr(admin))
    assert resp.status_code == 200
    return resp.json()


def _put(client, admin, body: dict):
    with patch("app.routers.admin.save_config") as save:
        resp = client.put("/admin/api/config", json=body, headers=_hdr(admin))
    return resp, save


def _saved(save) -> dict:
    """The sections save_config was called with (positional, see save_config_api)."""
    args = save.call_args.args
    return {"models": args[0], "azure_models": args[3], "bedrock_models": args[5]}


class TestMasking:
    def test_no_secret_reaches_the_browser(self, client, admin_user):
        text = client.get("/admin/api/config", headers=_hdr(admin_user)).text
        for secret in SECRETS:
            assert secret not in text

    def test_set_secrets_become_references_and_empty_stay_empty(self, client, admin_user):
        data = _get(client, admin_user)
        assert data["azure_models"]["azure-gpt-4"]["api_key"] == "<stored:azure_models/azure-gpt-4/api_key>"
        assert data["bedrock_models"]["bedrock-claude"]["api_key"] == "<stored:bedrock_models/bedrock-claude/api_key>"
        assert data["models"]["test-llm"]["api_key"] == "<stored:models/test-llm/api_key>"
        unset = [a for a, m in data["bedrock_models"].items() if not m.get("aws_secret_access_key")]
        assert all(data["bedrock_models"][a].get("aws_secret_access_key", "") == "" for a in unset)

    def test_non_secret_fields_are_untouched(self, client, admin_user):
        data = _get(client, admin_user)
        assert data["azure_models"]["azure-gpt-4"]["endpoint"] == "https://test.openai.azure.com"


class TestRoundTrip:
    def test_unchanged_round_trip_keeps_every_secret(self, client, admin_user):
        resp, save = _put(client, admin_user, _get(client, admin_user))
        assert resp.status_code == 200, resp.text
        saved = _saved(save)
        assert saved["azure_models"]["azure-gpt-4"]["api_key"] == "azure-test-key"
        assert saved["bedrock_models"]["bedrock-claude"]["api_key"] == "bedrock-test-key"
        assert saved["models"]["test-llm"]["api_key"] == "VLLM_API_KEY"

    def test_a_typed_value_replaces_the_secret(self, client, admin_user):
        body = _get(client, admin_user)
        body["azure_models"]["azure-gpt-4"]["api_key"] = "new-azure-key"
        resp, save = _put(client, admin_user, body)
        assert resp.status_code == 200, resp.text
        assert _saved(save)["azure_models"]["azure-gpt-4"]["api_key"] == "new-azure-key"

    def test_rename_and_duplicate_keep_the_referenced_secret(self, client, admin_user):
        body = _get(client, admin_user)
        entry = body["azure_models"].pop("azure-gpt-4")
        body["azure_models"]["azure-gpt-4-renamed"] = entry
        body["azure_models"]["azure-gpt-4-copy"] = copy.deepcopy(entry)
        resp, save = _put(client, admin_user, body)
        assert resp.status_code == 200, resp.text
        saved = _saved(save)["azure_models"]
        assert saved["azure-gpt-4-renamed"]["api_key"] == "azure-test-key"
        assert saved["azure-gpt-4-copy"]["api_key"] == "azure-test-key"

    def test_reference_to_nothing_is_refused(self, client, admin_user):
        body = _get(client, admin_user)
        body["azure_models"]["azure-gpt-4"]["api_key"] = "<stored:azure_models/gone/api_key>"
        resp, save = _put(client, admin_user, body)
        assert resp.status_code == 400
        assert "enter it again" in resp.json()["detail"]
        save.assert_not_called()

    def test_reference_to_a_non_secret_field_is_refused(self, client, admin_user):
        body = _get(client, admin_user)
        body["azure_models"]["azure-gpt-4"]["api_key"] = "<stored:azure_models/azure-gpt-4/endpoint>"
        resp, save = _put(client, admin_user, body)
        assert resp.status_code == 400
        save.assert_not_called()


class TestTestConnection:
    def test_resolves_the_reference_before_probing(self, client, admin_user):
        from unittest.mock import AsyncMock, MagicMock

        fake = MagicMock()
        fake.get = AsyncMock(return_value=MagicMock(
            status_code=200, json=lambda: {"data": [{"id": "real-llm-v1"}]},
            text=json.dumps({"data": [{"id": "real-llm-v1"}]}),
        ))
        with patch("app.core.server_state.get_health_client", return_value=fake):
            resp = client.post(
                "/admin/api/test-model",
                json={"base_url": "http://mock-llm:8000/v1", "type": "llm", "real_model": "real-llm-v1",
                      "api_key": "<stored:models/test-llm/api_key>"},
                headers=_hdr(admin_user),
            )
        assert resp.status_code == 200, resp.text
        sent = fake.get.call_args.kwargs["headers"]
        assert sent == {"Authorization": "Bearer VLLM_API_KEY"}
