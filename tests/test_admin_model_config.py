"""PUT /admin/api/config — Bedrock entries and cross-backend alias checks.

The Model Config page edits all three backends, so the endpoint accepts
`bedrock_models` / `bedrock_fallback` (passed through to save_config) and
refuses an alias that two backends share before anything is written —
_build_config would refuse to load such a file, but only after save_config
had already persisted it.
"""

from __future__ import annotations

from unittest.mock import patch

from tests.conftest import web_auth_header

PRICING = {"_default": {"input_price_per_1m": 0.1, "output_price_per_1m": 0.1}}
VLLM = {"m1": {"real_model": "rm", "base_url": "http://mock:8000/v1", "api_key": "", "type": "llm"}}
AZURE = {"az1": {"type": "llm", "endpoint": "https://x.openai.azure.com", "deployment": "d", "api_key": "k"}}
BEDROCK = {
    "br1": {"type": "llm", "region": "us-east-1", "model_id": "anthropic.x-v1:0", "api_key": "k",
            "rate_limit_fallback": "br2"},
    "br2": {"type": "llm", "region": "us-east-1", "model_id": "anthropic.y-v1:0", "api_key": "k"},
}


def _put(client, admin_user, **body):
    payload = {"models": VLLM, "pricing": PRICING, "fallback": {}, **body}
    with patch("app.routers.admin.save_config") as save:
        resp = client.put(
            "/admin/api/config",
            json=payload,
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
        )
    return resp, save


class TestBedrockSave:
    def test_bedrock_models_passed_to_save_config(self, client, admin_user):
        resp, save = _put(client, admin_user, azure_models=AZURE, azure_fallback={},
                          bedrock_models=BEDROCK, bedrock_fallback={"llm": "br2"})
        assert resp.status_code == 200, resp.text
        args = save.call_args.args
        assert args[5] == BEDROCK
        assert args[6] == {"llm": "br2"}

    def test_omitting_bedrock_leaves_it_untouched(self, client, admin_user):
        resp, save = _put(client, admin_user, azure_models=AZURE)
        assert resp.status_code == 200
        assert save.call_args.args[5] is None and save.call_args.args[6] is None

    def test_bedrock_missing_model_id_rejected(self, client, admin_user):
        bad = {"br1": {"type": "llm", "region": "us-east-1", "model_id": "", "api_key": "k"}}
        resp, save = _put(client, admin_user, bedrock_models=bad)
        assert resp.status_code == 400
        assert "model_id" in resp.json()["detail"]
        save.assert_not_called()

    def test_bedrock_rate_limit_target_must_exist(self, client, admin_user):
        bad = {"br1": {**BEDROCK["br1"], "rate_limit_fallback": "az1"}}
        resp, _ = _put(client, admin_user, azure_models=AZURE, bedrock_models=bad)
        assert resp.status_code == 400
        assert "az1" in resp.json()["detail"]

    def test_bedrock_fallback_must_match_type(self, client, admin_user):
        models = {**BEDROCK, "br3": {**BEDROCK["br2"], "type": "vlm"}}
        resp, _ = _put(client, admin_user, bedrock_models=models, bedrock_fallback={"llm": "br3"})
        assert resp.status_code == 400

    def test_bedrock_metadata_type_checked(self, client, admin_user):
        bad = {"br1": {**BEDROCK["br2"], "context_window": "big"}}
        resp, _ = _put(client, admin_user, bedrock_models=bad)
        assert resp.status_code == 400


class TestAliasUniqueness:
    def test_vllm_and_azure_collision_rejected(self, client, admin_user):
        resp, save = _put(client, admin_user, azure_models={"m1": AZURE["az1"]})
        assert resp.status_code == 400
        assert "m1" in resp.json()["detail"]
        save.assert_not_called()

    def test_azure_and_bedrock_collision_rejected(self, client, admin_user):
        resp, save = _put(client, admin_user, azure_models=AZURE,
                          bedrock_models={"az1": BEDROCK["br2"]})
        assert resp.status_code == 400
        save.assert_not_called()
