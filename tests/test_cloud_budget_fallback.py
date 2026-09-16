"""Tests for the cloud-budget fallback (``[app].cloud_budget_fallback``).

Semantics under test:
  - Default (flag off): an exhausted Azure/Bedrock daily sub-limit on the
    unified ``/v1/*`` surface is a 429 — unchanged.
  - Flag on: the same request is served by the on-prem vLLM default
    instead (``_resolve_model`` fallback), tagged with ``X-Budget-Fallback``
    and billed as ``backend="vllm"``.
  - The overall ``daily_limit_usd`` is untouched by the flag: once that is
    exhausted too, the request is refused with the usual 429.
  - The dedicated ``/azure/v1/*`` / ``/aws/v1/*`` surfaces never fall back.
  - Under the sub-limit nothing changes (request stays on the cloud backend).
  - ENFORCE_DAILY_LIMIT=false (soft mode) keeps the request on the cloud
    backend — there is no 429 for the fallback to replace.
  - Admin: ``POST /admin/cloud-budget-fallback`` persists the flag and the
    admin page renders its state.
"""

from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import patch

from sqlmodel import Session, select

from app.core.config import get_cloud_budget_fallback
from app.models.schema import UsageLog, User
from tests.conftest import auth_header, make_httpx_response, web_auth_header

_FLAG_ON = {"cloud_budget_fallback": True}
_FLAG_OFF = {"cloud_budget_fallback": False}


def _burn(db_session: Session, user: User, amount: Decimal, backend: str) -> None:
    db_session.add(
        UsageLog(
            user_id=user.id,
            model={"azure": "azure-gpt-4", "bedrock": "bedrock-claude"}.get(backend, "test-llm"),
            model_type="llm",
            input_tokens=0,
            output_tokens=0,
            cost_usd=amount,
            endpoint="/v1/chat/completions",
            backend=backend,
        )
    )
    db_session.commit()


def _exhaust_azure(db_session: Session, user: User) -> None:
    user.azure_daily_limit_usd = 10.0
    db_session.add(user)
    db_session.commit()
    _burn(db_session, user, Decimal("10.00"), "azure")


def _exhaust_bedrock(db_session: Session, user: User) -> None:
    user.bedrock_daily_limit_usd = 10.0
    db_session.add(user)
    db_session.commit()
    _burn(db_session, user, Decimal("10.00"), "bedrock")


