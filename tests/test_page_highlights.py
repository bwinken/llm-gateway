"""What the dashboard and admin panel lead with: the budget meter, the
on-prem status rollup, and the admin "Needs attention" panel."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.models.schema import User, UsageLog
from app.services.stats import get_budget_pressure, get_today_totals, summarize_server_status
from tests.conftest import web_auth_header


def _spend(session, user: User, usd: float, *, minutes_ago: int = 5) -> None:
    session.add(UsageLog(
        user_id=user.id, model="test-llm", model_type="llm", input_tokens=1000,
        output_tokens=100, cost_usd=Decimal(str(usd)), endpoint="/v1/messages",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    ))
    session.commit()


# app.routers.* are imported inside the tests, never at module level:
# conftest's _build_test_app imports the routers under its config patches, and
# a collection-time import would bind them to the real (unpatched) config.
class TestBudgetState:
    @pytest.mark.parametrize("limit, spent, state", [
        (0, 50, "unlimited"),
        (10, 0, "ok"),
        (10, 6.99, "ok"),
        (10, 7, "warn"),
        (10, 8.99, "warn"),
        (10, 9, "crit"),
        (10, 9.99, "crit"),
        (10, 10, "over"),
        (10, 25, "over"),
    ])
    def test_bands(self, limit, spent, state):
        from app.routers.web_ui import _budget_state
        assert _budget_state(limit, spent) == state

    @pytest.mark.parametrize("seconds, text", [
        (0, "0m"), (59, "0m"), (60, "1m"), (3599, "59m"), (3600, "1h 00m"), (4 * 3600 + 7 * 60 + 30, "4h 07m"),
    ])
    def test_format_duration(self, seconds, text):
        from app.routers.web_ui import _format_duration
        assert _format_duration(seconds) == text


class TestServerStatus:
    def test_rollup(self):
        status = summarize_server_status([
            {"name": "a", "alive": True, "waiting": 0},
            {"name": "b", "alive": True, "waiting": 3},
            {"name": "c", "alive": True, "waiting": 12},
            {"name": "d", "alive": False, "waiting": None},
            {"name": "e", "alive": True, "waiting": None},  # /metrics unavailable
        ])
        assert status["total"] == 5
        assert status["online"] == 4
        assert status["down"] == ["d"]
        assert [q["name"] for q in status["queueing"]] == ["c", "b"]  # busiest first
        assert [q["name"] for q in status["overloaded"]] == ["c"]

    def test_empty(self):
        assert summarize_server_status([]) == {
            "total": 0, "online": 0, "down": [], "queueing": [], "overloaded": [],
        }


class TestTodayAggregates:
    def test_today_totals_count_people_not_apps(self, db_session, test_user):
        app = User(username="app_bot", api_key="sk-app")
        db_session.add(app)
        db_session.commit()
        _spend(db_session, test_user, 1.5)
        _spend(db_session, test_user, 0.5)
        _spend(db_session, app, 2.0)
        _spend(db_session, test_user, 9.0, minutes_ago=60 * 30)  # yesterday-ish: excluded
        totals = get_today_totals(db_session)
        assert totals["requests"] == 3
        assert totals["cost"] == pytest.approx(4.0)
        assert totals["active_users"] == 1

    def test_budget_pressure(self, db_session, test_user):
        near = User(username="near", api_key="sk-near", daily_limit_usd=10)
        over = User(username="over", api_key="sk-over", daily_limit_usd=10)
        fine = User(username="fine", api_key="sk-fine", daily_limit_usd=10)
        unlimited = User(username="unl", api_key="sk-unl", daily_limit_usd=0)
        disabled = User(username="dis", api_key="sk-dis", daily_limit_usd=10, is_disabled=True)
        db_session.add_all([near, over, fine, unlimited, disabled])
        db_session.commit()
        for u, usd in [(near, 9.2), (over, 12), (fine, 5), (unlimited, 500), (disabled, 50)]:
            _spend(db_session, u, usd)
        rows = get_budget_pressure(db_session)
        assert [r["username"] for r in rows] == ["over", "near"]
        assert rows[0]["percent"] == 120.0


class TestPages:
    def test_dashboard_leads_with_budget(self, client, db_session, test_user):
        test_user.daily_limit_usd = 10
        db_session.add(test_user)
        db_session.commit()
        _spend(db_session, test_user, 9.5)
        resp = client.get("/dashboard", headers=web_auth_header(sub=test_user.username))
        assert resp.status_code == 200
        body = resp.text
        assert "Spent today" in body
        assert "Remaining" in body
        assert "$0.5000" in body  # 10 - 9.5
        assert "Almost used up" in body
        # The budget comes before the model tables.
        assert body.index("Spent today") < body.index("On-Prem Models")

    def test_dashboard_unlimited(self, client, db_session, test_user):
        test_user.daily_limit_usd = 0
        db_session.add(test_user)
        db_session.commit()
        body = client.get("/dashboard", headers=web_auth_header(sub=test_user.username)).text
        assert "No daily limit on this account" in body
        assert "Remaining" not in body

    def test_dashboard_status_chips(self, client, test_user):
        metrics = {"http://mock-llm:8000/v1": {"running": 4, "waiting": 12}}
        with (
            patch("app.routers.web_ui.is_alive", side_effect=lambda url: url != "http://mock-vlm:8001/v1"),
            patch("app.routers.web_ui.get_metrics", side_effect=lambda url: metrics.get(url)),
        ):
            body = client.get("/dashboard", headers=web_auth_header(sub=test_user.username)).text
        assert "1 queueing" in body
        assert "1 down" in body
        assert "12 queued · busy" in body

    def test_admin_all_clear(self, client, admin_user):
        with patch("app.routers.admin.is_alive", return_value=True), patch("app.routers.admin.get_metrics", return_value=None):
            body = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        assert "All clear" in body
        assert "Needs attention" not in body

    def test_admin_needs_attention(self, client, db_session, admin_user, test_user):
        test_user.daily_limit_usd = 10
        db_session.add(test_user)
        db_session.commit()
        _spend(db_session, test_user, 10.5)
        with (
            patch("app.routers.admin.is_alive", side_effect=lambda url: url != "http://mock-embed:8080/v1"),
            patch("app.routers.admin.get_metrics", return_value=None),
        ):
            body = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        assert "Needs attention" in body
        assert "test-embedding" in body
        assert "105% · blocked" in body
        assert "Spend today" in body
        # Attention first, settings last.
        assert body.index("Needs attention") < body.index("Spend today") < body.index("User Management") < body.index("Concurrency Limit")
