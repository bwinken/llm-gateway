"""Weekly report mails: the Langfuse reader (langfuse_reader.py), what the
reports say (report_content.py, report_charts.py), their schedule and sending
(weekly_reports.py) and the admin card."""

from __future__ import annotations

import dataclasses
import json
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
from sqlmodel import select

from app.core.timeutil import local_day_start_utc_of
from app.models.schema import ClientMailLog, ReportSchedule, UsageLog, User
from app.services import langfuse_reader, mailer, observability, report_charts, report_content, usage_rollup, weekly_reports
from app.services.report_content import Period, Rendered
from tests.conftest import web_auth_header

WEEK = Period(date(2026, 11, 2))               # Monday 2 to Sunday 8 November
MONDAY_9 = datetime(2026, 11, 9, 1, 0)         # Monday 9 November 09:00 Asia/Taipei, as naive UTC


def _admin(admin: User) -> dict:
    return {**web_auth_header(sub=admin.username, scopes=["admin"]), "Accept": "application/json"}


@pytest.fixture
def mail_on(monkeypatch):
    for name, value in (
        ("EXCHANGE_SERVER", "mail.example.test"), ("EXCHANGE_USER", "EX\\svc"), ("EXCHANGE_PASSWORD", "pw"),
        ("EXCHANGE_EMAIL", "noreply@example.test"), ("COMPANY_EMAIL_DOMAIN", "example.test"),
    ):
        monkeypatch.setenv(name, value)
    sent: list[tuple] = []
    with patch.object(mailer, "_send_sync", side_effect=lambda *args: sent.append(args)):
        yield sent


@pytest.fixture(autouse=True)
def langfuse_on(monkeypatch):
    """Langfuse monitoring is on unless a test asks for ``langfuse_off``:
    the reports can only be turned on, and are only sent, while it is."""
    monkeypatch.setenv("LANGFUSE_HOST", "https://langfuse.example.test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.delenv("LANGFUSE_SAMPLE_RATE", raising=False)
    langfuse_reader.reset()
    observability.reset_langfuse_cache()
    yield
    langfuse_reader.reset()
    observability.reset_langfuse_cache()


@pytest.fixture
def langfuse_off(monkeypatch):
    for name in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def stub_reports(monkeypatch):
    """Both reports with a canned body: these tests are about when and to
    whom, not what the mail says."""
    built: list[tuple[str, Period]] = []

    def build(name):
        def _build(_session, period):
            built.append((name, period))
            return Rendered(f"{name} report", f"<p>{name}</p>", {"chart": b"png"})
        return _build

    monkeypatch.setattr(weekly_reports, "REPORTS", {
        name: dataclasses.replace(report, build=build(name)) for name, report in weekly_reports.REPORTS.items()
    })
    return built


def _fake_langfuse(handler):
    """``new_client`` answering from ``handler(request) -> httpx.Response``."""
    return patch.object(
        langfuse_reader, "new_client",
        lambda: httpx.Client(base_url="https://langfuse.example.test", transport=httpx.MockTransport(handler)),
    )


def _query(request: httpx.Request) -> dict:
    return json.loads(request.url.params["query"])


def _log(session, user_id: int, day: date, backend: str, model: str, tokens_in: int, tokens_out: int, hour: int = 4) -> None:
    session.add(UsageLog(
        user_id=user_id, model=model, model_type="llm", input_tokens=tokens_in, output_tokens=tokens_out,
        cost_usd=Decimal("0.5"), endpoint="/v1/chat/completions", backend=backend,
        created_at=(local_day_start_utc_of(day) + timedelta(hours=hour)).replace(tzinfo=None),
    ))


class TestLangfuseReader:
    def test_v2_first_then_v1_and_remembered(self, langfuse_on):
        paths: list[str] = []

        def handler(request):
            paths.append(request.url.path)
            if request.url.path == "/api/public/v2/metrics":
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"data": [{"providedModelName": "m", "count_count": "7"}]})

        with _fake_langfuse(handler), langfuse_reader.new_client() as client:
            assert langfuse_reader.count(langfuse_reader.metrics(client, {"view": "observations"})[0]) == 7
            langfuse_reader.metrics(client, {"view": "observations"})
        assert paths == ["/api/public/v2/metrics", "/api/public/metrics", "/api/public/metrics"]

    def test_a_failure_names_the_status_and_the_servers_message(self, langfuse_on):
        def handler(_request):
            return httpx.Response(400, json={"message": "Invalid dimension tags\nstack trace"})

        with _fake_langfuse(handler), langfuse_reader.new_client() as client:
            with pytest.raises(langfuse_reader.LangfuseError) as caught:
                langfuse_reader.metrics(client, {"view": "observations"})
        assert str(caught.value) == "HTTP 400: Invalid dimension tags"

    def test_a_timeout_is_reported_by_its_type(self, langfuse_on):
        def handler(request):
            raise httpx.ReadTimeout("", request=request)

        with _fake_langfuse(handler), langfuse_reader.new_client() as client:
            with pytest.raises(langfuse_reader.LangfuseError, match="ReadTimeout"):
                langfuse_reader.metrics(client, {"view": "observations"})

    def test_the_error_never_carries_the_key(self, langfuse_on):
        def handler(_request):
            return httpx.Response(401, text="nope")

        with langfuse_reader.new_client() as real:      # the real client signs with the key pair
            assert real.auth is not None and str(real.base_url).startswith("https://langfuse.example.test")

        with _fake_langfuse(handler), langfuse_reader.new_client() as client:
            with pytest.raises(langfuse_reader.LangfuseError) as caught:
                langfuse_reader.metrics(client, {"view": "observations"})
        assert "sk-lf-test" not in str(caught.value) and "pk-lf-test" not in str(caught.value)

    def test_a_metric_is_read_under_either_name_and_as_a_string(self):
        assert langfuse_reader.value({"p95_latency": "1234.5"}, "latency", "p95") == 1234.5
        assert langfuse_reader.value({"latency_p95": 12}, "latency", "p95") == 12.0
        assert langfuse_reader.value({"p95_latency": None}, "latency", "p95") is None
        assert langfuse_reader.value({}, "latency", "p95") is None

    def test_scores_are_paged_and_a_cut_is_reported(self, langfuse_on):
        def handler(request):
            assert request.url.path == "/api/public/v2/scores"
            assert request.url.params["name"] == "guardrail_action"
            assert request.url.params["traceTags"] == "guardrail:unsafe"
            page = int(request.url.params["page"])
            size = 100 if page < 3 else 40
            return httpx.Response(200, json={"data": [{"stringValue": "flag"}] * size, "meta": {"totalPages": 3}})

        args = dict(name="guardrail_action", trace_tags=["guardrail:unsafe"],
                    start=local_day_start_utc_of(WEEK.start), end=local_day_start_utc_of(WEEK.end))
        with _fake_langfuse(handler), langfuse_reader.new_client() as client:
            items, cut = langfuse_reader.scores(client, max_pages=10, **args)
            assert (len(items), cut) == (240, False)
            items, cut = langfuse_reader.scores(client, max_pages=2, **args)
            assert (len(items), cut) == (200, True)

    def test_not_configured(self, langfuse_off):
        assert not langfuse_reader.is_configured()
        with pytest.raises(langfuse_reader.LangfuseError, match="not configured"):
            langfuse_reader.new_client()


