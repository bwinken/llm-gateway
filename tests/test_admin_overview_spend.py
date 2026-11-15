"""Admin Overview: estimated spend per day by backend, and the tile that
shows today's cloud spend while in-flight requests are not counted."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from app.core.timeutil import local_today
from app.models.schema import UsageLog
from app.services import stats
from tests.conftest import earlier_today, web_auth_header


def _log(session, user, usd, backend, when):
    session.add(UsageLog(user_id=user.id, model="m", model_type="llm", backend=backend,
                         input_tokens=10, output_tokens=10, cost_usd=Decimal(str(usd)),
                         endpoint="/v1/messages", created_at=when))
    session.commit()


class TestSpendTrends:
    def test_every_day_present_and_split_by_backend(self, db_session, test_user):
        _log(db_session, test_user, 1.0, "vllm", earlier_today())
        _log(db_session, test_user, 2.0, "azure", earlier_today())
        _log(db_session, test_user, 4.0, "bedrock", earlier_today() - timedelta(days=2))
        _log(db_session, test_user, 8.0, "vllm", earlier_today() - timedelta(days=40))
        rows = stats.get_spend_trends(db_session)
        assert len(rows) == 30
        assert rows[-1] == {"date": local_today().isoformat(), "vllm": 1.0, "azure": 2.0, "bedrock": 0.0, "total": 3.0}
        assert rows[-3]["bedrock"] == 4.0 and rows[-3]["total"] == 4.0
        assert rows[-2]["total"] == 0.0  # a quiet day is a zero bar, not a gap
        assert sum(r["total"] for r in rows) == 7.0  # 40 days ago is outside

    def test_an_unknown_backend_counts_in_the_total_only(self, db_session, test_user):
        _log(db_session, test_user, 1.5, "other", earlier_today())
        today = stats.get_spend_trends(db_session)[-1]
        assert today["total"] == 1.5 and today["vllm"] == today["azure"] == today["bedrock"] == 0.0


class TestOverviewTiles:
    def _page(self, client, admin_user):
        return client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["read", "admin"])).text

    def test_cloud_spend_while_concurrency_is_off(self, client, db_session, admin_user, test_user):
        _log(db_session, test_user, 2.0, "azure", earlier_today())
        _log(db_session, test_user, 0.5, "bedrock", earlier_today())
        page = self._page(client, admin_user)
        assert 'data-stat="cloud-today"' in page and "$2.50" in page
        assert "tracking off" not in page
        assert 'id="spendChart"' in page

    def test_in_flight_while_the_limit_runs(self, client, admin_user):
        with (
            patch("app.routers.admin.page.get_concurrency_settings", return_value=("monitor", 8)),
            patch("app.services.concurrency.in_flight_summary",
                  return_value={"total": 3, "users": 2, "peak_user": 2, "peak_cloud_user": 1}),
        ):
            page = self._page(client, admin_user)
        assert 'data-stat="cloud-today"' not in page  # the in-flight tile takes its place
        assert "In flight now" in page and "busiest account 2 / 8 local" in page
