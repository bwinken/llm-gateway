"""Admin → Model Config "Test connection" (POST /admin/api/test-model):
probes a local (vLLM) server with the editor's values, read-only."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx

from tests.conftest import make_httpx_response, web_auth_header


def _probe(client, body, *, reply=None, side_effect=None):
    probe = AsyncMock(spec=httpx.AsyncClient)
    probe.get = AsyncMock(return_value=reply, side_effect=side_effect)
    with patch("app.core.server_state.get_health_client", return_value=probe):
        resp = client.post("/admin/api/test-model", json=body,
                           headers=web_auth_header(sub="admin", scopes=["admin"]))
    return resp, probe


_BODY = {"base_url": "http://vllm:8000/v1/", "api_key": "k1", "real_model": "Qwen/Qwen3-8B", "type": "llm"}


def test_model_served(client, admin_user):
    reply = make_httpx_response(200, {"data": [{"id": "Qwen/Qwen3-8B"}]})
    resp, probe = _probe(client, _BODY, reply=reply)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True and "serves Qwen/Qwen3-8B" in resp.json()["detail"]
    args, kwargs = probe.get.call_args
    assert args[0] == "http://vllm:8000/v1/models"
    assert kwargs["headers"] == {"Authorization": "Bearer k1"}


def test_model_not_served(client, admin_user):
    reply = make_httpx_response(200, {"data": [{"id": "other"}]})
    resp, _ = _probe(client, _BODY, reply=reply)
    data = resp.json()
    assert data["ok"] is False and "not among" in data["detail"] and data["served_models"] == ["other"]


def test_bad_key(client, admin_user):
    resp, _ = _probe(client, _BODY, reply=make_httpx_response(401, {"error": "no"}))
    assert resp.json()["ok"] is False and "API key" in resp.json()["detail"]


def test_unreachable(client, admin_user):
    resp, _ = _probe(client, _BODY, side_effect=httpx.ConnectError("refused"))
    assert resp.json()["ok"] is False and "ConnectError" in resp.json()["detail"]


def test_rejects_non_http_url(client, admin_user):
    resp, probe = _probe(client, dict(_BODY, base_url="file:///etc/passwd"))
    assert resp.status_code == 400
    probe.get.assert_not_called()


def test_requires_admin(client):
    resp = client.post("/admin/api/test-model", json=_BODY, headers=web_auth_header(sub="testuser"))
    assert resp.status_code in (401, 403)
