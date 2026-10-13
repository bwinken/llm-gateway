"""Tests for daily-limit enforcement in deps.py.

Every account has a limit: ``daily_limit_usd = 0`` is a $0 budget (every
request refused), not "unlimited". The only escape hatch is gateway-wide:
``ENFORCE_DAILY_LIMIT=false`` → log-only soft mode.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from sqlmodel import Session

from app.models.schema import UsageLog, User
from tests.conftest import auth_header, make_httpx_response, web_auth_header


def _post_chat(client, headers=None):
    """Helper: send a mock-backed chat completion request."""
    downstream = {
        "id": "c1",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    async def _post(*args, **kwargs):
        return make_httpx_response(200, downstream)

    client.__httpx_mock__.post = _post
    return client.post(
        "/v1/chat/completions",
        json={
            "model": "test-llm",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers=headers or auth_header(),
    )


def _burn_budget(db_session: Session, user: User, amount: Decimal) -> None:
    """Pre-populate usage_logs so the user is already over their limit."""
    db_session.add(
        UsageLog(
            user_id=user.id,
            model="test-llm",
            model_type="llm",
            input_tokens=0,
            output_tokens=0,
            cost_usd=amount,
            endpoint="/v1/chat/completions",
        )
    )
    db_session.commit()


class TestDailyLimitEnforcement:
    """Default behavior: request is 429'd when today's spend >= limit."""

    def test_under_limit_passes(self, client, db_session, test_user):
        _burn_budget(db_session, test_user, Decimal("5.00"))  # < 100.0
        resp = _post_chat(client)
        assert resp.status_code == 200

    def test_at_limit_blocks(self, client, db_session, test_user):
        _burn_budget(db_session, test_user, Decimal("100.00"))  # == limit
        resp = _post_chat(client)
        assert resp.status_code == 429
        assert "Daily spending limit" in resp.json()["detail"]


class TestZeroLimit:
    """daily_limit_usd = 0 is a $0 budget, not "unlimited"."""

    def test_zero_limit_blocks_without_any_spend(self, client, db_session, test_user):
        test_user.daily_limit_usd = 0.0
        db_session.add(test_user)
        db_session.commit()
        resp = _post_chat(client)
        assert resp.status_code == 429
        assert "Daily spending limit is $0" in resp.json()["detail"]

    def test_zero_limit_soft_mode_lets_through(self, client, db_session, test_user, monkeypatch):
        monkeypatch.setenv("ENFORCE_DAILY_LIMIT", "false")
        test_user.daily_limit_usd = 0.0
        db_session.add(test_user)
        db_session.commit()
        assert _post_chat(client).status_code == 200


class TestGatewaySoftMode:
    """ENFORCE_DAILY_LIMIT=false → log-only, never blocks."""

    def test_soft_mode_lets_overage_through(self, client, db_session, test_user, monkeypatch):
        monkeypatch.setenv("ENFORCE_DAILY_LIMIT", "false")
        _burn_budget(db_session, test_user, Decimal("100.00"))
        resp = _post_chat(client)
        assert resp.status_code == 200

    def test_soft_mode_case_insensitive(self, client, db_session, test_user, monkeypatch):
        for value in ("False", "FALSE", "0", "no", "off"):
            monkeypatch.setenv("ENFORCE_DAILY_LIMIT", value)
            resp = _post_chat(client)
            assert resp.status_code == 200, f"value={value!r} should disable enforcement"

    def test_strict_by_default(self, client, db_session, test_user, monkeypatch):
        monkeypatch.delenv("ENFORCE_DAILY_LIMIT", raising=False)
        _burn_budget(db_session, test_user, Decimal("100.00"))
        resp = _post_chat(client)
        assert resp.status_code == 429

    def test_explicit_true_still_enforces(self, client, db_session, test_user, monkeypatch):
        monkeypatch.setenv("ENFORCE_DAILY_LIMIT", "true")
        _burn_budget(db_session, test_user, Decimal("100.00"))
        resp = _post_chat(client)
        assert resp.status_code == 429


class TestAdminLimitValidation:
    """Limits are finite numbers >= 0: negative / inf / nan would quietly
    reintroduce "unlimited", so admin writes reject them."""

    @staticmethod
    def _hdr(admin_user):
        return web_auth_header(sub=admin_user.username, scopes=["admin"])

    def test_web_form_rejects_negative(self, client, admin_user, test_user):
        resp = client.post(
            f"/admin/users/{test_user.id}/limit", data={"new_limit": "-1"},
            headers=self._hdr(admin_user), follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_web_form_accepts_zero(self, client, db_session, admin_user, test_user):
        resp = client.post(
            f"/admin/users/{test_user.id}/limit", data={"new_limit": "0"},
            headers=self._hdr(admin_user), follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        assert db_session.get(User, test_user.id).daily_limit_usd == 0.0

    def test_api_rejects_inf_and_nan(self, client, admin_user, test_user):
        for raw in ("inf", "nan", "-0.5", "abc", True):
            resp = client.patch(
                f"/admin/users/{test_user.id}", json={"daily_limit_usd": raw},
                headers=self._hdr(admin_user),
            )
            assert resp.status_code == 400, raw
        resp = client.patch(
            f"/admin/users/{test_user.id}", json={"bedrock_daily_limit_usd": "inf"},
            headers=self._hdr(admin_user),
        )
        assert resp.status_code == 400

    def test_api_create_rejects_negative(self, client, admin_user):
        resp = client.post(
            "/admin/users", json={"username": "neg", "daily_limit_usd": -5},
            headers=self._hdr(admin_user),
        )
        assert resp.status_code == 400

    def test_default_limit_rejects_inf(self, client, admin_user):
        with patch("app.routers.admin.set_default_daily_limit") as saved:
            resp = client.post(
                "/admin/default-limit", data={"new_default": "inf"},
                headers=self._hdr(admin_user), follow_redirects=False,
            )
        assert resp.status_code == 400
        saved.assert_not_called()