class TestPeriod:
    def test_last_week_is_the_full_week_before_this_one(self):
        for today in (date(2026, 11, 9), date(2026, 11, 12), date(2026, 11, 15)):   # Monday, Thursday, Sunday
            assert report_content.last_week(today) == WEEK
        assert WEEK.end == date(2026, 11, 9) and len(WEEK.days) == 7
        assert WEEK.previous == Period(date(2026, 10, 26))
        assert WEEK.label == "2026-11-02 to 2026-11-08"

    def test_the_week_turns_at_local_midnight_not_utc(self):
        # Sunday 23:30 Taipei is still the week of the 2nd; 00:30 on Monday is the next.
        assert weekly_reports.current_period(datetime(2026, 11, 8, 15, 30)) == WEEK.previous
        assert weekly_reports.current_period(datetime(2026, 11, 8, 16, 30)) == WEEK

    def test_slot_is_the_weekday_and_time_of_the_week_after(self):
        assert weekly_reports.slot(WEEK, 0, "09:00").isoformat() == "2026-11-09T09:00:00+08:00"
        assert weekly_reports.slot(WEEK, 2, "18:30").isoformat() == "2026-11-11T18:30:00+08:00"


class TestSettings:
    def test_recipients_are_company_mailboxes(self, mail_on):
        assert weekly_reports.parse_recipients("alice, bob@example.test;  Carol\nalice") == [
            "alice@example.test", "bob@example.test", "carol@example.test",
        ]
        assert weekly_reports.parse_recipients("") == []
        for bad in ("eve@elsewhere.test", "a@b@example.test"):
            with pytest.raises(ValueError):
                weekly_reports.parse_recipients(f"alice, {bad}")
        with pytest.raises(ValueError, match="At most"):
            weekly_reports.parse_recipients(", ".join(f"u{i}" for i in range(weekly_reports.MAX_RECIPIENTS + 1)))

    def test_time_and_weekday(self):
        assert weekly_reports.parse_time("9:05") == "09:05" and weekly_reports.parse_time("23:59") == "23:59"
        for bad in ("24:00", "9", "09:60", "", "soon"):
            with pytest.raises(ValueError):
                weekly_reports.parse_time(bad)
        assert weekly_reports.parse_weekday("6") == 6
        for bad in ("7", "-1", "x", None):
            with pytest.raises(ValueError):
                weekly_reports.parse_weekday(bad)

    def test_an_enabled_report_needs_a_recipient(self, db_session):
        with pytest.raises(ValueError, match="recipient"):
            weekly_reports.save_schedule(db_session, "usage", enabled=True, weekday=0, send_time="09:00", recipients=[])
        with pytest.raises(ValueError, match="Unknown"):
            weekly_reports.save_schedule(db_session, "nope", enabled=False, weekday=0, send_time="09:00", recipients=[])
        assert db_session.exec(select(ReportSchedule)).all() == []

    def test_a_report_without_a_row_is_off(self, db_session, mail_on, stub_reports):
        views = weekly_reports.schedules(db_session, now=MONDAY_9)
        assert [(v["name"], v["enabled"], v["next"]) for v in views] == [("usage", False, None), ("guardrail", False, None)]
        assert weekly_reports.run_due(db_session, now=MONDAY_9) == "nothing due"
        assert mail_on == []


def _schedule(session, name="usage", *, now, weekday=0, send_time="09:00", recipients=("alice@example.test", "bob@example.test")):
    return weekly_reports.save_schedule(
        session, name, enabled=True, weekday=weekday, send_time=send_time, recipients=list(recipients), now=now,
    )


