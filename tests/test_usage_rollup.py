"""The usage_daily rollup (app/services/usage_rollup.py) and the reports that
read through it.

The contract that matters: every report gives the same numbers whether the
rollup has run or not. Closed days up to the watermark come from
``usage_daily``, everything after from ``usage_logs``; a rollup that never
ran only costs speed.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlmodel import delete, select

from app.core.timeutil import LOCAL_TZ, local_day_start_utc_of, local_today
from app.models.schema import AppOwner, UsageDaily, UsageLog, UsageRollupState, User
from app.services import usage_rollup


def _user(session, username: str, org: str = "") -> User:
    u = User(username=username, org_code=org, api_key=f"sk-{username}")
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _log(session, user: User, when: datetime, *, cost: str = "0.010000", model: str = "m1",
         model_type: str = "llm", backend: str = "vllm", out: int = 10,
         endpoint: str = "/v1/chat/completions") -> None:
    session.add(UsageLog(
        user_id=user.id, model=model, model_type=model_type, input_tokens=100,
        output_tokens=out, cost_usd=Decimal(cost), endpoint=endpoint, backend=backend,
        created_at=when,
    ))


def _at_local(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=LOCAL_TZ).astimezone(timezone.utc)


@pytest.fixture()
def history(db_session):
    """Usage over the last ~40 local days plus today, with rows right at the
    local-midnight boundary, several models/backends, an app account and an
    empty turn."""
    alice = _user(db_session, "alice", "ENG")
    bob = _user(db_session, "bob", "OPS")
    bot = _user(db_session, "app_bot", "ENG")
    db_session.add(AppOwner(app_id=bot.id, owner_id=alice.id))
    today = local_today()
    for back in range(0, 41):
        day = today - timedelta(days=back)
        _log(db_session, alice, _at_local(day, 0, 0, 0), cost="0.100000")          # first instant of the day
        _log(db_session, alice, _at_local(day, 23, 59, 59), cost="0.200000",       # last second of the day
             model="m2", backend="azure", endpoint="/v1/messages")
        if back % 3 == 0:
            _log(db_session, bob, _at_local(day, 12), cost="1.000000", model_type="vlm", out=1)
        if back % 2 == 0:
            _log(db_session, bot, _at_local(day, 9), cost="5.000000", backend="bedrock")
    db_session.commit()
    return {"alice": alice, "bob": bob, "bot": bot, "today": today}


def _reports(session, users) -> dict:
    """Every report that reads through ``aggregate``."""
    import sys
    from pathlib import Path

    from app.services import analytics, stats

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import scan_anomalies

    today = local_today()
    prev = today.replace(day=1) - timedelta(days=1)
    now = datetime.now(timezone.utc)
    return {
        "overview": stats.get_monthly_overview(session),
        "dau": stats.get_dau_trends(session),
        "daily": stats.get_daily_trends(session, users["alice"].id),
        "monthly": stats.get_user_monthly_summary(session, users["alice"].id),
        "models": stats.get_model_breakdown(session, users["alice"].id),
        "models_day": stats.get_model_breakdown(session, users["alice"].id, period="day"),
        "apps": stats.get_owned_apps_summary(session, users["alice"].id),
        "report": analytics.build_monthly_report(
            session, f"{prev.year:04d}-{prev.month:02d}", f"{today.year:04d}-{today.month:02d}",
        ).breakdowns,
        "csv": analytics.user_backend_costs(
            session, f"{prev.year:04d}-{prev.month:02d}", f"{today.year:04d}-{today.month:02d}",
        ),
        "spike": scan_anomalies.detect_cost_spike(session, now),
        "shift": scan_anomalies.detect_behavior_shift(session, now),
        "empty": scan_anomalies.detect_empty_turn_rate(session, now),
    }


class TestSameNumbersEitherWay:
    def test_every_report_matches_before_and_after_rollup(self, db_session, history):
        before = _reports(db_session, history)
        rolled = usage_rollup.advance(db_session)
        assert rolled, "fixture history should leave closed days to roll"
        assert db_session.exec(select(UsageDaily)).first() is not None
        after = _reports(db_session, history)
        for key in before:
            assert after[key] == before[key], key

    def test_partial_rollup_matches_too(self, db_session, history):
        """Watermark in the middle of a month: the month is split between
        usage_daily and usage_logs."""
        before = _reports(db_session, history)
        usage_rollup.advance(db_session, max_days=25)
        assert usage_rollup.rolled_through(db_session) is not None
        assert _reports(db_session, history) == before

    def test_aggregate_ignores_rollup_rows_past_the_watermark(self, db_session, history):
        """Only days up to the watermark are trusted from usage_daily."""
        today = history["today"]
        usage_rollup.advance(db_session)
        expected = usage_rollup.aggregate(db_session, today - timedelta(days=5), today + timedelta(days=1))
        # A stray row for today (never rolled) must not be double counted.
        db_session.add(UsageDaily(
            day=today, user_id=history["alice"].id, backend="vllm", model="ghost",
            model_type="llm", endpoint="/x", requests=999, input_tokens=0, output_tokens=0,
            cost_usd=Decimal("99"), empty_turns=0,
        ))
        db_session.commit()
        got = usage_rollup.aggregate(db_session, today - timedelta(days=5), today + timedelta(days=1))
        assert got == expected


class TestAggregate:
    def test_local_midnight_boundary(self, db_session):
        alice = _user(db_session, "alice")
        day = date(2026, 3, 10)
        _log(db_session, alice, local_day_start_utc_of(day) - timedelta(microseconds=1))  # 23:59:59.999999 on the 9th
        _log(db_session, alice, local_day_start_utc_of(day))                              # 00:00 on the 10th
        db_session.commit()
        got = usage_rollup.aggregate(db_session, date(2026, 3, 9), date(2026, 3, 11), ("day",))
        assert {k: v["requests"] for k, v in got.items()} == {
            (date(2026, 3, 9),): 1, (date(2026, 3, 10),): 1,
        }

    def test_empty_inputs(self, db_session):
        assert usage_rollup.aggregate(db_session, date(2026, 1, 2), date(2026, 1, 1)) == {}
        assert usage_rollup.aggregate(db_session, date(2026, 1, 1), date(2026, 1, 2), user_ids=[]) == {}
        assert usage_rollup.aggregate(db_session, date(2026, 1, 1), date(2026, 1, 2)) == {}

    def test_unknown_dimension(self, db_session):
        with pytest.raises(ValueError):
            usage_rollup.aggregate(db_session, date(2026, 1, 1), date(2026, 1, 2), ("username",))

    def test_empty_turns_counted(self, db_session):
        alice = _user(db_session, "alice")
        day = date(2026, 3, 10)
        for out in (0, 1, 2, 50):
            _log(db_session, alice, _at_local(day, 10), out=out)
        db_session.commit()
        raw = usage_rollup.aggregate(db_session, day, day + timedelta(days=1))[()]
        assert raw["empty_turns"] == 2 and raw["requests"] == 4
        usage_rollup.advance(db_session, now=_at_local(day + timedelta(days=2), 1))
        rolled = usage_rollup.aggregate(db_session, day, day + timedelta(days=1))[()]
        assert rolled == raw


class TestTaipeiMonths:
    def test_first_local_hours_of_a_month_belong_to_it(self, db_session):
        """2026-03-01 01:00 Taipei is 2026-02-28 17:00 UTC: a March request."""
        from app.services.analytics import build_monthly_report

        alice = _user(db_session, "alice", "ENG")
        _log(db_session, alice, datetime(2026, 2, 28, 17, 0, tzinfo=timezone.utc), cost="2.000000")
        _log(db_session, alice, datetime(2026, 2, 28, 15, 0, tzinfo=timezone.utc), cost="1.000000")
        db_session.commit()
        report = build_monthly_report(db_session, "2026-02", "2026-03")
        feb, mar = report.breakdowns
        assert feb.summary["cost_usd"] == 1.0
        assert mar.summary["cost_usd"] == 2.0
        assert mar.summary["dau_peak"] == 1 and mar.summary["active_days"] == 1


class TestAdvance:
    def test_settled_through(self):
        # A day is rolled only once it has been over for SETTLE_DELAY.
        assert usage_rollup.settled_through(_at_local(date(2026, 3, 11), 0, 10)) == date(2026, 3, 9)
        assert usage_rollup.settled_through(_at_local(date(2026, 3, 11), 0, 20)) == date(2026, 3, 10)

    def test_backfills_from_oldest_row_and_is_idempotent(self, db_session):
        alice = _user(db_session, "alice")
        _log(db_session, alice, _at_local(date(2026, 3, 1), 10))
        _log(db_session, alice, _at_local(date(2026, 3, 4), 10))
        db_session.commit()
        now = _at_local(date(2026, 3, 6), 12)
        assert usage_rollup.pending_days(db_session, now) == [
            date(2026, 3, d) for d in range(1, 6)
        ]
        assert usage_rollup.advance(db_session, now) == [date(2026, 3, d) for d in range(1, 6)]
        assert usage_rollup.rolled_through(db_session) == date(2026, 3, 5)
        assert usage_rollup.advance(db_session, now) == []
        rows = db_session.exec(select(UsageDaily).order_by(UsageDaily.day)).all()
        assert [r.day for r in rows] == [date(2026, 3, 1), date(2026, 3, 4)]

    def test_max_days_and_resume(self, db_session):
        alice = _user(db_session, "alice")
        _log(db_session, alice, _at_local(date(2026, 3, 1), 10))
        db_session.commit()
        now = _at_local(date(2026, 3, 6), 12)
        assert usage_rollup.advance(db_session, now, max_days=2) == [date(2026, 3, 1), date(2026, 3, 2)]
        assert usage_rollup.advance(db_session, now)[0] == date(2026, 3, 3)

    def test_no_data_rolls_nothing(self, db_session):
        assert usage_rollup.advance(db_session) == []
        assert usage_rollup.rolled_through(db_session) is None

    def test_today_is_never_rolled(self, db_session):
        alice = _user(db_session, "alice")
        _log(db_session, alice, datetime.now(timezone.utc))
        db_session.commit()
        usage_rollup.advance(db_session)
        assert db_session.exec(select(UsageDaily).where(UsageDaily.day == local_today())).all() == []


class TestRecompute:
    def test_rerolls_changed_days_and_keeps_watermark(self, db_session):
        alice = _user(db_session, "alice")
        day = date(2026, 3, 2)
        _log(db_session, alice, _at_local(date(2026, 3, 1), 10))
        _log(db_session, alice, _at_local(day, 10))
        db_session.commit()
        now = _at_local(date(2026, 3, 5), 12)
        usage_rollup.advance(db_session, now)
        _log(db_session, alice, _at_local(day, 11))   # a late correction
        db_session.commit()
        done, skipped = usage_rollup.recompute(db_session, day, date(2026, 3, 30))
        assert done == [day, date(2026, 3, 3), date(2026, 3, 4)] and skipped == []
        assert usage_rollup.rolled_through(db_session) == date(2026, 3, 4)
        assert usage_rollup.aggregate(db_session, day, day + timedelta(days=1))[()]["requests"] == 2

    def test_keeps_days_whose_raw_rows_are_gone(self, db_session):
        """Retention cleanup removed the raw rows: the rollup is all that is left."""
        alice = _user(db_session, "alice")
        old, new = date(2026, 3, 1), date(2026, 3, 3)
        _log(db_session, alice, _at_local(old, 10))
        _log(db_session, alice, _at_local(new, 10))
        db_session.commit()
        usage_rollup.advance(db_session, _at_local(date(2026, 3, 5), 12))
        db_session.execute(
            delete(UsageLog).where(UsageLog.created_at < local_day_start_utc_of(date(2026, 3, 2)))
        )
        db_session.commit()
        done, skipped = usage_rollup.recompute(db_session, old, new)
        # The oldest raw row left is on the 3rd: every day before it is kept.
        assert skipped == [old, date(2026, 3, 2)] and done == [new]
        assert usage_rollup.aggregate(db_session, old, old + timedelta(days=1))[()]["requests"] == 1

    def test_never_rolled(self, db_session):
        assert usage_rollup.recompute(db_session, date(2026, 1, 1), date(2026, 1, 5)) == ([], [])


class TestInAppLoop:
    def test_roll_next_day_rolls_one_day(self, db_session):
        alice = _user(db_session, "alice")
        yesterday = local_today() - timedelta(days=1)
        _log(db_session, alice, _at_local(yesterday - timedelta(days=1), 10))
        db_session.commit()
        first = usage_rollup.roll_next_day()
        assert first == yesterday - timedelta(days=1)
        db_session.expire_all()
        assert db_session.get(UsageRollupState, usage_rollup.STATE_NAME).rolled_through == first

    def test_roll_next_day_never_raises(self):
        with patch.object(usage_rollup, "advance", side_effect=RuntimeError("db down")):
            assert usage_rollup.roll_next_day() is None
        with patch.object(usage_rollup, "advance", side_effect=usage_rollup.RollupBusy("x")):
            assert usage_rollup.roll_next_day() is None

    def test_loop_disabled_by_zero_interval(self):
        with patch.object(usage_rollup, "roll_next_day") as roll:
            asyncio.run(usage_rollup.rollup_loop(interval=0))
        roll.assert_not_called()

    @pytest.mark.parametrize("raw, expected", [
        ("", 600.0), ("120", 120.0), ("0", 0.0), ("-5", 0.0), ("soon", 600.0),
    ])
    def test_interval_from_env(self, monkeypatch, raw, expected):
        monkeypatch.setenv("USAGE_ROLLUP_INTERVAL_S", raw)
        assert usage_rollup.interval_seconds() == expected

    def test_lifespan_starts_the_loop(self):
        import inspect

        import app.main

        assert "rollup_loop()" in inspect.getsource(app.main.lifespan)


class TestDauCountsPeopleOnly:
    def test_apps_and_deleted_users_excluded(self, db_session):
        from app.services.stats import get_dau_trends

        alice = _user(db_session, "alice")
        bot = _user(db_session, "app_bot")
        now = datetime.now(timezone.utc)
        _log(db_session, alice, now)
        _log(db_session, bot, now)
        db_session.add(UsageLog(user_id=9999, model="m", endpoint="/x", created_at=now))  # deleted account
        db_session.commit()
        assert get_dau_trends(db_session) == [{"date": local_today().isoformat(), "dau": 1}]
