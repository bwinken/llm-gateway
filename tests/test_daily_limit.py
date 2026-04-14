"""Tests for the daily-limit enforcement escape hatches in deps.py.

Two ways to avoid the 429 block:
  1. Per-user: set ``User.daily_limit_usd`` to 0 → unlimited for that user.
  2. Gateway-wide: set ``ENFORCE_DAILY_LIMIT=false`` → log-only soft mode.

Both preserve the default (strict) behavior when unused.
"""

from __future__ import annotations

import os
from decimal import Decimal

from sqlmodel import Session

from app.models.schema import UsageLog, User
from tests.conftest import auth_header, make_httpx_response


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


class TestPerUserUnlimited:
    """daily_limit_usd <= 0 → skip the check entirely for this user."""

    def test_zero_limit_skips_check(self, client, db_session, test_user):
        test_user.daily_limit_usd = 0.0
        db_session.add(test_user)
        db_session.commit()
        _burn_budget(db_session, test_user, Decimal("9999.00"))
        resp = _post_chat(client)
        assert resp.status_code == 200


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