class TestSchedule:
    def test_sent_once_at_its_time_as_one_mail_to_everyone(self, db_session, mail_on, stub_reports):
        _schedule(db_session, now=MONDAY_9 - timedelta(days=3))
        assert weekly_reports.run_due(db_session, now=MONDAY_9 - timedelta(minutes=1)) == "nothing due"
        assert mail_on == []

        assert weekly_reports.run_due(db_session, now=MONDAY_9) == "usage=sent"
        assert len(mail_on) == 1                      # one mail, not one per recipient
        to, subject, body, images = mail_on[0]
        assert to == ["alice@example.test", "bob@example.test"]
        assert subject == "usage report" and images == {"chart": b"png"}
        assert stub_reports == [("usage", WEEK)]

        for later in (timedelta(minutes=5), timedelta(days=2), timedelta(days=6, hours=23)):
            assert weekly_reports.run_due(db_session, now=MONDAY_9 + later) == "nothing due"
        assert len(mail_on) == 1

        assert weekly_reports.run_due(db_session, now=MONDAY_9 + timedelta(days=7)) == "usage=sent"
        assert stub_reports[-1] == ("usage", Period(date(2026, 11, 9)))

    def test_each_report_keeps_its_own_time_and_recipients(self, db_session, mail_on, stub_reports, langfuse_on):
        _schedule(db_session, "usage", now=MONDAY_9 - timedelta(days=3))
        _schedule(db_session, "guardrail", now=MONDAY_9 - timedelta(days=3), weekday=2, send_time="18:30",
                  recipients=["carol@example.test"])
        assert weekly_reports.run_due(db_session, now=MONDAY_9) == "usage=sent"
        wednesday = datetime(2026, 11, 11, 10, 30)          # 18:30 Asia/Taipei
        assert weekly_reports.run_due(db_session, now=wednesday - timedelta(minutes=1)) == "nothing due"
        assert weekly_reports.run_due(db_session, now=wednesday) == "guardrail=sent"
        assert [args[0] for args in mail_on] == [["alice@example.test", "bob@example.test"], ["carol@example.test"]]
        assert stub_reports == [("usage", WEEK), ("guardrail", WEEK)]     # both about the same last week

    def test_a_gateway_that_was_down_sends_when_it_is_back(self, db_session, mail_on, stub_reports):
        _schedule(db_session, now=MONDAY_9 - timedelta(days=3))
        assert weekly_reports.run_due(db_session, now=MONDAY_9 + timedelta(days=3)) == "usage=sent"
        assert stub_reports == [("usage", WEEK)]

    def test_saving_after_this_weeks_time_does_not_mail(self, db_session, mail_on, stub_reports):
        row = _schedule(db_session, now=MONDAY_9 + timedelta(hours=5))
        assert row.last_status == "skipped" and row.last_period == WEEK.start
        assert weekly_reports.run_due(db_session, now=MONDAY_9 + timedelta(hours=6)) == "nothing due"
        assert mail_on == []
        view = weekly_reports.schedules(db_session, now=MONDAY_9 + timedelta(hours=6))[0]
        assert view["next"] == "Mon 2026-11-16 09:00"
        assert weekly_reports.run_due(db_session, now=MONDAY_9 + timedelta(days=7)) == "usage=sent"

    def test_an_edit_after_the_mail_went_out_does_not_send_it_again(self, db_session, mail_on, stub_reports):
        _schedule(db_session, now=MONDAY_9 - timedelta(days=3))
        weekly_reports.run_due(db_session, now=MONDAY_9)
        _schedule(db_session, now=MONDAY_9 + timedelta(hours=1), weekday=1, send_time="08:00", recipients=["carol@example.test"])
        assert weekly_reports.run_due(db_session, now=MONDAY_9 + timedelta(days=1)) == "nothing due"
        assert len(mail_on) == 1
        assert db_session.get(ReportSchedule, "usage").last_status == "sent"

    def test_one_claim_per_week(self, db_session, mail_on, stub_reports):
        _schedule(db_session, now=MONDAY_9 - timedelta(days=3))
        assert weekly_reports._claim(db_session, "usage", WEEK, MONDAY_9) is True
        assert weekly_reports._claim(db_session, "usage", WEEK, MONDAY_9) is False      # the other worker
        assert weekly_reports._claim(db_session, "usage", WEEK.previous, MONDAY_9) is False

    def test_a_failed_send_is_tried_again_later_the_same_week(self, db_session, mail_on, stub_reports):
        _schedule(db_session, now=MONDAY_9 - timedelta(days=3))
        with patch.object(mailer, "_send_sync", side_effect=TimeoutError("timed out")):
            assert weekly_reports.run_due(db_session, now=MONDAY_9) == "usage=failed"
        row = db_session.get(ReportSchedule, "usage")
        db_session.refresh(row)
        assert row.last_status == "failed" and "TimeoutError" in row.last_detail
        assert weekly_reports.run_due(db_session, now=MONDAY_9 + timedelta(hours=5)) == "nothing due"
        assert weekly_reports.run_due(db_session, now=MONDAY_9 + timedelta(seconds=weekly_reports.RETRY_S)) == "usage=sent"
        assert len(mail_on) == 1

    def test_off_means_off(self, db_session, mail_on, stub_reports):
        _schedule(db_session, now=MONDAY_9 - timedelta(days=3))
        weekly_reports.save_schedule(
            db_session, "usage", enabled=False, weekday=0, send_time="09:00", recipients=["alice@example.test"],
            now=MONDAY_9 - timedelta(days=2),
        )
        assert weekly_reports.run_due(db_session, now=MONDAY_9) == "nothing due"
        assert mail_on == []

    def test_nothing_is_claimed_while_mail_is_not_configured(self, db_session, stub_reports, monkeypatch):
        monkeypatch.setenv("COMPANY_EMAIL_DOMAIN", "example.test")
        row = weekly_reports.save_schedule(
            db_session, "usage", enabled=True, weekday=0, send_time="09:00", recipients=["alice@example.test"],
            now=MONDAY_9 - timedelta(days=3),
        )
        for name in ("EXCHANGE_SERVER", "EXCHANGE_PASSWORD"):
            monkeypatch.delenv(name, raising=False)
        assert weekly_reports.run_due(db_session, now=MONDAY_9) == "usage=mail not configured"
        db_session.refresh(row)
        assert row.last_period == WEEK.previous.start        # this week's is still due once mail works

    def test_every_recipient_is_in_the_mail_log(self, db_session, mail_on, stub_reports):
        _schedule(db_session, now=MONDAY_9 - timedelta(days=3))
        weekly_reports.run_due(db_session, now=MONDAY_9)
        rows = db_session.exec(select(ClientMailLog).order_by(ClientMailLog.address)).all()
        assert [(r.address, r.rule_ids, r.kind, r.ok) for r in rows] == [
            ("alice@example.test", "report:usage", "report", True),
            ("bob@example.test", "report:usage", "report", True),
        ]

    def test_a_report_that_cannot_be_built_is_a_failure_not_an_exception(self, db_session, mail_on, monkeypatch):
        def boom(_session, _period):
            raise RuntimeError("secret detail")

        monkeypatch.setattr(weekly_reports, "REPORTS", {
            "usage": dataclasses.replace(weekly_reports.REPORTS["usage"], build=boom),
        })
        _schedule(db_session, now=MONDAY_9 - timedelta(days=3))
        assert weekly_reports.run_due(db_session, now=MONDAY_9) == "usage=failed"
        detail = db_session.get(ReportSchedule, "usage").last_detail
        assert "RuntimeError" in detail and "secret detail" not in detail
        assert mail_on == []

    def test_the_job_is_wired_into_the_gateway(self):
        import inspect

        from app import main

        assert weekly_reports.JOB.name == "weekly_reports" and weekly_reports.JOB.interval() == 300.0
        assert not hasattr(weekly_reports.REPORTS["usage"], "needs_langfuse")     # one rule for both reports
        assert "weekly_reports.JOB" in inspect.getsource(main.lifespan)


