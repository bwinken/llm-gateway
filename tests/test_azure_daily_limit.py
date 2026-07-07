"""Tests for the Azure/on-prem cost split and the Azure daily sub-limit.

Semantics under test:
  - usage_logs.backend is written as "vllm" for on-prem requests and
    "azure" for Azure-served requests (regardless of which surface —
    /azure/v1/* or the unified /v1/* dispatch — the request entered).
  - users.azure_daily_limit_usd is an *additional* cap on the Azure
    portion of today's spend. Azure spend still counts toward the overall
    daily_limit_usd. NULL / <= 0 → no separate Azure cap (pre-feature
    behavior for every existing user).
  - When exhausted, Azure-bound requests get 429 on BOTH surfaces;
    vLLM-bound requests are unaffected.
  - ENFORCE_DAILY_LIMIT=false (soft mode) applies to the Azure check too.
"""

from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import AsyncMock

from sqlmodel import Session, select

from app.models.schema import UsageLog, User
from tests.conftest import auth_header, make_httpx_response, web_auth_header


def _responses_payload(text: str, in_tk: int, out_tk: int) -> dict:
    """Minimal Azure Responses API non-stream payload."""
    return {
        "id": "resp_test",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {"input_tokens": in_tk, "output_tokens": out_tk, "total_tokens": in_tk + out_tk},
    }


def _mock_azure_ok(client) -> None:
    client.__httpx_mock__.post = AsyncMock(
        return_value=make_httpx_response(200, _responses_payload("ok", 5, 5)),
    )


def _mock_vllm_ok(client) -> None:
    downstream = {
        "id": "c1",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
    }
    client.__httpx_mock__.post = AsyncMock(return_value=make_httpx_response(200, downstream))


