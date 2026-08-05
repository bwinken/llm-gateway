"""
Tests for the usage-anomaly scan (scripts/scan_anomalies.py) and its admin
API. Detectors are pure functions over a Session, so they run against the
in-memory SQLite harness directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.models.schema import AnomalyEvent, UsageLog, User
from scripts.scan_anomalies import (
    BURST_PER_HOUR,
    detect_burst_rate,
    detect_cost_spike,
    detect_empty_turn_rate,
    persist,
    prune_resolved,
)
from tests.conftest import web_auth_header


NOW = datetime.now(timezone.utc)


def _seed_user(session: Session, username: str) -> User:
    u = User(username=username)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _log(session: Session, user: User, *, cost=0.01, out_tokens=100,
         when: datetime | None = None, model="m", model_type="llm"):
    session.add(UsageLog(
        user_id=user.id, model=model, model_type=model_type,
        input_tokens=100, output_tokens=out_tokens, cost_usd=cost,
        endpoint="/v1/chat/completions", created_at=when or NOW,
    ))


class TestCostSpike:

    def test_spike_detected(self, db_session):
        u = _seed_user(db_session, "spiky")
        # 30 days of ~$0.20/day baseline
        for d in range(1, 15):
            _log(db_session, u, cost=0.2, when=NOW - timedelta(days=d))
        # today: $3 (15× median, above floor)
        for _ in range(3):
            _log(db_session, u, cost=1.0)
        db_session.commit()

        findings = detect_cost_spike(db_session, NOW)
        rules = [(f["rule"], f["scope"]) for f in findings]
        assert ("cost_spike", f"user:{u.id}") in rules

    def test_no_spike_below_floor(self, db_session):
        u = _seed_user(db_session, "tiny")
        _log(db_session, u, cost=0.05, when=NOW - timedelta(days=2))
        _log(db_session, u, cost=0.5)  # 10× median but under $1 floor
        db_session.commit()
        assert detect_cost_spike(db_session, NOW) == []


class TestBurstRate:

    def test_burst_flagged_for_human_only(self, db_session):
        human = _seed_user(db_session, "fastfingers")
        app = _seed_user(db_session, "app_batch")
        for _ in range(BURST_PER_HOUR + 1):
            _log(db_session, human, when=NOW - timedelta(minutes=30))
            _log(db_session, app, when=NOW - timedelta(minutes=30))
        db_session.commit()

        findings = detect_burst_rate(db_session, NOW)
        scopes = [f["scope"] for f in findings]
        assert f"user:{human.id}" in scopes
        assert f"user:{app.id}" not in scopes  # app accounts are exempt


class TestEmptyTurnRate:

    def test_model_level_spike_detected(self, db_session):
        u = _seed_user(db_session, "victim")
        # Baseline: healthy responses over prior days
        for d in range(1, 8):
            for _ in range(10):
                _log(db_session, u, out_tokens=500, when=NOW - timedelta(days=d), model="radLLM")
        # Last hour: 40% empty turns
        for i in range(30):
            _log(db_session, u, out_tokens=(1 if i < 12 else 500),
                 when=NOW - timedelta(minutes=30), model="radLLM")
        db_session.commit()

        findings = detect_empty_turn_rate(db_session, NOW)
        assert any(f["rule"] == "empty_turn_rate" and f["scope"] == "model:radLLM"
                   for f in findings)


class TestPersistIdempotency:

    def test_upsert_no_duplicates(self, db_session):
        u = _seed_user(db_session, "dupe")
        finding = {
            "scope": f"user:{u.id}", "rule": "cost_spike", "severity": "warning",
            "user_id": u.id, "model": None,
            "window_start": NOW.replace(minute=0, second=0, microsecond=0),
            "window_end": NOW, "observed": 5.0, "baseline": 1.0, "ratio": 5.0,
            "detail": "{\"summary\": \"x\"}",
        }
        created, updated = persist(db_session, [finding])
        assert (created, updated) == (1, 0)
        # Same window again, observed grew → update in place, no new row
        finding2 = dict(finding, observed=8.0, ratio=8.0)
        created, updated = persist(db_session, [finding2])
        assert (created, updated) == (0, 1)

        rows = db_session.exec(select(AnomalyEvent)).all()
        assert len(rows) == 1
        assert rows[0].observed == 8.0

    def test_prune_resolved(self, db_session):
        old = datetime.now(timezone.utc) - timedelta(days=120)
        db_session.add(AnomalyEvent(
            scope="user:1", rule="cost_spike", window_start=old, window_end=old,
            status="resolved", created_at=old, updated_at=old,
        ))
        db_session.add(AnomalyEvent(
            scope="user:2", rule="cost_spike", window_start=old, window_end=old,
            status="new", created_at=old, updated_at=old,
        ))
        db_session.commit()
        assert prune_resolved(db_session) == 1
        remaining = db_session.exec(select(AnomalyEvent)).all()
        assert len(remaining) == 1 and remaining[0].status == "new"


class TestAnomalyAdminAPI:

    def _seed_event(self, db_session) -> int:
        ev = AnomalyEvent(
            scope="user:1", rule="burst_rate", severity="warning",
            window_start=NOW, window_end=NOW, observed=700, baseline=600,
            ratio=1.2, detail='{"summary": "test"}',
        )
        db_session.add(ev)
        db_session.commit()
        db_session.refresh(ev)
        return ev.id

    def test_list_ack_resolve_cycle(self, client, db_session, admin_user):
        eid = self._seed_event(db_session)
        h = web_auth_header(sub=admin_user.username, scopes=["admin"])

        listed = client.get("/admin/api/anomalies?status=new", headers=h).json()
        assert any(e["id"] == eid for e in listed)

        assert client.post(f"/admin/api/anomalies/{eid}/ack", headers=h).status_code == 200
        db_session.expire_all()
        assert db_session.get(AnomalyEvent, eid).status == "acknowledged"

        assert client.post(f"/admin/api/anomalies/{eid}/resolve", headers=h).status_code == 200
        db_session.expire_all()
        assert db_session.get(AnomalyEvent, eid).status == "resolved"

    def test_requires_admin(self, client, db_session):
        resp = client.get(
            "/admin/api/anomalies",
            headers=web_auth_header(sub="testuser", scopes=["read"]),
        )
        assert resp.status_code == 403

    def test_admin_page_shows_open_anomalies(self, client, db_session, admin_user):
        self._seed_event(db_session)
        resp = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"]))
        assert resp.status_code == 200
        assert "Anomalies" in resp.text
        assert "burst_rate" in resp.text