class TestNeedsLangfuseMonitoring:
    """Both reports are built on what Langfuse records: neither can be
    turned on, and nothing is sent, while monitoring is off."""

    def test_monitoring_is_the_settings_and_a_sample_rate(self, monkeypatch):
        assert langfuse_reader.monitoring() == {"on": True, "reason": ""}
        monkeypatch.setenv("LANGFUSE_SAMPLE_RATE", "0")
        observability.reset_langfuse_cache()
        state = langfuse_reader.monitoring()
        assert state["on"] is False and "LANGFUSE_SAMPLE_RATE is 0" in state["reason"]
        monkeypatch.setenv("LANGFUSE_SAMPLE_RATE", "0.25")
        observability.reset_langfuse_cache()
        assert langfuse_reader.monitoring()["on"] is True
        monkeypatch.delenv("LANGFUSE_SECRET_KEY")
        state = langfuse_reader.monitoring()
        assert state["on"] is False and "LANGFUSE_SECRET_KEY" in state["reason"] and "sk-lf-test" not in state["reason"]

    @pytest.mark.parametrize("name", ["usage", "guardrail"])
    def test_a_report_cannot_be_turned_on_while_it_is_off(self, db_session, mail_on, langfuse_off, name):
        with pytest.raises(ValueError, match="Langfuse is not configured"):
            _schedule(db_session, name, now=MONDAY_9 - timedelta(days=3))
        assert db_session.get(ReportSchedule, name) is None
        # Its day, time and recipients can still be prepared, off.
        row = weekly_reports.save_schedule(
            db_session, name, enabled=False, weekday=3, send_time="07:30", recipients=["alice@example.test"],
        )
        assert (row.enabled, row.weekday, row.recipients) == (False, 3, "alice@example.test")

    def test_recording_nothing_counts_as_off(self, db_session, mail_on, monkeypatch):
        monkeypatch.setenv("LANGFUSE_SAMPLE_RATE", "0")
        observability.reset_langfuse_cache()
        with pytest.raises(ValueError, match="LANGFUSE_SAMPLE_RATE is 0"):
            _schedule(db_session, now=MONDAY_9 - timedelta(days=3))

    def test_one_that_is_on_can_still_be_edited_or_turned_off(self, db_session, mail_on, monkeypatch):
        _schedule(db_session, now=MONDAY_9 - timedelta(days=3))
        monkeypatch.delenv("LANGFUSE_HOST")
        row = _schedule(db_session, now=MONDAY_9 - timedelta(days=2), send_time="10:00", recipients=["carol@example.test"])
        assert (row.enabled, row.send_time, row.recipients) == (True, "10:00", "carol@example.test")
        row = weekly_reports.save_schedule(
            db_session, "usage", enabled=False, weekday=0, send_time="10:00", recipients=["carol@example.test"],
        )
        assert row.enabled is False
        with pytest.raises(ValueError, match="Langfuse"):          # and not back on
            _schedule(db_session, now=MONDAY_9 - timedelta(days=1))

    def test_a_due_report_waits_and_goes_out_once_monitoring_is_back(self, db_session, mail_on, stub_reports, monkeypatch):
        _schedule(db_session, "usage", now=MONDAY_9 - timedelta(days=3))
        _schedule(db_session, "guardrail", now=MONDAY_9 - timedelta(days=3))
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY")
        assert weekly_reports.run_due(db_session, now=MONDAY_9) == "usage=langfuse monitoring off guardrail=langfuse monitoring off"
        assert mail_on == [] and stub_reports == []
        assert db_session.get(ReportSchedule, "usage").last_period == WEEK.previous.start     # not claimed
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
        assert weekly_reports.run_due(db_session, now=MONDAY_9 + timedelta(hours=2)) == "usage=sent guardrail=sent"
        assert stub_reports == [("usage", WEEK), ("guardrail", WEEK)]

    def test_nothing_is_sent_by_hand_either(self, client, admin_user, db_session, mail_on, stub_reports, langfuse_off):
        assert weekly_reports.send(db_session, "usage", ["alice@example.test"])[:2] == (
            False, "Langfuse is not configured (LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY in .env).",
        )
        for to in ("me", "recipients"):
            reply = client.post("/admin/reports/usage/send", headers=_admin(admin_user), data={"to": to})
            assert reply.status_code == 400 and "Langfuse is not configured" in reply.json()["detail"]
        assert mail_on == [] and stub_reports == []
        assert db_session.exec(select(ClientMailLog)).all() == []

    def test_the_admin_form_is_refused_and_the_switch_is_locked(self, client, admin_user, db_session, mail_on, langfuse_off):
        reply = client.post("/admin/reports/guardrail", headers=_admin(admin_user), data={
            "enabled": "on", "weekday": "0", "send_time": "09:00", "recipients": "alice",
        })
        assert reply.status_code == 400 and "Langfuse is not configured" in reply.json()["detail"]
        assert db_session.get(ReportSchedule, "guardrail") is None
        saved = client.post("/admin/reports/guardrail", headers=_admin(admin_user), data={
            "enabled": "off", "weekday": "0", "send_time": "09:00", "recipients": "alice",
        })
        assert saved.status_code == 200 and saved.json()["enabled"] is False

        body = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        card = body[body.index('id="weekly-reports"'):body.index(">Site Links</h2>")]
        assert 'id="weekly-reports-langfuse-off"' in card and "Langfuse is not configured" in card
        assert card.count('name="enabled" value="on"  disabled') == 2          # both switches locked
        assert card.count('data-report-send="me" class="gw-btn gw-btn-ghost text-sm px-3 py-1.5" disabled') == 2
        assert "/admin/reports/usage/preview" in card                             # the preview stays

    def test_the_switch_is_free_while_monitoring_is_on(self, client, admin_user, mail_on):
        body = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        card = body[body.index('id="weekly-reports"'):body.index(">Site Links</h2>")]
        assert "weekly-reports-langfuse-off" not in card and " disabled" not in card


