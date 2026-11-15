"""The admin's account drawer: GET /admin/users/{id}/detail."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.models.schema import AppOwner, AuditLog, ClientViolation, UsageLog, User
from tests.conftest import earlier_today, web_auth_header


def _hdr(admin: User) -> dict:
    return web_auth_header(sub=admin.username, scopes=["read", "admin"])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _log(session, user: User, usd: float, *, model="test-llm", backend="vllm", when=None) -> None:
    session.add(UsageLog(
        user_id=user.id, model=model, model_type="llm", backend=backend, input_tokens=1000,
        output_tokens=100, cost_usd=Decimal(str(usd)), endpoint="/v1/messages",
        created_at=when or earlier_today(),
    ))
    session.commit()


def _detail(client, admin: User, user: User) -> str:
    resp = client.get(f"/admin/users/{user.id}/detail", headers=_hdr(admin))
    assert resp.status_code == 200, resp.text
    return resp.text


class TestAccess:
    def test_admins_only(self, client, test_user):
        resp = client.get(f"/admin/users/{test_user.id}/detail",
                          headers=web_auth_header(sub=test_user.username, scopes=["read"]))
        assert resp.status_code == 403

    def test_unknown_account(self, client, admin_user):
        assert client.get("/admin/users/99999/detail", headers=_hdr(admin_user)).status_code == 404

    def test_never_shows_the_key(self, client, admin_user, test_user):
        body = _detail(client, admin_user, test_user)
        assert test_user.api_key not in body
        assert 'onclick="showKeyModal(this)"' in body  # the logged key window instead


class TestContent:
    def test_today_against_the_limit(self, client, db_session, admin_user, test_user):
        test_user.daily_limit_usd = 2.0
        db_session.add(test_user)
        db_session.commit()
        _log(db_session, test_user, 1.5)
        body = _detail(client, admin_user, test_user)
        assert "$1.50" in body and "of $2 daily limit" in body
        assert "75% used" in body

    def test_thirty_days_by_model_and_older_usage_left_out(self, client, db_session, admin_user, test_user):
        _log(db_session, test_user, 3.0, model="azure-gpt-4", backend="azure", when=_now() - timedelta(days=3))
        _log(db_session, test_user, 1.0)
        _log(db_session, test_user, 50.0, model="old-model", when=_now() - timedelta(days=45))
        body = _detail(client, admin_user, test_user)
        assert "azure-gpt-4" in body and "test-llm" in body
        assert "old-model" not in body
        assert body.index("azure-gpt-4") < body.index(">test-llm<")  # biggest spend first
        assert "$4.00" in body  # the 30-day total

    def test_same_numbers_once_rolled_up(self, client, db_session, admin_user, test_user):
        from app.services import usage_rollup

        _log(db_session, test_user, 2.0, when=_now() - timedelta(days=4))
        before = _detail(client, admin_user, test_user)
        usage_rollup.advance(db_session, now=_now())
        assert usage_rollup.rolled_through(db_session) is not None
        assert _detail(client, admin_user, test_user) == before

    def test_open_and_fixed_notices(self, client, db_session, admin_user, test_user):
        now = _now()
        db_session.add(ClientViolation(rule_id="CC001", username=test_user.username,
                                       first_seen_at=now - timedelta(days=1), last_seen_at=now - timedelta(hours=2)))
        db_session.add(ClientViolation(rule_id="CC002", username=test_user.username,
                                       first_seen_at=now - timedelta(days=2), last_seen_at=now - timedelta(days=1),
                                       resolved_at=now - timedelta(hours=1)))
        db_session.add(ClientViolation(rule_id="CC003", username=test_user.username,
                                       first_seen_at=now - timedelta(days=40), last_seen_at=now - timedelta(days=30)))
        db_session.commit()
        body = _detail(client, admin_user, test_user)
        assert "CC001" in body and "CC002" in body
        assert "CC003" not in body  # outside the 15-day window
        assert body.count(">open<") == 1 and body.count(">fixed<") == 1

    def test_changes_to_it_and_its_own_actions_not_others(self, client, db_session, admin_user, test_user):
        other = User(username="someone-else")
        db_session.add(other)
        db_session.commit()
        db_session.refresh(other)
        db_session.add(AuditLog(actor=admin_user.username, method="POST", route="/admin/users/{user_id}/limit",
                                path=f"/admin/users/{test_user.id}/limit", status=200))
        db_session.add(AuditLog(actor=test_user.username, method="POST", route="/dashboard/refresh-key",
                                path="/dashboard/refresh-key", status=303))
        db_session.add(AuditLog(actor=admin_user.username, method="POST", route="/admin/users/{user_id}/toggle-azure",
                                path=f"/admin/users/{other.id}/toggle-azure", status=200))
        # /admin/users/1 must not match /admin/users/1x/...
        db_session.add(AuditLog(actor=admin_user.username, method="POST", route="/admin/users/{user_id}/delete",
                                path=f"/admin/users/{test_user.id}0/delete", status=200))
        db_session.commit()
        body = _detail(client, admin_user, test_user)
        assert f"POST /admin/users/{test_user.id}/limit" in body and "/dashboard/refresh-key" in body
        assert "toggle-azure" not in body
        assert "delete" not in body

    def test_service_account_lists_its_owners(self, client, db_session, admin_user, test_user):
        app = User(username="billing-etl", is_service_account=True)
        db_session.add(app)
        db_session.commit()
        db_session.refresh(app)
        db_session.add(AppOwner(app_id=app.id, owner_id=test_user.id))
        db_session.commit()
        assert "Owners:" in _detail(client, admin_user, app) and test_user.username in _detail(client, admin_user, app)
        assert "Owns:" in _detail(client, admin_user, test_user)

    def test_usage_of_nothing(self, client, admin_user, test_user):
        assert "No requests in the last 30 days." in _detail(client, admin_user, test_user)


class TestWiring:
    def test_names_open_the_drawer(self, client, db_session, admin_user, test_user):
        _log(db_session, test_user, 1.0)
        page = client.get("/admin", headers=_hdr(admin_user)).text
        assert f'data-um-detail="{test_user.id}"' in page  # table row and leaderboard
        assert page.count(f'data-um-detail="{test_user.id}"') >= 2
        assert 'id="udDrawer"' in page