def _post_azure_chat(client, path="/azure/v1/chat/completions"):
    return client.post(
        path,
        json={"model": "azure-gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(),
    )


def _post_vllm_chat(client):
    return client.post(
        "/v1/chat/completions",
        json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(),
    )


def _burn(db_session: Session, user: User, amount: Decimal, backend: str) -> None:
    """Pre-populate usage_logs with today's spend on the given backend."""
    db_session.add(
        UsageLog(
            user_id=user.id,
            model="azure-gpt-4" if backend == "azure" else "test-llm",
            model_type="llm",
            input_tokens=0,
            output_tokens=0,
            cost_usd=amount,
            endpoint="/azure/v1/chat/completions" if backend == "azure" else "/v1/chat/completions",
            backend=backend,
        )
    )
    db_session.commit()


def _set_azure_limit(db_session: Session, user: User, limit: float | None) -> None:
    user.azure_daily_limit_usd = limit
    db_session.add(user)
    db_session.commit()


class TestBackendTagging:
    """usage_logs.backend records which downstream served the request."""

    def test_vllm_request_logged_as_vllm(self, client, db_session, test_user):
        _mock_vllm_ok(client)
        assert _post_vllm_chat(client).status_code == 200
        log = db_session.exec(select(UsageLog)).first()
        assert log is not None
        assert log.backend == "vllm"

    def test_azure_surface_logged_as_azure(self, client, db_session, test_user):
        _mock_azure_ok(client)
        assert _post_azure_chat(client).status_code == 200
        log = db_session.exec(select(UsageLog)).first()
        assert log is not None
        assert log.backend == "azure"

    def test_unified_v1_azure_dispatch_logged_as_azure(self, client, db_session, test_user):
        """An Azure alias entering via /v1/chat/completions still logs backend=azure."""
        _mock_azure_ok(client)
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "azure-gpt-4", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        log = db_session.exec(select(UsageLog)).first()
        assert log is not None
        assert log.backend == "azure"


class TestAzureSubLimitDefaultOff:
    """NULL / <= 0 azure_daily_limit_usd → behavior identical to before."""

    def test_null_limit_never_blocks_azure(self, client, db_session, test_user):
        assert test_user.azure_daily_limit_usd is None  # model default
        _burn(db_session, test_user, Decimal("50.00"), "azure")  # any azure spend
        _mock_azure_ok(client)
        assert _post_azure_chat(client).status_code == 200

    def test_zero_limit_never_blocks_azure(self, client, db_session, test_user):
        _set_azure_limit(db_session, test_user, 0.0)
        _burn(db_session, test_user, Decimal("50.00"), "azure")
        _mock_azure_ok(client)
        assert _post_azure_chat(client).status_code == 200


class TestUsersWithoutAzureAccess:
    """Users without can_use_azure never touch the sub-limit machinery —
    their behavior is byte-for-byte what it was before the feature,
    even in the odd state where an admin set a sub-limit anyway."""

    def _revoke_azure(self, db_session: Session, user: User) -> None:
        user.can_use_azure = False
        db_session.add(user)
        db_session.commit()

    def test_azure_surface_still_403_not_429(self, client, db_session, test_user):
        """Permission check runs BEFORE the sub-limit check: a user without
        access gets 403 (as today), never a 429 that would leak limit state."""
        self._revoke_azure(db_session, test_user)
        _set_azure_limit(db_session, test_user, 10.0)
        _burn(db_session, test_user, Decimal("10.00"), "azure")
        resp = _post_azure_chat(client)
        assert resp.status_code == 403
        assert "Azure access not granted" in resp.json()["detail"]

    def test_unified_v1_still_falls_back_to_vllm(self, client, db_session, test_user):
        """An Azure alias from a user without access keeps falling back to
        the vLLM default (the gateway's longstanding stance) — the sub-limit
        never enters the picture, even when exhausted."""
        self._revoke_azure(db_session, test_user)
        _set_azure_limit(db_session, test_user, 10.0)
        _burn(db_session, test_user, Decimal("10.00"), "azure")
        _mock_vllm_ok(client)
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "azure-gpt-4", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        # Served by vLLM, logged as vllm.
        logs = db_session.exec(select(UsageLog).where(UsageLog.endpoint == "/v1/chat/completions")).all()
        assert logs and all(l.backend == "vllm" for l in logs)

    def test_vllm_requests_unaffected(self, client, db_session, test_user):
        self._revoke_azure(db_session, test_user)
        _set_azure_limit(db_session, test_user, 10.0)
        _burn(db_session, test_user, Decimal("10.00"), "azure")
        _mock_vllm_ok(client)
        assert _post_vllm_chat(client).status_code == 200


class TestAzureSubLimitEnforcement:
    def test_under_azure_limit_passes(self, client, db_session, test_user):
        _set_azure_limit(db_session, test_user, 10.0)
        _burn(db_session, test_user, Decimal("5.00"), "azure")
        _mock_azure_ok(client)
        assert _post_azure_chat(client).status_code == 200

    def test_at_azure_limit_blocks_azure_surface(self, client, db_session, test_user):
        _set_azure_limit(db_session, test_user, 10.0)
        _burn(db_session, test_user, Decimal("10.00"), "azure")
        resp = _post_azure_chat(client)
        assert resp.status_code == 429
        assert "Azure daily spending limit" in resp.json()["detail"]

    def test_at_azure_limit_blocks_unified_v1_dispatch(self, client, db_session, test_user):
        _set_azure_limit(db_session, test_user, 10.0)
        _burn(db_session, test_user, Decimal("10.00"), "azure")
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "azure-gpt-4", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 429
        assert "Azure daily spending limit" in resp.json()["detail"]

    def test_at_azure_limit_blocks_unified_v1_messages(self, client, db_session, test_user):
        _set_azure_limit(db_session, test_user, 10.0)
        _burn(db_session, test_user, Decimal("10.00"), "azure")
        resp = client.post(
            "/v1/messages",
            json={
                "model": "azure-gpt-4",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=auth_header(),
        )
        assert resp.status_code == 429

    def test_vllm_spend_does_not_count_toward_azure_limit(self, client, db_session, test_user):
        """Only backend='azure' rows are summed for the sub-limit."""
        _set_azure_limit(db_session, test_user, 10.0)
        _burn(db_session, test_user, Decimal("50.00"), "vllm")  # on-prem spend
        _mock_azure_ok(client)
        assert _post_azure_chat(client).status_code == 200

    def test_azure_exhausted_leaves_vllm_path_untouched(self, client, db_session, test_user):
        """Azure over-budget must not affect on-prem requests."""
        _set_azure_limit(db_session, test_user, 10.0)
        _burn(db_session, test_user, Decimal("10.00"), "azure")
        _mock_vllm_ok(client)
        assert _post_vllm_chat(client).status_code == 200

    def test_azure_spend_still_counts_toward_overall_limit(self, client, db_session, test_user):
        """Azure spend is a subset of the overall budget, not a parallel one."""
        # test_user fixture has daily_limit_usd=100 and no azure sub-limit.
        _burn(db_session, test_user, Decimal("100.00"), "azure")
        resp = _post_vllm_chat(client)
        assert resp.status_code == 429
        assert "Daily spending limit" in resp.json()["detail"]


class TestAzureSubLimitSoftMode:
    """ENFORCE_DAILY_LIMIT=false → log-only, no 429 (same as overall check)."""

    def test_soft_mode_lets_azure_request_through(self, client, db_session, test_user):
        _set_azure_limit(db_session, test_user, 10.0)
        _burn(db_session, test_user, Decimal("10.00"), "azure")
        _mock_azure_ok(client)
        os.environ["ENFORCE_DAILY_LIMIT"] = "false"
        try:
            assert _post_azure_chat(client).status_code == 200
        finally:
            os.environ.pop("ENFORCE_DAILY_LIMIT", None)


class TestAdminAzureLimitEndpoint:
    @staticmethod
    def _get_user(db_session: Session, user_id: int) -> User:
        db_session.expire_all()
        return db_session.exec(select(User).where(User.id == user_id)).one()

    def test_set_azure_limit(self, client, db_session, admin_user, test_user):
        resp = client.post(
            f"/admin/users/{test_user.id}/azure-limit",
            data={"new_azure_limit": "3.5"},
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert self._get_user(db_session, test_user.id).azure_daily_limit_usd == 3.5

    def test_clear_azure_limit_with_empty_value(self, client, db_session, admin_user, test_user):
        test_user.azure_daily_limit_usd = 5.0
        db_session.add(test_user)
        db_session.commit()
        resp = client.post(
            f"/admin/users/{test_user.id}/azure-limit",
            data={"new_azure_limit": ""},
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert self._get_user(db_session, test_user.id).azure_daily_limit_usd is None

    def test_non_numeric_value_is_400(self, client, db_session, admin_user, test_user):
        resp = client.post(
            f"/admin/users/{test_user.id}/azure-limit",
            data={"new_azure_limit": "abc"},
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_patch_users_api_accepts_azure_limit(self, client, db_session, admin_user, test_user):
        resp = client.patch(
            f"/admin/users/{test_user.id}",
            json={"azure_daily_limit_usd": 7.25},
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
        )
        assert resp.status_code == 200
        assert self._get_user(db_session, test_user.id).azure_daily_limit_usd == 7.25
