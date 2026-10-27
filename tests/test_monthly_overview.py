"""The admin page's month view (``stats.get_monthly_overview``): one aggregate
over usage_logs feeding the totals, both leaderboards, the per-user columns
and the department table. Also pins the ``app_`` prefix filter, whose bare
``LIKE 'app_%'`` form treated ``_`` as a wildcard."""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from decimal import Decimal

from sqlmodel import select

from app.models.schema import User, UsageLog, app_account_clause
from app.services.stats import get_monthly_overview


def _user(session, username: str, org: str = "") -> User:
    u = User(username=username, org_code=org)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _spend(session, user: User, usd: str, *, inp: int = 100, out: int = 10) -> None:
    session.add(UsageLog(
        user_id=user.id, model="m", model_type="llm", input_tokens=inp,
        output_tokens=out, cost_usd=Decimal(usd), endpoint="/v1/messages",
        created_at=datetime.now(timezone.utc),  # always inside this month
    ))
    session.commit()


class TestAppAccountClause:
    def test_underscore_is_literal(self, db_session):
        for name in ("app_batch", "apple", "app1", "appx_y"):
            _user(db_session, name)
        apps = db_session.exec(select(User.username).where(app_account_clause())).all()
        assert apps == ["app_batch"]


class TestMonthlyOverview:
    def test_one_scan_feeds_every_section(self, db_session):
        alice = _user(db_session, "alice", "ENG")
        bob = _user(db_session, "bob", "ENG")
        apple = _user(db_session, "apple", "")       # a person, despite the prefix
        bot = _user(db_session, "app_bot", "ENG")
        _spend(db_session, alice, "3.0")
        _spend(db_session, alice, "1.0")
        _spend(db_session, bob, "2.0")
        _spend(db_session, apple, "0.5")
        _spend(db_session, bot, "9.0")

        ov = get_monthly_overview(db_session)

        assert ov["totals"] == {
            "cost": 15.5, "input_tokens": 500, "output_tokens": 50, "requests": 5,
        }
        assert [r["username"] for r in ov["user_leaderboard"]] == ["alice", "bob", "apple"]
        assert [r["username"] for r in ov["app_leaderboard"]] == ["app_bot"]
        assert ov["user_leaderboard"][0]["monthly_cost"] == 4.0
        assert ov["user_leaderboard"][0]["monthly_reqs"] == 2
        assert ov["per_user"][alice.id]["cost"] == 4.0

        depts = {d["department"]: d for d in ov["departments"]}
        assert set(depts) == {"ENG", "Unknown"}   # the app's spend is not a department's
        assert depts["ENG"]["user_count"] == 2
        assert depts["ENG"]["cost"] == 6.0
        assert depts["Unknown"]["user_count"] == 1
        assert [d["department"] for d in ov["departments"]] == ["ENG", "Unknown"]

    def test_leaderboard_is_capped(self, db_session):
        for i in range(12):
            _spend(db_session, _user(db_session, f"u{i}"), str(i + 1))
        ov = get_monthly_overview(db_session)
        assert len(ov["user_leaderboard"]) == 10
        assert ov["user_leaderboard"][0]["username"] == "u11"

    def test_empty_month(self, db_session):
        _user(db_session, "idle")
        ov = get_monthly_overview(db_session)
        assert ov["totals"]["requests"] == 0
        assert ov["user_leaderboard"] == [] and ov["departments"] == []


class TestReportPagesRunOffTheEventLoop:
    """Pages that aggregate usage_logs are plain ``def`` handlers, which
    FastAPI runs in its threadpool. As ``async def`` their synchronous DB
    work ran on the event loop and stalled every stream on the worker."""

    def test_handlers_are_sync(self):
        from app.routers import admin, web_ui

        for fn in (
            admin.admin_page, admin.export_usage_report,
            admin.export_user_backend_costs_csv, admin.export_users_csv,
            web_ui.dashboard, web_ui.model_breakdown_api,
        ):
            assert not inspect.iscoroutinefunction(fn), fn.__name__
