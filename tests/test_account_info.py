"""Tests for ``GET /v1/me``: the caller's own access and today's budget."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from sqlmodel import Session

from app.models.schema import UsageLog, User
from tests.conftest import auth_header


def _spend(db_session: Session, user: User, amount: str, backend: str = "vllm", **kwargs) -> None:
    db_session.add(
        UsageLog(
            user_id=user.id,
            model="test-llm",
            model_type="llm",
            input_tokens=10,
            output_tokens=5,
            cost_usd=Decimal(amount),
            endpoint="/v1/chat/completions",
            backend=backend,
            **kwargs,
        )
    )
    db_session.commit()


def _update(db_session: Session, user: User, **fields) -> None:
    for key, value in fields.items():
        setattr(user, key, value)
    db_session.add(user)
    db_session.commit()


def _no_nulls(value) -> bool:
    if isinstance(value, dict):
        return all(_no_nulls(v) for v in value.values())
    if isinstance(value, list):
        return all(_no_nulls(v) for v in value)
    return value is not None


def _me(client, path: str = "/v1/me", headers=None):
    resp = client.get(path, headers=headers or auth_header())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # "No limit" is spelled unlimited=true, never a null a client could
    # misread as zero (or a missing sub-limit misread as "unlimited").
    assert _no_nulls(body), body
    return body


class TestAccountInfo:
    def test_fresh_user(self, client, test_user):
        body = _me(client)
        assert body["object"] == "account"
        assert body["username"] == "testuser"
        assert body["is_admin"] is False
        daily = body["daily"]
        assert daily["unlimited"] is False
        assert daily["limit_usd"] == 100.0
        assert daily["used_usd"] == 0.0
        assert daily["remaining_usd"] == 100.0
        assert daily["requests"] == 0
        assert daily["enforced"] is True
        assert daily["timezone"] == "Asia/Taipei"
        assert daily["resets_at"].endswith("+08:00")
        assert 0 < daily["resets_in_seconds"] <= 86400
        for name in ("onprem", "azure", "aws"):
            assert body["backends"][name] == {
                "access": True, "unlimited": False, "used_usd": 0.0, "remaining_usd": 100.0,
            }

    def test_unprefixed_alias(self, client, test_user):
        assert _me(client, "/me")["username"] == "testuser"

    def test_spend_split_by_backend(self, client, db_session, test_user):
        _update(db_session, test_user, azure_daily_limit_usd=5.0)
        _spend(db_session, test_user, "10")
        _spend(db_session, test_user, "3", backend="azure")
        _spend(db_session, test_user, "1.5", backend="bedrock")

        body = _me(client)
        daily = body["daily"]
        assert daily["used_usd"] == 14.5
        assert daily["remaining_usd"] == 85.5
        assert daily["requests"] == 3
        assert daily["input_tokens"] == 30
        assert daily["output_tokens"] == 15
        backends = body["backends"]
        assert backends["onprem"] == {
            "access": True, "unlimited": False, "used_usd": 10.0, "remaining_usd": 85.5,
        }
        assert backends["azure"] == {
            "access": True, "sub_limit_usd": 5.0, "unlimited": False,
            "used_usd": 3.0, "remaining_usd": 2.0,
        }
        assert backends["aws"]["used_usd"] == 1.5
        assert backends["aws"]["remaining_usd"] == 85.5

    def test_cloud_remaining_capped_by_overall_limit(self, client, db_session, test_user):
        # Cloud spend counts toward the overall limit too, so a generous
        # sub-limit can't promise more than the overall budget has left.
        _update(db_session, test_user, daily_limit_usd=10.0, bedrock_daily_limit_usd=50.0)
        _spend(db_session, test_user, "8")
        assert _me(client)["backends"]["aws"]["remaining_usd"] == 2.0

    def test_yesterday_not_counted(self, client, db_session, test_user):
        _spend(
            db_session, test_user, "40",
            created_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        assert _me(client)["daily"]["used_usd"] == 0.0

    def test_unlimited(self, client, db_session, test_user):
        _update(db_session, test_user, daily_limit_usd=0.0, azure_daily_limit_usd=0.0)
        _spend(db_session, test_user, "500")
        body = _me(client)
        daily = body["daily"]
        assert daily["unlimited"] is True
        assert "limit_usd" not in daily and "remaining_usd" not in daily
        assert daily["used_usd"] == 500.0
        assert body["backends"]["azure"] == {
            "access": True, "unlimited": True, "used_usd": 0.0,
        }

    def test_sub_limit_caps_an_otherwise_unlimited_user(self, client, db_session, test_user):
        _update(db_session, test_user, daily_limit_usd=0.0, azure_daily_limit_usd=5.0)
        _spend(db_session, test_user, "1", backend="azure")
        body = _me(client)
        assert body["daily"]["unlimited"] is True
        assert body["backends"]["onprem"]["unlimited"] is True
        assert body["backends"]["azure"] == {
            "access": True, "sub_limit_usd": 5.0, "unlimited": False,
            "used_usd": 1.0, "remaining_usd": 4.0,
        }

    def test_no_cloud_access(self, client, db_session, test_user):
        _update(db_session, test_user, can_use_azure=False, can_use_bedrock=False)
        backends = _me(client)["backends"]
        assert backends["onprem"]["access"] is True
        for name in ("azure", "aws"):
            assert backends[name]["access"] is False
            assert backends[name]["unlimited"] is False
            assert backends[name]["remaining_usd"] == 0.0

    def test_admin_bypasses_cloud_flags(self, client, admin_user):
        body = _me(client, headers=auth_header(admin_user.api_key))
        assert body["is_admin"] is True
        assert body["backends"]["azure"]["access"] is True
        assert body["backends"]["aws"]["access"] is True


class TestOverBudget:
    def test_answers_when_daily_limit_spent(self, client, db_session, test_user):
        # Billable endpoints 429 at auth; /v1/me must still answer.
        _spend(db_session, test_user, "120")
        assert client.get("/v1/models", headers=auth_header()).status_code == 429
        daily = _me(client)["daily"]
        assert daily["used_usd"] == 120.0
        assert daily["remaining_usd"] == 0.0

    def test_soft_mode_reported(self, client, test_user, monkeypatch):
        monkeypatch.setenv("ENFORCE_DAILY_LIMIT", "false")
        assert _me(client)["daily"]["enforced"] is False


class TestAuth:
    def test_missing_key(self, client, test_user):
        assert client.get("/v1/me").status_code == 401

    def test_invalid_key(self, client, test_user):
        assert client.get("/v1/me", headers=auth_header("sk-nope")).status_code == 401

    def test_disabled_account(self, client, db_session, test_user):
        _update(db_session, test_user, is_disabled=True)
        assert client.get("/v1/me", headers=auth_header()).status_code == 403

    def test_x_api_key_header(self, client, test_user):
        resp = client.get("/v1/me", headers={"x-api-key": "sk-testkey123"})
        assert resp.status_code == 200


class TestConcurrency:
    def test_off_by_default(self, client, test_user):
        assert _me(client)["concurrency"] == {"mode": "off"}

    def test_enforced_limit_reported(self, client, test_user):
        with patch("app.routers.v1_api.get_concurrency_settings", return_value=("enforce", 4)):
            assert _me(client)["concurrency"] == {"mode": "enforce", "limit": 4}

    def test_waived_user_reports_off(self, client, db_session, test_user):
        _update(db_session, test_user, concurrency_waived=True)
        with patch("app.routers.v1_api.get_concurrency_settings", return_value=("enforce", 4)):
            assert _me(client)["concurrency"] == {"mode": "off"}