def _observation_handler(seen: list | None = None, *, fail: bool = False):
    """A Langfuse that knows two models: one fine, one failing a tenth of
    its requests."""
    def handler(request):
        query = _query(request)
        if seen is not None:
            seen.append(query)
        if fail:
            return httpx.Response(503, json={"message": "ClickHouse is down"})
        assert query["view"] == "observations" and query["filters"] == []
        return httpx.Response(200, json={"data": [
            {"providedModelName": "test-llm", "level": "DEFAULT", "count_count": "900",
             "p50_latency": "1200", "p95_latency": "8400", "p50_timeToFirstToken": "300", "p95_timeToFirstToken": "950"},
            {"providedModelName": "test-llm", "level": "ERROR", "count_count": "100", "p50_latency": "30000", "p95_latency": "30000"},
            {"providedModelName": "test-llm", "level": "WARNING", "count_count": "5"},
            {"providedModelName": "<b>odd</b>", "level": "DEFAULT", "count_count": "10", "p50_latency": "90", "p95_latency": "400"},
        ]})
    return handler


class TestUsageReport:
    @pytest.fixture
    def week_of_usage(self, db_session, test_user):
        _log(db_session, test_user.id, date(2026, 11, 2), "vllm", "test-llm", 1000, 100)
        _log(db_session, test_user.id, date(2026, 11, 2), "azure", "gpt-test", 2000, 200)
        _log(db_session, test_user.id, date(2026, 11, 8), "vllm", "test-llm", 3000, 300, hour=23)   # Sunday 23:00 local
        _log(db_session, test_user.id, date(2026, 11, 5), "bedrock", "claude-test", 500, 50)
        _log(db_session, test_user.id, date(2026, 11, 9), "vllm", "test-llm", 9, 9, hour=0)         # next week
        _log(db_session, test_user.id, date(2026, 10, 28), "vllm", "test-llm", 2000, 200)           # the week before
        db_session.commit()

    def test_tokens_by_backend_and_day(self, db_session, week_of_usage):
        figures = report_content.usage_figures(db_session, WEEK)
        assert figures["backends"] == ["vllm", "azure", "bedrock"]
        assert {b: t["tokens"] for b, t in figures["totals"].items()} == {"vllm": 4400, "azure": 2200, "bedrock": 550}
        assert figures["totals"]["vllm"]["requests"] == 2
        assert figures["daily"]["vllm"] == [1100, 0, 0, 0, 0, 0, 3300]
        assert figures["daily"]["bedrock"][3] == 550
        assert figures["previous"] == {"vllm": {"tokens": 2200, "requests": 1}}
        assert figures["accounts"] == 1

    def test_estimated_cost_per_day_and_per_week(self, db_session, week_of_usage):
        _log(db_session, 1, date(2026, 9, 14), "azure", "gpt-test", 1, 1)       # eight weeks back: the first point
        _log(db_session, 1, date(2026, 9, 13), "azure", "gpt-test", 1, 1)       # a day earlier: off the chart
        db_session.commit()
        figures = report_content.usage_figures(db_session, WEEK)
        assert figures["cost_week_starts"][0] == date(2026, 9, 14) and figures["cost_week_starts"][-1] == WEEK.start
        assert figures["cost_weeks"]["vllm"] == [0, 0, 0, 0, 0, 0, 0.5, 1.0]
        assert figures["cost_weeks"]["azure"] == [0.5, 0, 0, 0, 0, 0, 0, 0.5]
        assert figures["cost_daily"]["vllm"] == [0.5, 0, 0, 0, 0, 0, 0.5]
        mail = report_content.render_usage(WEEK, figures, None, "Langfuse is not configured.")
        assert "Estimated cost" in mail.html and "an estimate, not a cloud invoice" in mail.html
        assert {"cost_by_day", "cost_trend"} <= set(mail.images)
        assert "$2.00" in mail.html           # this week's total: four requests at $0.50

    def test_the_same_numbers_once_rolled_up(self, db_session, week_of_usage):
        before = report_content.usage_figures(db_session, WEEK)
        usage_rollup.advance(db_session, now=datetime(2026, 11, 12, 4, 0))
        assert usage_rollup.rolled_through(db_session) >= date(2026, 11, 8)
        assert report_content.usage_figures(db_session, WEEK) == before

    def test_latency_and_error_rate_come_from_langfuse_per_local_day(self, db_session, week_of_usage, langfuse_on):
        seen: list[dict] = []
        with _fake_langfuse(_observation_handler(seen)):
            mail = report_content.usage_report(db_session, WEEK)
        assert mail.problems == []
        assert mail.subject.endswith("· ⚠ 1 to look at")
        assert "failed 10.0% of its requests" in mail.html and "100 of 1,000" in mail.html
        assert "8.4 s" in mail.html and "1.2 s" in mail.html and "950 ms" in mail.html
        # Tokens from the gateway's own record, whatever Langfuse says.
        assert "4,400" in mail.html and "2,200" in mail.html and "550" in mail.html
        # Each day is its own query, bounded by local midnights (16:00 UTC).
        windows = {(q["fromTimestamp"], q["toTimestamp"]) for q in seen}
        assert ("2026-11-01T16:00:00.000Z", "2026-11-08T16:00:00.000Z") in windows          # the week
        assert ("2026-10-25T16:00:00.000Z", "2026-11-01T16:00:00.000Z") in windows          # the week before
        assert ("2026-11-04T16:00:00.000Z", "2026-11-05T16:00:00.000Z") in windows          # Thursday
        assert len(seen) == 10
        assert set(mail.images) == {"tokens_by_day", "cost_by_day", "cost_trend", "latency_vllm", "error_rate"}
        assert all(png.startswith(b"\x89PNG") for png in mail.images.values())
        for name in mail.images:
            assert f'src="cid:{name}"' in mail.html

    def test_langfuse_down_costs_its_sections_not_the_mail(self, db_session, week_of_usage, langfuse_on):
        with _fake_langfuse(_observation_handler(fail=True)):
            mail = report_content.usage_report(db_session, WEEK)
        assert mail.problems == ["langfuse: HTTP 503: ClickHouse is down"]
        assert "could not be read from Langfuse: HTTP 503: ClickHouse is down" in mail.html
        assert "4,400" in mail.html and "tokens_by_day" in mail.images

    def test_without_langfuse_it_is_the_token_report(self, db_session, week_of_usage, langfuse_off):
        mail = report_content.usage_report(db_session, WEEK)
        assert "Langfuse is not configured" in mail.html and "4,400" in mail.html
        assert set(mail.images) == {"tokens_by_day", "cost_by_day", "cost_trend"}

    def test_what_comes_from_outside_is_escaped(self, db_session, week_of_usage, langfuse_on):
        _log(db_session, 1, date(2026, 11, 3), "vllm", "<script>x</script>", 5, 5)
        db_session.commit()
        with _fake_langfuse(_observation_handler()):
            mail = report_content.usage_report(db_session, WEEK)
        assert "<script>" not in mail.html and "&lt;script&gt;x&lt;/script&gt;" in mail.html
        assert "<b>odd</b>" not in mail.html and "&lt;b&gt;odd&lt;/b&gt;" in mail.html

    def test_a_chart_that_fails_costs_the_picture_only(self, db_session, week_of_usage, langfuse_off):
        with patch.object(report_charts, "stacked_columns", side_effect=RuntimeError("no font")):
            mail = report_content.usage_report(db_session, WEEK)
        assert set(mail.images) == {"cost_trend"}
        assert mail.problems[-2:] == ["chart tokens_by_day: RuntimeError", "chart cost_by_day: RuntimeError"]
        assert "4,400" in mail.html and 'src="cid:tokens_by_day"' not in mail.html

    def test_an_empty_week_still_renders(self, db_session, langfuse_off):
        mail = report_content.usage_report(db_session, WEEK)
        assert "No usage was recorded this week." in mail.html and mail.images == {}
        assert "No cost was recorded in the last 8 weeks." in mail.html

    def test_a_slower_p95_is_pointed_out(self):
        entry = {"ok": 500, "errors": 0, "blocked": 0, "latency_p50": 1000.0, "latency_p95": 9000.0}
        then = {"ok": 500, "errors": 0, "blocked": 0, "latency_p50": 1000.0, "latency_p95": 4000.0}
        usage = {"backends": [], "totals": {}, "daily": {}, "models": [], "previous": {}, "accounts": 0}
        observed = {"week": {"test-llm": entry}, "previous": {"test-llm": then}, "days": [{}] * 7, "ttft": None}
        assert "got slower: p95 9.0 s, from 4.0 s last week" in report_content.render_usage(WEEK, usage, observed).html
        observed["previous"]["test-llm"] = {**then, "latency_p95": 7000.0}
        assert "got slower" not in report_content.render_usage(WEEK, usage, observed).html


