"""Teams alerts (app/services/alerts.py): off unless TEAMS_WEBHOOK_URL, one
post per event across workers (alert_state), and the health / budget /
anomaly hooks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
from sqlmodel import select

from app.models.schema import AlertState, UsageLog
from app.services import alerts
from tests.conftest import auth_header, make_httpx_response
from tests.test_stream_idle_guard import _DeadClient, _run_check


@pytest.fixture()
def teams(monkeypatch):
    """Alerts on, posted cards captured, jobs run inline, fresh worker memory."""
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://example.invalid/webhook")
    posted: list[dict] = []
    monkeypatch.setattr(alerts, "post", lambda card: posted.append(card) or True)
    monkeypatch.setattr(alerts, "_submit", lambda fn, *args: fn(*args))
    monkeypatch.setattr(alerts, "_budget_seen", set())
    monkeypatch.setattr(alerts, "_budget_day", "")
    return posted


def _title(card: dict) -> str:
    return card["attachments"][0]["content"]["body"][0]["text"]


def _spend(db_session, user, usd: str) -> None:
    db_session.add(UsageLog(user_id=user.id, cost_usd=Decimal(usd)))
    db_session.commit()


def _chat(client) -> int:
    async def _post(*args, **kwargs):
        return make_httpx_response(200, {
            "id": "c1",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    client.__httpx_mock__.post = _post
    return client.post(
        "/v1/chat/completions",
        json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(),
    ).status_code


class TestDisabled:
    def test_nothing_posted_or_stored_without_a_webhook(self, client, db_session, test_user, monkeypatch):
        posted = []
        monkeypatch.setattr(alerts, "post", lambda card: posted.append(card) or True)
        _spend(db_session, test_user, "95")
        assert _chat(client) == 200
        alerts.server_state_changed("http://x:8000/v1", False, ["m"])
        assert posted == []
        assert db_session.exec(select(AlertState)).all() == []

    def test_teams_alerts_picks_kinds(self, monkeypatch):
        monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://example.invalid/webhook")
        monkeypatch.setenv("TEAMS_ALERTS", "health, anomaly")
        assert alerts.enabled("health") and alerts.enabled("anomaly")
        assert not alerts.enabled("budget")


class TestBudget:
    def test_80_percent_posts_once(self, client, db_session, test_user, teams):
        _spend(db_session, test_user, "85")       # limit is $100
        assert _chat(client) == 200
        assert _chat(client) == 200
        assert [_title(c) for c in teams] == ["Daily budget at 80%: testuser"]
        facts = {f["title"]: f["value"] for f in teams[0]["attachments"][0]["content"]["body"][-1]["facts"]}
        assert facts["Spent today"] == "$85.00" and facts["Daily limit"] == "$100.00"

    def test_another_worker_does_not_repost(self, client, db_session, test_user, teams, monkeypatch):
        _spend(db_session, test_user, "85")
        assert _chat(client) == 200
        monkeypatch.setattr(alerts, "_budget_seen", set())   # a second worker's memory
        assert _chat(client) == 200
        assert len(teams) == 1

    def test_exhausted_posts_with_the_429(self, client, db_session, test_user, teams):
        _spend(db_session, test_user, "100")
        assert _chat(client) == 429
        assert [_title(c) for c in teams] == ["Daily budget exhausted: testuser"]
        assert "429" in teams[0]["attachments"][0]["content"]["body"][1]["text"]

    def test_soft_mode_says_requests_are_still_served(self, client, db_session, test_user, teams, monkeypatch):
        monkeypatch.setenv("ENFORCE_DAILY_LIMIT", "false")
        _spend(db_session, test_user, "100")
        assert _chat(client) == 200
        assert "soft mode" in teams[0]["attachments"][0]["content"]["body"][1]["text"]

    def test_below_threshold_and_zero_budget_stay_quiet(self, db_session, test_user, teams):
        alerts.budget_reached(test_user, 79.0, 100.0)
        alerts.budget_reached(test_user, 0.0, 0.0)
        assert teams == []


class TestHealth:
    def test_down_then_up_posted_once_each(self, db_session, teams):
        url = "http://alerts-health-test-a:8000/v1"
        alerts.server_state_changed(url, True, ["m"])     # first sight UP: not news
        assert teams == []
        alerts.server_state_changed(url, False, ["m"])
        alerts.server_state_changed(url, False, ["m"])    # second worker, same event
        alerts.server_state_changed(url, True, ["m"])
        assert [_title(c) for c in teams] == ["On-prem server DOWN", "On-prem server back UP"]

    def test_health_loop_reports_a_down_server(self, db_session, teams):
        url = "http://alerts-health-test-b:8000/v1"
        _run_check(_DeadClient(), {"m": {"base_url": url, "api_key": ""}})
        assert [_title(c) for c in teams] == ["On-prem server DOWN"]
        facts = teams[0]["attachments"][0]["content"]["body"][-1]["facts"]
        assert {"title": "Models", "value": "m"} in facts


class TestAnomalies:
    def test_only_created_findings_are_posted(self, db_session, test_user, teams):
        from scripts.scan_anomalies import persist

        finding = {
            "scope": f"user:{test_user.id}", "rule": "cost_spike", "severity": "critical",
            "user_id": test_user.id, "model": None,
            "window_start": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "window_end": datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
            "observed": 5.0, "baseline": 1.0, "ratio": 5.0,
            "detail": '{"summary": "today $5.00 vs 30d median $1.00"}',
        }
        created: list[dict] = []
        persist(db_session, [finding], created)
        persist(db_session, [dict(finding, observed=6.0)], created)   # update: not new
        assert alerts.post_anomalies(created, {test_user.id: "testuser"})
        assert [_title(c) for c in teams] == ["1 new usage anomaly"]
        text = teams[0]["attachments"][0]["content"]["body"][1]["text"]
        assert "testuser" in text and "cost_spike" in text and "$5.00" in text


class TestDelivery:
    def test_post_shapes_and_status_handling(self, monkeypatch):
        monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://example.invalid/webhook")
        calls = []

        def fake_post(url, json, timeout):
            calls.append(json)
            return httpx.Response(status)

        status = 202
        with patch.object(alerts.httpx, "post", fake_post):
            assert alerts.post(alerts.build_card("t", [("k", "v")]))
            status = 500
            assert not alerts.post(alerts.build_card("t", []))
        card = calls[0]
        assert card["type"] == "message"
        assert card["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"

        def boom(*a, **k):
            raise httpx.ConnectError("no route")

        with patch.object(alerts.httpx, "post", boom):
            assert not alerts.post(alerts.build_card("t", []))

    def test_prune_drops_old_budget_keys_only(self, db_session):
        old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=8)
        db_session.add_all([
            AlertState(key="budget:1:2026-01-01:80", state="sent", updated_at=old),
            AlertState(key="budget:1:2026-01-09:80", state="sent", updated_at=old + timedelta(days=7)),
            AlertState(key="health:http://x/v1", state="down", updated_at=old),
        ])
        db_session.commit()
        assert alerts.prune(db_session) == 1
        assert {r.key for r in db_session.exec(select(AlertState)).all()} == {
            "budget:1:2026-01-09:80", "health:http://x/v1",
        }
