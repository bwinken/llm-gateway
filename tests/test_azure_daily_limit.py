"""Tests for the Azure/on-prem cost split and Azure access gating.

Semantics under test:
  - usage_logs.backend is written as "vllm" for on-prem requests and
    "azure" for Azure-served requests (regardless of which surface —
    /azure/v1/* or the unified /v1/* dispatch — the request entered).
  - A user without Azure access gets 403 on /azure/v1/* and the vLLM
    fallback on /v1/*, never a cloud-cap 429.

The cloud cap itself (Azure + Bedrock together) is tested in
tests/test_cloud_limit.py.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from sqlmodel import Session, select

from app.models.schema import UsageLog, User
from tests.conftest import auth_header, make_httpx_response


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


class TestUsersWithoutAzureAccess:
    """Users without can_use_azure never touch the cloud-cap machinery, even
    with the cap configured and spent ($10 cap, $10 of Azure spend)."""

    @pytest.fixture(autouse=True)
    def _spent_cloud_cap(self):
        with patch.dict("app.core.config.APP_CONFIG", {"cloud_daily_limit_usd": 10.0}):
            yield

    def _revoke_azure(self, db_session: Session, user: User) -> None:
        user.can_use_azure = False
        db_session.add(user)
        db_session.commit()

    def test_azure_surface_still_403_not_429(self, client, db_session, test_user):
        """Permission check runs BEFORE the cloud-cap check: a user without
        access gets 403 (as today), never a 429 that would leak limit state."""
        self._revoke_azure(db_session, test_user)
        _burn(db_session, test_user, Decimal("10.00"), "azure")
        resp = _post_azure_chat(client)
        assert resp.status_code == 403
        assert "Azure access not granted" in resp.json()["detail"]

    def test_unified_v1_still_falls_back_to_vllm(self, client, db_session, test_user):
        """An Azure alias from a user without access keeps falling back to
        the vLLM default (the gateway's longstanding stance) — the cloud cap
        never enters the picture, even when exhausted."""
        self._revoke_azure(db_session, test_user)
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
        assert logs and all(log.backend == "vllm" for log in logs)

    def test_vllm_requests_unaffected(self, client, db_session, test_user):
        self._revoke_azure(db_session, test_user)
        _burn(db_session, test_user, Decimal("10.00"), "azure")
        _mock_vllm_ok(client)
        assert _post_vllm_chat(client).status_code == 200