def _flagged(unsafe=0, controversial=0, blocked=0, **categories) -> dict:
    return {"unsafe": unsafe, "controversial": controversial, "blocked": blocked, "categories": Counter(categories)}


def _figures(week: dict, previous: dict | None = None, accounts: dict | None = None, **extra) -> dict:
    counts = Counter({("guardrail", "safe"): 1000, **week})
    return {
        "week": counts, "days": [counts] + [Counter()] * 6, "previous": Counter(previous or {("guardrail", "safe"): 1000}),
        "accounts": accounts or {}, "accounts_before": {}, "accounts_truncated": False, "accounts_error": "", **extra,
    }


class TestGuardrailReport:
    def test_who_is_pointed_out(self):
        reasons = report_content.account_reasons
        assert reasons(_flagged(unsafe=4), 1000) == []
        assert reasons(_flagged(unsafe=5), 1000) == ["5 unsafe verdicts"]
        assert reasons(_flagged(unsafe=1, blocked=3), 1000) == ["3 requests refused"]
        assert reasons(_flagged(unsafe=2, Jailbreak=2), 1000) == ["2 jailbreak attempts"]
        assert reasons(_flagged(unsafe=3), 12) == ["25% of its local requests judged unsafe"]
        assert reasons(_flagged(unsafe=2), 4) == []                  # too few to call a share
        assert reasons(_flagged(unsafe=3), None) == []               # an account that no longer exists
        assert reasons(_flagged(controversial=40), 100) == []        # controversial alone points at nobody

    def test_the_thresholds_are_code(self):
        fields = set(ReportSchedule.model_fields)
        assert fields == {
            "name", "enabled", "weekday", "send_time", "recipients", "last_period", "last_attempt_at",
            "last_status", "last_detail", "updated_by", "updated_at",
        }

    def test_alerts_and_accounts(self):
        accounts = {
            "alice": _flagged(unsafe=9, controversial=2, Jailbreak=6, PII=3),
            "<i>bob</i>": _flagged(unsafe=1),
            "batch": _flagged(unsafe=3),
        }
        figures = _figures(
            {("guardrail", "unsafe"): 40, ("guardrail", "controversial"): 12, ("guardrail", "timeout"): 80,
             ("guardrail_category", "Jailbreak"): 6, ("guardrail_category", "PII"): 3},
            previous={("guardrail", "safe"): 900, ("guardrail", "unsafe"): 10, ("guardrail_category", "PII"): 2},
            accounts=accounts,
        )
        with patch.object(report_content.config, "get_guardrail_settings",
                          return_value={"active": True, "action": "monitor", "block_threshold": "unsafe"}):
            mail = report_content.render_guardrail(WEEK, figures, {"alice": 200, "batch": 10}, {"batch"})
        assert mail.subject.endswith("· ⚠ 4 to look at")
        assert "did not judge <b>7.1%</b>" in mail.html and "80 timeout" in mail.html
        assert "Unsafe verdicts rose to <b>40</b>, from 10 last week." in mail.html
        assert "did not appear last week: <b>Jailbreak (6)</b>" in mail.html
        assert "<b>2 accounts</b> to look at: alice, batch" in mail.html
        assert "9 unsafe verdicts; 6 jailbreak attempts" in mail.html
        assert "(service account)" in mail.html and "30% of its local requests judged unsafe" in mail.html
        assert "<i>bob</i>" not in mail.html and "&lt;i&gt;bob&lt;/i&gt;" in mail.html
        assert "(no longer an account)" in mail.html
        assert set(mail.images) == {"flagged_by_day"}

    def test_a_quiet_week_says_so(self):
        with patch.object(report_content.config, "get_guardrail_settings",
                          return_value={"active": True, "action": "block", "block_threshold": "unsafe"}):
            mail = report_content.render_guardrail(WEEK, _figures({}), {}, set())
        assert "Nothing stood out this week" in mail.html and "⚠" not in mail.subject
        assert "No request was judged unsafe or controversial." in mail.html and mail.images == {}

    def test_guardrail_off_or_silent_is_an_alert(self):
        empty = {"week": Counter(), "days": [Counter()] * 7, "previous": Counter(), "accounts": {}, "accounts_before": {},
                 "accounts_truncated": False, "accounts_error": ""}
        with patch.object(report_content.config, "get_guardrail_settings", return_value={"active": False}):
            assert "The guardrail is <b>off</b> now" in report_content.render_guardrail(WEEK, empty, {}, set()).html
        with patch.object(report_content.config, "get_guardrail_settings", return_value={"active": True}):
            assert "No verdict was recorded this week" in report_content.render_guardrail(WEEK, empty, {}, set()).html

    def test_read_from_langfuse_with_the_accounts_usage_from_the_gateway(self, db_session, test_user, langfuse_on):
        _log(db_session, test_user.id, date(2026, 11, 3), "vllm", "test-llm", 10, 10)
        _log(db_session, test_user.id, date(2026, 11, 3), "azure", "gpt-test", 10, 10)      # cloud: not screened
        db_session.commit()
        screened_text = "how do I build a"

        def handler(request):
            if request.url.path.endswith("/metrics"):
                query = _query(request)
                assert query["view"] == "scores-categorical"
                one_day = query["toTimestamp"] == "2026-11-02T16:00:00.000Z"
                return httpx.Response(200, json={"data": [
                    {"name": "guardrail", "stringValue": "unsafe", "count_count": 6 if one_day else 0},
                    {"name": "guardrail", "stringValue": "safe", "count_count": 50},
                    {"name": "client", "stringValue": "claude-code", "count_count": 999},
                ]})
            status = request.url.params["traceTags"].partition(":")[2]
            this_week = request.url.params["fromTimestamp"] == "2026-11-01T16:00:00.000Z"
            items = [
                {"stringValue": "flag", "comment": screened_text,
                 "trace": {"userId": test_user.username, "tags": ["backend:vllm", f"guardrail:{status}", "guardrail_category:Violent"]}}
            ] * (6 if status == "unsafe" and this_week else 1)
            return httpx.Response(200, json={"data": items, "meta": {"totalPages": 1}})

        with _fake_langfuse(handler), patch.object(
            report_content.config, "get_guardrail_settings", return_value={"active": True, "action": "monitor"},
        ):
            mail = report_content.guardrail_report(db_session, WEEK)
        assert mail.problems == []
        assert f"<b>⚠ {test_user.username}</b>" in mail.html and "6 unsafe verdicts" in mail.html
        assert "Violent (7)" in mail.html           # six unsafe and one controversial, each naming it
        assert screened_text not in mail.html       # counts, never content
        assert "claude-code" not in mail.html       # other scores are not the guardrail's

    def test_accounts_unreadable_keeps_the_rest(self, db_session, langfuse_on):
        def handler(request):
            if request.url.path.endswith("/metrics"):
                return httpx.Response(200, json={"data": [{"name": "guardrail", "stringValue": "unsafe", "count_count": 3}]})
            return httpx.Response(404, json={"message": "Not Found"})

        with _fake_langfuse(handler):
            mail = report_content.guardrail_report(db_session, WEEK)
        assert "The accounts behind the verdicts could not be read from Langfuse: HTTP 404: Not Found" in mail.html
        assert mail.problems == ["langfuse accounts: HTTP 404: Not Found"]
        assert "flagged_by_day" in mail.images

    def test_without_langfuse_it_says_so(self, db_session, langfuse_off):
        mail = report_content.guardrail_report(db_session, WEEK)
        assert "Langfuse is not configured." in mail.html and mail.problems == ["langfuse: Langfuse is not configured."]


