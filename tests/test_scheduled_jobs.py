"""In-gateway periodic jobs (app/services/scheduler.py) and the two jobs that
used to be systemd timers: the anomaly scan and usage_logs retention."""

from __future__ import annotations

import asyncio
import inspect
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlmodel import select

from app.core.timeutil import LOCAL_TZ, local_day_start_utc_of
from app.models.schema import AnomalyEvent, ScheduledJob, UsageLog, User
from app.services import scheduler, usage_retention, usage_rollup


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None)


class TestClaim:
    def test_once_per_interval(self, db_session):
        t0 = datetime(2026, 10, 1, 12, 0)
        assert scheduler.claim(db_session, "job", 3600, now=t0) is True
        assert scheduler.claim(db_session, "job", 3600, now=t0) is False
        assert scheduler.claim(db_session, "job", 3600, now=t0 + timedelta(minutes=59)) is False
        assert scheduler.claim(db_session, "job", 3600, now=t0 + timedelta(minutes=60)) is True
        row = db_session.get(ScheduledJob, "job")
        assert row.last_started_at == t0 + timedelta(minutes=60)
        assert row.last_status == "running"

    def test_jobs_are_independent(self, db_session):
        t0 = datetime(2026, 10, 1, 12, 0)
        assert scheduler.claim(db_session, "a", 3600, now=t0)
        assert scheduler.claim(db_session, "b", 3600, now=t0)


class TestRunIfDue:
    def test_runs_and_records_ok(self, db_session):
        calls = []
        job = scheduler.Job("j", lambda: 60.0, lambda s: calls.append(s) or "did it")
        assert scheduler.run_if_due(job) == "ok"
        assert scheduler.run_if_due(job) is None      # claimed for this interval
        assert len(calls) == 1
        db_session.expire_all()
        row = db_session.get(ScheduledJob, "j")
        assert (row.last_status, row.last_detail) == ("ok", "did it")
        assert row.last_finished_at is not None

    def test_failure_is_recorded_not_raised(self, db_session):
        def boom(_s):
            raise RuntimeError("nope")
        assert scheduler.run_if_due(scheduler.Job("bad", lambda: 60.0, boom)) == "error"
        db_session.expire_all()
        row = db_session.get(ScheduledJob, "bad")
        assert row.last_status == "error" and "RuntimeError: nope" in row.last_detail

    def test_claim_failure_is_not_raised(self):
        with patch.object(scheduler, "claim", side_effect=RuntimeError("no table")):
            assert scheduler.run_if_due(scheduler.Job("x", lambda: 60.0, lambda s: "")) is None

    def test_disabled_job_never_runs(self, db_session):
        job = scheduler.Job("off", lambda: 0.0, lambda s: pytest.fail("ran"))
        assert scheduler.run_if_due(job) is None
        assert db_session.get(ScheduledJob, "off") is None
        asyncio.run(scheduler.job_loop(job))  # returns at once

    @pytest.mark.parametrize("raw, expected", [("", 5.0), ("7", 7.0), ("0", 0.0), ("x", 5.0)])
    def test_env_seconds(self, monkeypatch, raw, expected):
        monkeypatch.setenv("SOME_INTERVAL_S", raw)
        assert scheduler.env_seconds("SOME_INTERVAL_S", 5.0) == expected

    def test_lifespan_starts_both_jobs(self):
        import app.main

        src = inspect.getsource(app.main.lifespan)
        assert "job_loop(job)" in src and "SCAN_JOB" in src and "RETENTION_JOB" in src


class TestAnomalyScanJob:
    def test_findings_for_deleted_accounts_are_dropped(self, db_session):
        """delete_user keeps usage; a finding about an account that no longer
        exists would violate anomaly_events.user_id's foreign key."""
        from app.services.anomaly_scan import detect_all, run_scan

        now = datetime.now(timezone.utc)
        alive = User(username="alive")
        db_session.add(alive)
        db_session.commit()
        for uid in (alive.id, 9999):   # 9999: deleted, usage kept
            for _ in range(20):
                db_session.add(UsageLog(user_id=uid, model="m", model_type="llm", input_tokens=1,
                                        output_tokens=1, cost_usd=Decimal("1"), endpoint="/x",
                                        created_at=now))
        db_session.commit()
        users = {f["user_id"] for f in detect_all(db_session, now)}
        assert alive.id in users and 9999 not in users
        assert "finding(s)" in run_scan(db_session)
        persisted = {e.user_id for e in db_session.exec(select(AnomalyEvent)).all()}
        assert persisted - {None} == {alive.id}   # None: model-scoped findings

    def test_interval(self, monkeypatch):
        from app.services.anomaly_scan import SCAN_JOB

        monkeypatch.delenv("ANOMALY_SCAN_INTERVAL_S", raising=False)
        assert SCAN_JOB.interval() == 3600.0
        monkeypatch.setenv("ANOMALY_SCAN_INTERVAL_S", "0")
        assert SCAN_JOB.interval() == 0.0


