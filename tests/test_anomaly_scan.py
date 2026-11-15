"""
Tests for the usage-anomaly scan (app/services/anomaly_scan.py) and its admin
API. Detectors are pure functions over a Session, so they run against the
in-memory SQLite harness directly.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from sqlmodel import Session, select

from app.core.timeutil import local_day_start_utc
from app.models.schema import AnomalyEvent, UsageLog, User
from app.services.anomaly_scan import (
    OFF_HOURS_MIN_REQUESTS,
    OPEN_EXPIRY_DAYS,
    close_stale,
    detect_all,
    detect_behavior_shift,
    detect_burst_rate,
    detect_cost_spike,
    detect_empty_turn_rate,
    detect_guardrail_unsafe,
    detect_off_hours,
    persist,
    prune_resolved,
)
from tests.conftest import web_auth_header


NOW = datetime.now(timezone.utc)


def _seed_user(session: Session, username: str, service: bool = False) -> User:
    u = User(username=username, is_service_account=service)
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


def _days(session, user, n_days, *, cost=0.01, per_day=1, first=1):
    """``per_day`` requests of ``cost`` at noon on each of ``n_days`` local days before today."""
    for d in range(first, first + n_days):
        noon = local_day_start_utc() - timedelta(days=d) + timedelta(hours=12)
        for _ in range(per_day):
            _log(session, user, cost=cost, when=noon)


class TestCostSpike:

    def _found(self, session):
        return {f["scope"]: f for f in detect_cost_spike(session, NOW)}

    def test_spike_detected(self, db_session):
        u = _seed_user(db_session, "spiky")
        _days(db_session, u, 14, cost=0.2)
        # today: $3, fifteen times a busy day and above the floor
        for _ in range(3):
            _log(db_session, u, cost=1.0)
        db_session.commit()

        finding = self._found(db_session)[f"user:{u.id}"]
        assert (finding["rule"], finding["severity"], finding["baseline"]) == ("cost_spike", "warning", 0.2)

    def test_no_spike_below_floor(self, db_session):
        u = _seed_user(db_session, "tiny")
        _days(db_session, u, 12, cost=0.05)
        _log(db_session, u, cost=0.5)  # 10× a busy day but under the $1 floor
        db_session.commit()
        assert detect_cost_spike(db_session, NOW) == []

    def test_the_baseline_is_a_busy_day_not_a_typical_one(self, db_session):
        """Most days small, a long session now and then: today is another
        long session. Five times the median, and nothing unusual."""
        u = _seed_user(db_session, "sessions")
        _days(db_session, u, 16, cost=0.5)
        _days(db_session, u, 4, cost=3.0, first=17)      # one day in five is a long one
        _log(db_session, u, cost=3.5)
        db_session.commit()
        assert detect_cost_spike(db_session, NOW) == []

    def test_days_without_usage_are_not_in_the_sample(self, db_session):
        """Ten days of usage spread over the window is ten samples, however
        many weekends and days off lie between them."""
        u = _seed_user(db_session, "parttime")
        for d in range(2, 42, 4):
            _days(db_session, u, 1, cost=1.0, first=d)
        _log(db_session, u, cost=2.5)
        db_session.commit()
        finding = self._found(db_session)[f"user:{u.id}"]
        assert (finding["baseline"], finding["severity"]) == (1.0, "warning")

    def test_critical_needs_the_amount_as_well_as_the_ratio(self, db_session):
        small = _seed_user(db_session, "small")
        big = _seed_user(db_session, "big")
        _days(db_session, small, 12, cost=0.3)
        _days(db_session, big, 12, cost=2.0)
        _log(db_session, small, cost=3.0)        # ten times its busy day, and $3
        _log(db_session, big, cost=12.0)         # six times, and over $10
        db_session.commit()
        found = self._found(db_session)
        assert found[f"user:{small.id}"]["severity"] == "warning"
        assert found[f"user:{big.id}"]["severity"] == "critical"

    def test_too_little_history_is_judged_by_the_amount_alone(self, db_session):
        newcomer = _seed_user(db_session, "newcomer")
        runaway = _seed_user(db_session, "runaway")
        _days(db_session, newcomer, 3, cost=0.1)
        _log(db_session, newcomer, cost=12.0)    # 120× three small days: a first real day, not a finding
        _log(db_session, runaway, cost=25.0)     # no history at all, and a lot of money
        db_session.commit()
        found = self._found(db_session)
        assert list(found) == [f"user:{runaway.id}"]
        assert found[f"user:{runaway.id}"]["severity"] == "warning"

    def test_service_accounts_are_watched_too(self, db_session):
        """A leaked service key shows as a spend spike."""
        app = _seed_user(db_session, "batch-runner", service=True)
        _days(db_session, app, 12, cost=1.0)
        _log(db_session, app, cost=5.0)
        db_session.commit()
        assert list(self._found(db_session)) == [f"user:{app.id}"]


class TestBurstRate:

    LIMIT = 6

    def _found(self, session):
        with patch("app.services.anomaly_scan.BURST_PER_HOUR", self.LIMIT), \
             patch("app.services.anomaly_scan.BURST_CRITICAL_PER_HOUR", self.LIMIT * 3):
            return detect_burst_rate(session, NOW)

    def test_burst_flagged_for_human_only(self, db_session):
        human = _seed_user(db_session, "fastfingers")
        app = _seed_user(db_session, "batch-runner", service=True)
        for _ in range(self.LIMIT + 1):
            _log(db_session, human, when=NOW - timedelta(minutes=30))
            _log(db_session, app, when=NOW - timedelta(minutes=30))
        db_session.commit()

        scopes = [f["scope"] for f in self._found(db_session)]
        assert f"user:{human.id}" in scopes
        assert f"user:{app.id}" not in scopes  # service accounts are exempt

    def test_one_row_per_account_per_day_holding_the_peak_hour(self, db_session):
        """A burst that lasts the afternoon is one finding, not one per scan."""
        u = _seed_user(db_session, "allday")
        for _ in range(self.LIMIT * 2):
            _log(db_session, u, when=NOW - timedelta(minutes=30))
        db_session.commit()
        first = self._found(db_session)
        persist(db_session, first)

        # The next hour's scan: still bursting, less than before.
        later = [dict(first[0], observed=self.LIMIT + 1, window_end=NOW + timedelta(hours=1))]
        assert later[0]["window_start"] == first[0]["window_start"] == local_day_start_utc()
        assert persist(db_session, later) == (0, 1)

        rows = db_session.exec(select(AnomalyEvent)).all()
        assert [(r.rule, r.observed) for r in rows] == [("burst_rate", self.LIMIT * 2)]


class TestOffHours:

    FLOOR = 5

    def _night(self, session, user, n):
        night = local_day_start_utc() + timedelta(hours=2)      # 02:00 local
        for _ in range(n):
            _log(session, user, when=night)

    def _day(self, session, user, days_ago, n):
        noon = local_day_start_utc() - timedelta(days=days_ago) + timedelta(hours=12)
        for _ in range(n):
            _log(session, user, when=noon)

    def _scopes(self, session):
        with patch("app.services.anomaly_scan.OFF_HOURS_MIN_REQUESTS", self.FLOOR):
            return [(f["scope"], f["observed"], f["baseline"]) for f in detect_off_hours(session, NOW)]

    def test_counts_only_local_night_requests_of_humans(self, db_session):
        night_owl = _seed_user(db_session, "night_owl")
        day_worker = _seed_user(db_session, "day_worker")
        app = _seed_user(db_session, "app_cron", service=True)
        night = local_day_start_utc() + timedelta(hours=2)      # 02:00 local
        day = local_day_start_utc() + timedelta(hours=7)        # 07:00 local
        for _ in range(OFF_HOURS_MIN_REQUESTS + 1):
            _log(db_session, night_owl, when=night)
            _log(db_session, app, when=night)
            _log(db_session, day_worker, when=day)
        _log(db_session, day_worker, when=night)
        db_session.commit()

        findings = detect_off_hours(db_session, NOW)
        assert [(f["scope"], f["observed"]) for f in findings] == [
            (f"user:{night_owl.id}", OFF_HOURS_MIN_REQUESTS + 1),
        ]

    def test_a_night_no_bigger_than_the_accounts_busy_day_is_ordinary(self, db_session):
        """Someone who runs agents overnight has days this large: nothing new."""
        heavy = _seed_user(db_session, "heavy")
        for d in range(1, 6):
            self._day(db_session, heavy, d, 40)
        self._night(db_session, heavy, 30)
        db_session.commit()
        assert self._scopes(db_session) == []

    def test_a_night_bigger_than_the_accounts_busy_day_is_flagged(self, db_session):
        light = _seed_user(db_session, "light")
        for d in range(1, 6):
            self._day(db_session, light, d, 8)
        self._night(db_session, light, 30)
        db_session.commit()
        assert self._scopes(db_session) == [(f"user:{light.id}", 30, 8)]

    def test_one_exceptional_day_does_not_become_the_standard(self, db_session):
        """The busy day is the 90th percentile, so with ten days or more a
        single outlier (an earlier incident) does not excuse the next one."""
        u = _seed_user(db_session, "once")
        for d in range(1, 11):
            self._day(db_session, u, d, 8)
        self._day(db_session, u, 11, 100)
        self._night(db_session, u, 30)
        db_session.commit()
        assert self._scopes(db_session) == [(f"user:{u.id}", 30, 8)]

    def test_below_the_floor_is_never_flagged(self, db_session):
        u = _seed_user(db_session, "quiet")
        self._day(db_session, u, 1, 1)
        self._night(db_session, u, self.FLOOR)
        db_session.commit()
        assert self._scopes(db_session) == []


class TestDetectAll:

    def test_a_failed_rule_is_rolled_back_so_later_rules_still_run(self, db_session):
        """On PostgreSQL a failed statement (e.g. the statement timeout)
        aborts the transaction; every later rule would then fail too."""
        def broken(session, now):
            raise RuntimeError("canceling statement due to statement timeout")

        with patch("app.services.anomaly_scan.detect_cost_spike", broken), \
             patch.object(db_session, "rollback", wraps=db_session.rollback) as rollback:
            detect_all(db_session, NOW)
        rollback.assert_called_once()


class TestEmptyTurnRate:

    def _seed(self, session, empties_by):
        """A healthy week, then a last hour of 30 responses, 12 of them empty
        and coming from ``empties_by`` (cycled)."""
        users = [_seed_user(session, name) for name in ("ann", "bob", "cy")]
        for d in range(1, 8):
            for _ in range(10):
                _log(session, users[0], out_tokens=500, when=NOW - timedelta(days=d), model="radLLM")
        for i in range(30):
            user = users[empties_by[i % len(empties_by)]] if i < 12 else users[i % 3]
            _log(session, user, out_tokens=(1 if i < 12 else 500),
                 when=NOW - timedelta(minutes=30), model="radLLM")
        session.commit()

    def test_model_level_spike_detected(self, db_session):
        self._seed(db_session, empties_by=[0, 1])
        findings = detect_empty_turn_rate(db_session, NOW)
        assert [(f["rule"], f["scope"], f["severity"]) for f in findings] == [
            ("empty_turn_rate", "model:radLLM", "critical"),
        ]
        assert findings[0]["window_start"] == local_day_start_utc()   # one row a day, like every rule

    def test_one_accounts_broken_client_is_not_the_model(self, db_session):
        self._seed(db_session, empties_by=[1])
        assert detect_empty_turn_rate(db_session, NOW) == []


class TestBehaviorShift:

    def test_a_new_model_type_is_recorded_as_info(self, db_session):
        """A first embedding job looks exactly like this: worth a record, not an alarm."""
        u = _seed_user(db_session, "rag")
        _days(db_session, u, 3)
        with patch("app.services.anomaly_scan.BEHAVIOR_SHIFT_MIN_REQUESTS", 3):
            for _ in range(4):
                _log(db_session, u, model="bge", model_type="embedding")
            db_session.commit()
            findings = detect_behavior_shift(db_session, NOW)
        assert [(f["scope"], f["severity"]) for f in findings] == [(f"user:{u.id}", "info")]


def _verdicts(unsafe, blocked=0, **categories):
    return {"unsafe": unsafe, "controversial": 0, "blocked": blocked, "categories": Counter(categories)}


class TestGuardrailUnsafe:
    """Verdicts live in Langfuse only: the rule reads counts per account from there."""

    @contextmanager
    def _langfuse(self, per_day, *, active=True, monitoring=True, sample_rate=1.0, cut=False):
        """``per_day``: one ``{account: verdicts}`` per read, today first."""
        reads = iter(per_day)
        flagged = MagicMock(side_effect=lambda *a, **k: (next(reads), cut))
        with patch("app.services.anomaly_scan.get_guardrail_settings", return_value={"active": active}), \
             patch("app.services.langfuse_reader.monitoring", return_value={"on": monitoring, "reason": ""}), \
             patch("app.services.langfuse_reader.sample_rate", return_value=sample_rate), \
             patch("app.services.langfuse_reader.new_client", return_value=MagicMock()), \
             patch("app.services.langfuse_reader.flagged_accounts", flagged):
            yield flagged

    # Well into the local day, so only today is read.
    MIDDAY = local_day_start_utc() + timedelta(hours=12)

    def test_repeated_unsafe_verdicts_are_a_finding_one_is_not(self, db_session):
        repeat = _seed_user(db_session, "repeat")
        once = _seed_user(db_session, "once")
        jail = _seed_user(db_session, "jail")
        app = _seed_user(db_session, "batch-runner", service=True)
        with self._langfuse([{
            "repeat": _verdicts(4, blocked=1, Violent=3, PII=1),
            "once": _verdicts(1, Violent=1),             # the guard misjudges: one verdict is not a finding
            "jail": _verdicts(2, Jailbreak=2),           # two jailbreak attempts are
            "batch-runner": _verdicts(3, Violent=3),     # a program's account counts too
            "gone": _verdicts(9, Violent=9),             # no such account any more
            "(unknown)": _verdicts(5),
        }]) as flagged:
            findings = detect_guardrail_unsafe(db_session, self.MIDDAY)

        assert {(f["scope"], f["observed"]) for f in findings} == {
            (f"user:{repeat.id}", 4), (f"user:{jail.id}", 2), (f"user:{app.id}", 3),
        }
        assert once.id not in {f["user_id"] for f in findings}
        first = next(f for f in findings if f["user_id"] == repeat.id)
        assert (first["rule"], first["severity"], first["window_start"]) == (
            "guardrail_unsafe", "warning", local_day_start_utc())
        assert "4 request(s) judged unsafe" in first["detail"] and "Violent ×3" in first["detail"]
        assert "1 refused" in first["detail"]
        # One read, of today's unsafe verdicts up to now.
        (_client, start, end, statuses, _pages), _kw = flagged.call_args
        assert flagged.call_count == 1
        assert (start, end, statuses) == (local_day_start_utc(), self.MIDDAY, ("unsafe",))

    def test_off_without_the_guardrail_or_langfuse_and_nothing_is_read(self, db_session):
        _seed_user(db_session, "repeat")
        for off in ({"active": False}, {"monitoring": False}):
            with self._langfuse([{"repeat": _verdicts(9)}], **off) as flagged:
                assert detect_guardrail_unsafe(db_session, self.MIDDAY) == []
            flagged.assert_not_called()

    def test_early_in_the_day_yesterday_is_read_once_more(self, db_session):
        """Yesterday's last scan ran before the day ended."""
        u = _seed_user(db_session, "late")
        day_start = local_day_start_utc()
        with self._langfuse([{}, {"late": _verdicts(3, Violent=3)}]) as flagged:
            findings = detect_guardrail_unsafe(db_session, day_start + timedelta(minutes=30))
        assert flagged.call_count == 2
        assert [(f["scope"], f["window_start"], f["window_end"]) for f in findings] == [
            (f"user:{u.id}", day_start - timedelta(days=1), day_start),
        ]

    def test_the_summary_says_when_the_count_is_a_sample_or_cut(self, db_session):
        _seed_user(db_session, "repeat")
        with self._langfuse([{"repeat": _verdicts(3)}], sample_rate=0.25, cut=True):
            (finding,) = detect_guardrail_unsafe(db_session, self.MIDDAY)
        assert "a sample" in finding["detail"] and "more verdicts than were read" in finding["detail"]

    def test_the_connection_is_released_before_langfuse_is_asked(self, db_session):
        """The read is HTTP and can take a while: not with a pooled connection held."""
        _seed_user(db_session, "repeat")
        order = []
        with self._langfuse([{"repeat": _verdicts(3)}]) as flagged, \
             patch.object(db_session, "rollback", side_effect=lambda: order.append("released")):
            flagged.side_effect = lambda *a, **k: (order.append("read") or {"repeat": _verdicts(3)}, False)
            detect_guardrail_unsafe(db_session, self.MIDDAY)
        assert order == ["released", "read"]

    def test_langfuse_down_costs_this_rule_only(self, db_session):
        from app.services.langfuse_reader import LangfuseError

        u = _seed_user(db_session, "spiky")
        _days(db_session, u, 12, cost=0.2)
        _log(db_session, u, cost=3.0)
        db_session.commit()
        with self._langfuse([]) as flagged:
            flagged.side_effect = LangfuseError("ReadTimeout reaching Langfuse")
            findings = detect_all(db_session, self.MIDDAY)
        assert [f["rule"] for f in findings] == ["cost_spike"]


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

    def test_a_lower_figure_does_not_replace_the_windows_peak(self, db_session):
        u = _seed_user(db_session, "peak")
        finding = {
            "scope": f"user:{u.id}", "rule": "burst_rate", "severity": "critical",
            "user_id": u.id, "model": None, "window_start": local_day_start_utc(),
            "window_end": NOW, "observed": 4000.0, "baseline": 1200.0, "ratio": 3.33,
            "detail": "{\"summary\": \"4000 requests in one hour\"}",
        }
        persist(db_session, [finding])
        later = NOW + timedelta(hours=1)
        persist(db_session, [dict(finding, observed=1300.0, ratio=1.08, severity="warning", window_end=later)])

        row = db_session.exec(select(AnomalyEvent)).one()
        assert (row.observed, row.severity, row.ratio) == (4000.0, "critical", 3.33)
        assert row.window_end == later.replace(tzinfo=None)     # still happening: the row says until when

    def test_open_findings_not_seen_again_are_closed(self, db_session):
        """Nothing else ever closed one, so the admin's list only grew."""
        stale = datetime.now(timezone.utc) - timedelta(days=OPEN_EXPIRY_DAYS + 1)
        fresh = datetime.now(timezone.utc) - timedelta(days=OPEN_EXPIRY_DAYS - 1)
        for scope, status, when in (("user:1", "new", stale), ("user:2", "acknowledged", stale),
                                    ("user:3", "new", fresh), ("user:4", "resolved", stale)):
            db_session.add(AnomalyEvent(
                scope=scope, rule="cost_spike", window_start=when, window_end=when,
                status=status, detail='{"summary": "x"}', created_at=when, updated_at=when,
            ))
        db_session.commit()

        assert close_stale(db_session) == 2
        rows = {r.scope: r for r in db_session.exec(select(AnomalyEvent)).all()}
        assert {scope: r.status for scope, r in rows.items()} == {
            "user:1": "resolved", "user:2": "resolved", "user:3": "new", "user:4": "resolved",
        }
        assert "not seen again" in rows["user:1"].detail and "summary" in rows["user:1"].detail
        assert prune_resolved(db_session) == 0      # kept as resolved for the retention period

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

    def test_info_findings_stay_out_of_needs_attention(self, client, db_session, admin_user):
        db_session.add(AnomalyEvent(
            scope="user:1", rule="behavior_shift", severity="info",
            window_start=NOW, window_end=NOW, observed=120, detail='{"summary": "new type"}',
        ))
        db_session.commit()
        h = web_auth_header(sub=admin_user.username, scopes=["admin"])
        assert "behavior_shift" not in client.get("/admin", headers=h).text
        listed = client.get("/admin/api/anomalies?status=new", headers=h).json()
        assert [e["rule"] for e in listed] == ["behavior_shift"]     # still on record

    def test_admin_page_shows_open_anomalies(self, client, db_session, admin_user):
        self._seed_event(db_session)
        resp = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"]))
        assert resp.status_code == 200
        assert "Usage anomalies" in resp.text
        assert "burst_rate" in resp.text