class TestCharts:
    def test_each_chart_is_a_png(self):
        days = list(report_content.WEEKDAYS)
        series = [("Local", report_charts.BLUE, [1.0, 2.0, None, 4.0, 0.0, 0.0, 3.0]),
                  ("Azure", report_charts.ORANGE, [None] * 7)]
        for png in (
            report_charts.stacked_columns(days, series),
            report_charts.stacked_columns(days, [("Local", report_charts.BLUE, [0.0] * 7)]),
            report_charts.lines(days, series, str),
            report_charts.line_panels(days, [(f"model-{i}", series) for i in range(4)], str),
        ):
            assert png.startswith(b"\x89PNG\r\n")

    def test_compact_numbers(self):
        assert [report_charts.compact(n) for n in (0, 999, 9999, 12_900, 4_200_000, 1_300_000_000)] == [
            "0", "999", "9,999", "12.9K", "4.2M", "1.3B",
        ]

    def test_pyplot_is_not_used(self):
        import inspect

        source = inspect.getsource(report_charts)
        assert "matplotlib.pyplot" not in source and "import pyplot" not in source


class TestMailer:
    def test_several_recipients_and_inline_images(self, mail_on):
        assert mailer.send(["a@example.test", "b@example.test"], "s", "<img src='cid:x'>", {"x": b"png"}) == (True, "")
        assert mailer.send("a@example.test", "s", "b") == (True, "")
        assert mail_on == [
            (["a@example.test", "b@example.test"], "s", "<img src='cid:x'>", {"x": b"png"}),
            ("a@example.test", "s", "b"),                # the three-argument call the other mail paths make
        ]