def _log(session, user_id: int, when: datetime, cost: str = "1.000000") -> None:
    session.add(UsageLog(user_id=user_id, model="m", model_type="llm", input_tokens=10,
                         output_tokens=10, cost_usd=Decimal(cost), endpoint="/x", created_at=when))


class TestRetention:
    @pytest.mark.parametrize("raw, expected", [("", 0), ("365", 365), ("0", 0), ("-3", 0), ("1y", 0)])
    def test_retention_days(self, monkeypatch, raw, expected):
        monkeypatch.setenv("USAGE_LOG_RETENTION_DAYS", raw)
        assert usage_retention.retention_days() == expected
        assert usage_retention.RETENTION_JOB.interval() == (86400.0 if expected else 0.0)

    def test_off_by_default(self, monkeypatch, db_session):
        monkeypatch.delenv("USAGE_LOG_RETENTION_DAYS", raising=False)
        assert usage_retention.RETENTION_JOB.interval() == 0.0
        assert usage_retention.run_retention(db_session) == "retention off"

    def test_nothing_deleted_before_first_rollup(self, monkeypatch, db_session):
        _log(db_session, 1, datetime(2020, 1, 1, tzinfo=timezone.utc))
        db_session.commit()
        monkeypatch.setenv("USAGE_LOG_RETENTION_DAYS", "30")
        assert usage_retention.purge_cutoff(db_session, 30) is None
        assert "not run yet" in usage_retention.run_retention(db_session)
        assert len(db_session.exec(select(UsageLog)).all()) == 1

    def test_cutoff_capped_at_watermark_and_day_aligned(self, db_session):
        _log(db_session, 1, datetime(2026, 3, 1, 3, tzinfo=timezone.utc))
        db_session.commit()
        usage_rollup.advance(db_session, now=local_day_start_utc_of(date(2026, 3, 5)) + timedelta(hours=1))
        assert usage_rollup.rolled_through(db_session) == date(2026, 3, 4)
        now = datetime(2026, 6, 1, 7, 30, tzinfo=timezone.utc)
        # 30 days would reach May 2; the rollup only covers through March 4.
        assert usage_retention.purge_cutoff(db_session, 30, now) == local_day_start_utc_of(date(2026, 3, 5))
        # Inside the rolled range: a local midnight, never mid-day.
        cut = usage_retention.purge_cutoff(db_session, 90, datetime(2026, 6, 1, 7, 30, tzinfo=timezone.utc))
        assert cut == local_day_start_utc_of(date(2026, 3, 3))
        assert cut.astimezone(LOCAL_TZ).hour == 0

    def test_purge_keeps_every_report(self, db_session):
        """Rolled days survive in usage_daily, so reports don't change."""
        from app.services.analytics import build_monthly_report

        alice = User(username="alice", org_code="ENG")
        db_session.add(alice)
        db_session.commit()
        for d in range(1, 11):
            for _ in range(3):
                _log(db_session, alice.id, local_day_start_utc_of(date(2026, 3, d)) + timedelta(hours=10))
        db_session.commit()
        usage_rollup.advance(db_session, now=local_day_start_utc_of(date(2026, 3, 20)))
        before = build_monthly_report(db_session, "2026-03", "2026-03").breakdowns
        # Apr 6 08:00 local - 30 days = Mar 7: whole days Mar 1-6 go.
        cutoff = usage_retention.purge_cutoff(db_session, 30, datetime(2026, 4, 6, tzinfo=timezone.utc))
        assert usage_retention.count_before(db_session, cutoff) == 18      # March 1–6
        assert usage_retention.purge_before(db_session, cutoff, batch_size=5) == 18
        assert len(db_session.exec(select(UsageLog)).all()) == 12
        assert build_monthly_report(db_session, "2026-03", "2026-03").breakdowns == before

    def test_scheduled_run(self, monkeypatch, db_session):
        old = datetime.now(timezone.utc) - timedelta(days=100)
        _log(db_session, 1, old)
        _log(db_session, 1, datetime.now(timezone.utc))
        db_session.commit()
        usage_rollup.advance(db_session)
        monkeypatch.setenv("USAGE_LOG_RETENTION_DAYS", "30")
        monkeypatch.setattr(usage_retention, "BATCH_PAUSE_S", 0.0)
        assert usage_retention.run_retention(db_session).startswith("deleted 1 ")
        assert [_naive(r.created_at) > _naive(old) for r in db_session.exec(select(UsageLog)).all()] == [True]