def _mock_downstream(client) -> dict:
    """One mock for every downstream POST; records the URL so a test can
    assert which backend actually served the call."""
    captured: dict = {}

    async def fake_post(url, **kwargs):
        captured["url"] = url
        if url.endswith("/tokenize"):
            return make_httpx_response(200, {"count": 3, "tokens": [1, 2, 3]})
        if "/openai/v1/responses" in url:
            return make_httpx_response(200, {
                "id": "resp_test",
                "status": "completed",
                "output": [{"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": "azure"}]}],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            })
        return make_httpx_response(200, {
            "id": "cmpl-1",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "on-prem"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    client.__httpx_mock__.post = fake_post
    return captured


def _chat(client, model="azure-gpt-4", path="/v1/chat/completions"):
    return client.post(
        path,
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(),
    )


def _messages(client, model="azure-gpt-4", path="/v1/messages"):
    return client.post(
        path,
        json={"model": model, "max_tokens": 10,
              "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(),
    )


class TestFlagOffIsUnchanged:
    def test_default_is_off(self):
        with patch.dict("app.core.config.APP_CONFIG", {}, clear=True):
            assert get_cloud_budget_fallback() is False

    def test_azure_exhausted_is_429(self, client, db_session, test_user):
        _exhaust_azure(db_session, test_user)
        with patch.dict("app.core.config.APP_CONFIG", _FLAG_OFF):
            resp = _chat(client)
        assert resp.status_code == 429
        assert "Azure daily spending limit" in resp.json()["detail"]
        assert "X-Budget-Fallback" not in resp.headers

    def test_bedrock_exhausted_is_429(self, client, db_session, test_user):
        _exhaust_bedrock(db_session, test_user)
        with patch.dict("app.core.config.APP_CONFIG", _FLAG_OFF):
            resp = _messages(client, model="bedrock-claude")
        assert resp.status_code == 429
        assert "Bedrock daily spending limit" in resp.json()["detail"]


class TestFlagOnFallsBackToOnPrem:
    def test_azure_chat_served_by_vllm(self, client, db_session, test_user):
        _exhaust_azure(db_session, test_user)
        captured = _mock_downstream(client)
        with patch.dict("app.core.config.APP_CONFIG", _FLAG_ON):
            resp = _chat(client)
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "on-prem"
        assert "/openai/v1/responses" not in captured["url"]
        assert "azure" in resp.headers["X-Budget-Fallback"]
        # The vLLM resolver still reports the alias → default rewrite.
        assert resp.headers.get("X-Model-Fallback")

    def test_fallback_request_is_billed_as_vllm(self, client, db_session, test_user):
        _exhaust_azure(db_session, test_user)
        _mock_downstream(client)
        with patch.dict("app.core.config.APP_CONFIG", _FLAG_ON):
            assert _chat(client).status_code == 200
        rows = db_session.exec(
            select(UsageLog).where(UsageLog.user_id == test_user.id).order_by(UsageLog.id.desc())
        ).all()
        assert rows[0].backend == "vllm"

    def test_bedrock_messages_served_by_vllm(self, client, db_session, test_user):
        _exhaust_bedrock(db_session, test_user)
        captured = _mock_downstream(client)
        with patch.dict("app.core.config.APP_CONFIG", _FLAG_ON):
            resp = _messages(client, model="bedrock-claude")
        assert resp.status_code == 200
        assert "bedrock-runtime" not in captured["url"]
        assert resp.json()["content"][0]["text"] == "on-prem"
        assert "bedrock" in resp.headers["X-Budget-Fallback"]

    def test_azure_messages_served_by_vllm(self, client, db_session, test_user):
        _exhaust_azure(db_session, test_user)
        captured = _mock_downstream(client)
        with patch.dict("app.core.config.APP_CONFIG", _FLAG_ON):
            resp = _messages(client)
        assert resp.status_code == 200
        assert "/openai/v1/responses" not in captured["url"]
        assert "azure" in resp.headers["X-Budget-Fallback"]

    def test_azure_responses_served_by_vllm(self, client, db_session, test_user):
        _exhaust_azure(db_session, test_user)
        captured = _mock_downstream(client)
        with patch.dict("app.core.config.APP_CONFIG", _FLAG_ON):
            resp = client.post(
                "/v1/responses",
                json={"model": "azure-gpt-4", "input": "hi"},
                headers=auth_header(),
            )
        assert resp.status_code == 200
        assert "/openai/v1/responses" not in captured["url"]
        assert "azure" in resp.headers["X-Budget-Fallback"]

    def test_count_tokens_uses_onprem_tokenizer(self, client, db_session, test_user):
        _exhaust_azure(db_session, test_user)
        captured = _mock_downstream(client)
        with patch.dict("app.core.config.APP_CONFIG", _FLAG_ON):
            resp = client.post(
                "/v1/messages/count_tokens",
                json={"model": "azure-gpt-4", "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )
        assert resp.status_code == 200
        assert resp.json()["input_tokens"] == 3
        assert captured["url"].endswith("/tokenize")
        assert "azure" in resp.headers["X-Budget-Fallback"]

    def test_under_sublimit_stays_on_cloud(self, client, db_session, test_user):
        test_user.azure_daily_limit_usd = 10.0
        db_session.add(test_user)
        db_session.commit()
        _burn(db_session, test_user, Decimal("5.00"), "azure")
        captured = _mock_downstream(client)
        with patch.dict("app.core.config.APP_CONFIG", _FLAG_ON):
            resp = _chat(client)
        assert resp.status_code == 200
        assert "/openai/v1/responses" in captured["url"]
        assert "X-Budget-Fallback" not in resp.headers

    def test_no_sublimit_stays_on_cloud(self, client, db_session, test_user):
        """No sub-limit configured → nothing to exhaust → cloud as usual."""
        _burn(db_session, test_user, Decimal("50.00"), "azure")
        captured = _mock_downstream(client)
        with patch.dict("app.core.config.APP_CONFIG", _FLAG_ON):
            resp = _chat(client)
        assert resp.status_code == 200
        assert "/openai/v1/responses" in captured["url"]


class TestOverallLimitStillRefuses:
    """'On-prem 也用完' — once the overall daily limit is gone, no fallback."""

    def test_overall_exhausted_is_429_even_with_flag(self, client, db_session, test_user):
        # test_user: daily_limit_usd=100. Azure sub-limit 10, spent 10 on
        # Azure and 90 on-prem → overall 100 reached.
        _exhaust_azure(db_session, test_user)
        _burn(db_session, test_user, Decimal("90.00"), "vllm")
        _mock_downstream(client)
        with patch.dict("app.core.config.APP_CONFIG", _FLAG_ON):
            resp = _chat(client)
        assert resp.status_code == 429
        assert "Daily spending limit" in resp.json()["detail"]
        assert "Retry-After" in resp.headers


class TestDedicatedSurfacesNeverFallBack:
    def test_azure_surface_still_429(self, client, db_session, test_user):
        _exhaust_azure(db_session, test_user)
        _mock_downstream(client)
        with patch.dict("app.core.config.APP_CONFIG", _FLAG_ON):
            resp = _chat(client, path="/azure/v1/chat/completions")
        assert resp.status_code == 429
        assert "Azure daily spending limit" in resp.json()["detail"]

    def test_aws_surface_still_429(self, client, db_session, test_user):
        _exhaust_bedrock(db_session, test_user)
        _mock_downstream(client)
        with patch.dict("app.core.config.APP_CONFIG", _FLAG_ON):
            resp = _chat(client, model="bedrock-claude", path="/aws/v1/chat/completions")
        assert resp.status_code == 429
        assert "Bedrock daily spending limit" in resp.json()["detail"]


class TestSoftMode:
    def test_soft_mode_stays_on_cloud(self, client, db_session, test_user):
        """ENFORCE_DAILY_LIMIT=false never 429s, so there is nothing to
        replace — the request keeps going to Azure (logged as before)."""
        _exhaust_azure(db_session, test_user)
        captured = _mock_downstream(client)
        os.environ["ENFORCE_DAILY_LIMIT"] = "false"
        try:
            with patch.dict("app.core.config.APP_CONFIG", _FLAG_ON):
                resp = _chat(client)
        finally:
            os.environ.pop("ENFORCE_DAILY_LIMIT", None)
        assert resp.status_code == 200
        assert "/openai/v1/responses" in captured["url"]
        assert "X-Budget-Fallback" not in resp.headers


class TestConfigParsing:
    def test_string_spellings(self):
        for raw, expected in [
            ("true", True), ("Yes", True), ("1", True), ("on", True),
            ("false", False), ("", False), ("nope", False),
        ]:
            with patch.dict("app.core.config.APP_CONFIG", {"cloud_budget_fallback": raw}):
                assert get_cloud_budget_fallback() is expected, raw

    def test_bool_and_garbage(self):
        with patch.dict("app.core.config.APP_CONFIG", {"cloud_budget_fallback": True}):
            assert get_cloud_budget_fallback() is True
        with patch.dict("app.core.config.APP_CONFIG", {"cloud_budget_fallback": [1]}):
            assert get_cloud_budget_fallback() is False


class TestAdminEndpoint:
    """POST /admin/cloud-budget-fallback (JWT admin auth)."""

    @staticmethod
    def _hdr(admin_user):
        return web_auth_header(sub=admin_user.username, scopes=["admin"])

    def test_enable_form_redirects(self, client, db_session, admin_user):
        with patch("app.routers.admin.set_cloud_budget_fallback") as mock_set:
            resp = client.post(
                "/admin/cloud-budget-fallback",
                data={"enabled": "on"},
                headers=self._hdr(admin_user),
                follow_redirects=False,
            )
        assert resp.status_code == 303
        mock_set.assert_called_once_with(True)

    def test_unchecked_checkbox_disables(self, client, db_session, admin_user):
        """A browser omits an unchecked checkbox from the form body."""
        with patch("app.routers.admin.set_cloud_budget_fallback") as mock_set:
            resp = client.post(
                "/admin/cloud-budget-fallback",
                data={},
                headers=self._hdr(admin_user),
                follow_redirects=False,
            )
        assert resp.status_code == 303
        mock_set.assert_called_once_with(False)

    def test_json_reply_for_in_place_toggle(self, client, db_session, admin_user):
        headers = {**self._hdr(admin_user), "Accept": "application/json"}
        with patch("app.routers.admin.set_cloud_budget_fallback") as mock_set:
            resp = client.post(
                "/admin/cloud-budget-fallback",
                data={"enabled": "on"},
                headers=headers,
                follow_redirects=False,
            )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "cloud_budget_fallback": True}
        mock_set.assert_called_once_with(True)

    def test_non_admin_forbidden(self, client, db_session, test_user):
        with patch("app.routers.admin.set_cloud_budget_fallback") as mock_set:
            resp = client.post(
                "/admin/cloud-budget-fallback",
                data={"enabled": "on"},
                headers=web_auth_header(sub="testuser", scopes=["read"]),
                follow_redirects=False,
            )
        assert resp.status_code == 403
        mock_set.assert_not_called()

    def test_admin_page_renders_state(self, client, db_session, admin_user):
        with patch.dict("app.core.config.APP_CONFIG", _FLAG_ON):
            resp = client.get("/admin", headers=self._hdr(admin_user))
        assert resp.status_code == 200
        assert "Cloud Budget Fallback" in resp.text
        assert 'name="enabled" value="on" checked' in resp.text

        with patch.dict("app.core.config.APP_CONFIG", _FLAG_OFF):
            resp = client.get("/admin", headers=self._hdr(admin_user))
        assert 'name="enabled" value="on" checked' not in resp.text


class TestGrantTogglesAnswerJson:
    """The admin page flips Azure/AWS grants in place: with
    ``Accept: application/json`` the toggle endpoints return the new flag
    instead of the classic 303 back to /admin."""

    @staticmethod
    def _hdr(admin_user):
        return {**web_auth_header(sub=admin_user.username, scopes=["admin"]),
                "Accept": "application/json"}

    def test_toggle_azure_json(self, client, db_session, admin_user, test_user):
        resp = client.post(
            f"/admin/users/{test_user.id}/toggle-azure",
            headers=self._hdr(admin_user), follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "can_use_azure": False}
        db_session.refresh(test_user)
        assert test_user.can_use_azure is False

    def test_toggle_bedrock_json(self, client, db_session, admin_user, test_user):
        resp = client.post(
            f"/admin/users/{test_user.id}/toggle-bedrock",
            headers=self._hdr(admin_user), follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "can_use_bedrock": False}
        db_session.refresh(test_user)
        assert test_user.can_use_bedrock is False

    def test_plain_form_still_redirects(self, client, db_session, admin_user, test_user):
        resp = client.post(
            f"/admin/users/{test_user.id}/toggle-azure",
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
            follow_redirects=False,
        )
        assert resp.status_code == 303
