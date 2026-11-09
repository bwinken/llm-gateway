"""users.api_key_created_at / api_key_last_used_at, for access reviews.

A successful API-key auth stamps ``api_key_last_used_at`` at most once per
KEY_USE_RESOLUTION_S per worker (and the UPDATE is conditional), after the
auth session closed, even when the budget check then refuses the request.
Reissuing a key resets both columns. Admins see "used N d ago" per account
and can filter keys unused for 90+ days.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.core import deps
from app.models.schema import User
from tests.conftest import auth_header, web_auth_header


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _reload(db_session, user: User) -> User:
    db_session.expire_all()
    return db_session.get(User, user.id)


class TestStamp:
    def test_successful_auth_stamps_last_use(self, client, db_session, test_user):
        assert test_user.api_key_last_used_at is None
        assert client.get("/v1/me", headers=auth_header()).status_code == 200
        used = _reload(db_session, test_user).api_key_last_used_at
        assert used is not None and abs((_now() - used).total_seconds()) < 60

    def test_at_most_one_write_per_window(self, client, test_user):
        with patch.object(deps, "_record_key_use", wraps=deps._record_key_use) as rec:
            for _ in range(5):
                client.get("/v1/me", headers=auth_header())
        assert rec.call_count == 1

    def test_the_update_is_conditional(self, db_session, test_user):
        recent = _now() - timedelta(minutes=5)
        test_user.api_key_last_used_at = recent
        db_session.add(test_user)
        db_session.commit()
        deps._record_key_use(test_user.id)   # another worker within the window
        assert _reload(db_session, test_user).api_key_last_used_at == recent

    def test_stamped_even_when_the_budget_refuses(self, client, db_session, test_user):
        test_user.daily_limit_usd = 0.0
        db_session.add(test_user)
        db_session.commit()
        resp = client.post("/v1/chat/completions", json={"model": "test-llm", "messages": []},
                           headers=auth_header())
        assert resp.status_code == 429
        assert _reload(db_session, test_user).api_key_last_used_at is not None

    def test_invalid_key_stamps_nothing(self, client, db_session, test_user):
        client.get("/v1/me", headers=auth_header("sk-wrong"))
        assert _reload(db_session, test_user).api_key_last_used_at is None

    def test_a_failing_write_never_fails_the_request(self, client):
        with patch.object(deps, "update", side_effect=RuntimeError("db down")):
            assert client.get("/v1/me", headers=auth_header()).status_code == 200


class TestReissue:
    def test_regenerating_resets_issue_and_use(self, client, db_session, test_user):
        test_user.api_key_last_used_at = _now() - timedelta(days=3)
        test_user.api_key_created_at = _now() - timedelta(days=400)
        db_session.add(test_user)
        db_session.commit()
        client.post("/dashboard/refresh-key", headers=web_auth_header(sub=test_user.username))
        u = _reload(db_session, test_user)
        assert u.api_key_last_used_at is None
        assert abs((_now() - u.api_key_created_at).total_seconds()) < 60

    def test_new_accounts_record_when_their_key_was_issued(self, db_session):
        u = User(username="fresh")
        db_session.add(u)
        db_session.commit()
        assert u.api_key_created_at is not None


class TestAdminView:
    def _table(self, client, admin, **params) -> str:
        resp = client.get("/admin/user-table", params={"kind": "users", **params},
                          headers=web_auth_header(sub=admin.username, scopes=["admin"]))
        assert resp.status_code == 200, resp.text
        return resp.text

    def _add(self, db_session, name, used_days_ago=None, issued_days_ago=None):
        u = User(username=name)
        db_session.add(u)
        db_session.commit()
        # Assigned after insert: on insert the column default fills a None.
        # A NULL issue date is what the migration leaves for keys of unknown age.
        u.api_key_last_used_at = None if used_days_ago is None else _now() - timedelta(days=used_days_ago)
        u.api_key_created_at = None if issued_days_ago is None else _now() - timedelta(days=issued_days_ago)
        db_session.add(u)
        db_session.commit()

    def test_stale_filter(self, client, db_session, admin_user, test_user):
        self._add(db_session, "idle", used_days_ago=120, issued_days_ago=500)
        self._add(db_session, "active", used_days_ago=2, issued_days_ago=500)
        self._add(db_session, "never_old", issued_days_ago=200)
        self._add(db_session, "never_new", issued_days_ago=3)       # just issued: not stale
        self._add(db_session, "unknown")                            # issue date unknown, never used
        body = self._table(client, admin_user, status="stale_key")
        for name in ("idle", "never_old", "unknown"):
            assert f'data-username="{name}"' in body, name
        for name in ("active", "never_new"):
            assert f'data-username="{name}"' not in body, name

    def test_rows_say_when_the_key_was_used(self, client, db_session, admin_user, test_user):
        self._add(db_session, "idle", used_days_ago=120, issued_days_ago=500)
        body = self._table(client, admin_user)
        assert "used 120 d ago" in body
        assert "never used" in body          # testuser
        assert "old format" in body          # testuser's hand-set key
