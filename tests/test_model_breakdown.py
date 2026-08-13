"""
Per-model monthly cost breakdown (stats.get_model_breakdown) and its
dashboard card — spend split by model alias with on-prem / Azure / Bedrock
backends kept separate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session

from app.models.schema import UsageLog, User
from app.services.stats import get_model_breakdown
from tests.conftest import web_auth_header


def _log(db: Session, user_id: int, *, model: str, backend: str = "vllm",
         cost: float = 0.1, input_tokens: int = 100, output_tokens: int = 10,
         model_type: str = "llm", created_at: datetime | None = None):
    db.add(UsageLog(
        user_id=user_id, model=model, model_type=model_type,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cost_usd=cost, endpoint="/v1/chat/completions", backend=backend,
        created_at=created_at or datetime.now(timezone.utc),
    ))


class TestGetModelBreakdown:

    def test_groups_by_model_and_backend(self, db_session, test_user):
        _log(db_session, test_user.id, model="test-llm", backend="vllm", cost=0.2)
        _log(db_session, test_user.id, model="test-llm", backend="vllm", cost=0.3)
        _log(db_session, test_user.id, model="azure-gpt-4", backend="azure", cost=1.0)
        db_session.commit()

        rows = get_model_breakdown(db_session, test_user.id)
        assert len(rows) == 2
        by_key = {(r["model"], r["backend"]): r for r in rows}
        vllm_row = by_key[("test-llm", "vllm")]
        assert vllm_row["requests"] == 2
        assert vllm_row["cost_usd"] == 0.5
        assert vllm_row["input_tokens"] == 200
        assert vllm_row["output_tokens"] == 20
        azure_row = by_key[("azure-gpt-4", "azure")]
        assert azure_row["requests"] == 1
        assert azure_row["cost_usd"] == 1.0

    def test_ordered_by_cost_desc(self, db_session, test_user):
        _log(db_session, test_user.id, model="cheap", cost=0.01)
        _log(db_session, test_user.id, model="pricey", cost=2.0)
        _log(db_session, test_user.id, model="mid", cost=0.5)
        db_session.commit()

        rows = get_model_breakdown(db_session, test_user.id)
        assert [r["model"] for r in rows] == ["pricey", "mid", "cheap"]

    def test_excludes_previous_months_and_other_users(self, db_session, test_user):
        other = User(username="someoneelse", api_key="sk-other")
        db_session.add(other)
        db_session.commit()
        db_session.refresh(other)

        _log(db_session, test_user.id, model="this-month", cost=0.1)
        _log(db_session, test_user.id, model="last-month", cost=9.9,
             created_at=datetime.now(timezone.utc) - timedelta(days=40))
        _log(db_session, other.id, model="not-mine", cost=5.0)
        db_session.commit()

        rows = get_model_breakdown(db_session, test_user.id)
        assert [r["model"] for r in rows] == ["this-month"]

    def test_same_alias_split_across_backends(self, db_session, test_user):
        # A renamed/reused alias must not merge across backends.
        _log(db_session, test_user.id, model="shared-name", backend="vllm", cost=0.1)
        _log(db_session, test_user.id, model="shared-name", backend="bedrock", cost=0.2)
        db_session.commit()

        rows = get_model_breakdown(db_session, test_user.id)
        assert len(rows) == 2
        assert {r["backend"] for r in rows} == {"vllm", "bedrock"}


class TestDashboardCostByModelCard:

    def _get_dashboard(self, client, username):
        return client.get(
            "/dashboard",
            headers=web_auth_header(sub=username, scopes=["read"]),
        )

    def test_card_renders_backend_groups_and_rows(self, client, db_session, test_user):
        _log(db_session, test_user.id, model="test-llm", backend="vllm", cost=0.5)
        _log(db_session, test_user.id, model="azure-gpt-4", backend="azure", cost=1.5)
        _log(db_session, test_user.id, model="bedrock-claude", backend="bedrock", cost=1.0)
        db_session.commit()

        resp = self._get_dashboard(client, test_user.username)
        assert resp.status_code == 200
        body = resp.text
        assert "Cost by Model" in body
        # Backend section labels (breakdown group headers)
        assert "On-Prem" in body
        assert "AWS Bedrock" in body
        # Per-model rows
        assert "test-llm" in body
        assert "azure-gpt-4" in body
        assert "bedrock-claude" in body

    def test_empty_state_without_usage(self, client, db_session):
        db_session.add(User(username="nousage"))
        db_session.commit()

        resp = self._get_dashboard(client, "nousage")
        assert resp.status_code == 200
        assert "Cost by Model" in resp.text
        assert "No usage data yet." in resp.text

    def test_cloud_spend_shows_without_cloud_access(self, client, db_session):
        # Billing truth wins: Azure spend stays visible in the breakdown even
        # for a user without can_use_azure (e.g. access revoked mid-month).
        u = User(username="revokedmb", can_use_azure=False, can_use_bedrock=False)
        db_session.add(u)
        db_session.commit()
        db_session.refresh(u)
        _log(db_session, u.id, model="azure-gpt-4", backend="azure", cost=0.7)
        db_session.commit()

        body = self._get_dashboard(client, "revokedmb").text
        assert "azure-gpt-4" in body