class TestAdmin:
    def test_the_card_is_on_the_settings_tab(self, client, admin_user):
        body = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        card = body.index('id="weekly-reports"')
        assert body.index('data-panel="settings"') < card < body.index("gwTabs('adminTabs'")
        assert card > body.index('data-panel="security"')
        assert 'action="/admin/reports/usage"' in body and 'action="/admin/reports/guardrail"' in body
        assert "/admin/reports/guardrail/preview" in body

    def test_save_a_schedule(self, client, admin_user, db_session, mail_on):
        reply = client.post("/admin/reports/guardrail", headers=_admin(admin_user), data={
            "enabled": "on", "weekday": "2", "send_time": "18:30", "recipients": "alice, bob@example.test",
        })
        assert reply.status_code == 200, reply.text
        data = reply.json()
        assert data["enabled"] is True and data["recipients"] == ["alice@example.test", "bob@example.test"]
        assert data["next"]
        row = db_session.get(ReportSchedule, "guardrail")
        assert (row.enabled, row.weekday, row.send_time, row.updated_by) == (True, 2, "18:30", admin_user.username)
        assert mail_on == []          # saving never sends
        assert db_session.get(ReportSchedule, "usage") is None

    def test_a_refused_recipient_saves_nothing(self, client, admin_user, db_session, mail_on):
        for form in (
            {"enabled": "on", "weekday": "0", "send_time": "09:00", "recipients": "alice, eve@elsewhere.test"},
            {"enabled": "on", "weekday": "0", "send_time": "09:00", "recipients": ""},
            {"enabled": "on", "weekday": "9", "send_time": "09:00", "recipients": "alice"},
            {"enabled": "on", "weekday": "0", "send_time": "25:00", "recipients": "alice"},
        ):
            assert client.post("/admin/reports/usage", headers=_admin(admin_user), data=form).status_code == 400, form
        assert client.post("/admin/reports/nope", headers=_admin(admin_user), data={}).status_code == 404
        assert db_session.exec(select(ReportSchedule)).all() == []

    def test_only_admins(self, client, test_user):
        headers = web_auth_header(sub=test_user.username)
        assert client.post("/admin/reports/usage", headers=headers, data={"enabled": "off"}).status_code == 403
        assert client.get("/admin/reports/usage/preview", headers=headers).status_code == 403
        assert client.post("/admin/reports/usage/send", headers=headers, data={"to": "me"}).status_code == 403

    def test_preview_shows_the_mail_and_sends_nothing(self, client, admin_user, db_session, test_user, mail_on, langfuse_off):
        today = report_content.last_week().start
        _log(db_session, test_user.id, today, "vllm", "test-llm", 1000, 234)
        db_session.commit()
        reply = client.get("/admin/reports/usage/preview", headers=web_auth_header(sub=admin_user.username, scopes=["admin"]))
        assert reply.status_code == 200
        assert "Weekly usage report" in reply.text and "1,234" in reply.text
        assert 'src="data:image/png;base64,' in reply.text and "cid:" not in reply.text
        assert "Langfuse is not configured" in reply.text
        assert mail_on == []
        assert client.get("/admin/reports/nope/preview", headers=_admin(admin_user)).status_code == 404

    def test_send_to_me_is_a_test_mail(self, client, admin_user, db_session, mail_on, stub_reports):
        reply = client.post("/admin/reports/usage/send", headers=_admin(admin_user), data={"to": "me"})
        assert reply.json() == {"ok": True, "to": [f"{admin_user.username}@example.test"], "detail": ""}
        to, subject, _body, _images = mail_on[0]
        assert to == [f"{admin_user.username}@example.test"] and subject == "[TEST] usage report"
        log = db_session.exec(select(ClientMailLog)).one()
        assert (log.kind, log.actor, log.rule_ids) == ("test", admin_user.username, "report:usage")
        assert db_session.get(ReportSchedule, "usage") is None       # the schedule is not touched

    def test_send_now_goes_to_the_saved_recipients(self, client, admin_user, db_session, mail_on, stub_reports):
        refused = client.post("/admin/reports/usage/send", headers=_admin(admin_user), data={"to": "recipients"})
        assert refused.status_code == 400 and mail_on == []
        _schedule(db_session, now=MONDAY_9 + timedelta(hours=5))
        reply = client.post("/admin/reports/usage/send", headers=_admin(admin_user), data={"to": "recipients"})
        assert reply.json()["to"] == ["alice@example.test", "bob@example.test"]
        assert mail_on[0][0] == ["alice@example.test", "bob@example.test"] and mail_on[0][1] == "usage report"
        assert db_session.get(ReportSchedule, "usage").last_status == "skipped"     # still next week's turn

    def test_send_needs_mail(self, client, admin_user, stub_reports):
        reply = client.post("/admin/reports/usage/send", headers=_admin(admin_user), data={"to": "me"})
        assert reply.status_code == 400 and "Mail is not configured" in reply.json()["detail"]
