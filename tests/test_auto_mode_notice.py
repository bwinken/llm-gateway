"""Claude Code auto mode notice (app/services/auto_mode_notice.py).

A Messages request carrying ``safeguards`` means the client waits for a
server-side review no backend here gives. The gateway cannot change the
client's settings, so it stamps ``users.auto_mode_server_seen_at``: the
dashboard then shows that user the fix and the admin page lists the account.
The request itself is served exactly as before.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.models.schema import User
from app.services import auto_mode_notice
from tests.conftest import auth_header, make_httpx_response, web_auth_header

_DOWNSTREAM = {
    "id": "chatcmpl-1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _reload(db_session, user: User) -> User:
    db_session.expire_all()
    return db_session.get(User, user.id)


def _messages(client, **extra):
    async def _post(*args, **kwargs):
        return make_httpx_response(200, _DOWNSTREAM)

    body = {"model": "test-llm", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}], **extra}
    with patch.object(client.__httpx_mock__, "post", side_effect=_post) as post:
        resp = client.post("/v1/messages", json=body, headers=auth_header())
    return resp, post


def _stamp(db_session, user: User, when: datetime) -> None:
    user.auto_mode_server_seen_at = when
    db_session.add(user)
    db_session.commit()


class TestDetection:
    def test_a_request_asking_for_server_review_is_stamped_and_still_served(self, client, db_session, test_user):
        resp, post = _messages(client, safeguards={"anything": True})
        assert resp.status_code == 200
        assert resp.json()["content"][0]["text"] == "ok"
        seen = _reload(db_session, test_user).auto_mode_server_seen_at
        assert seen is not None and abs((_now() - seen).total_seconds()) < 60
        # The field is still not forwarded downstream.
        assert "safeguards" not in post.call_args.kwargs["json"]

    def test_an_ordinary_request_stamps_nothing(self, client, db_session, test_user):
        resp, _ = _messages(client)
        assert resp.status_code == 200
        assert _reload(db_session, test_user).auto_mode_server_seen_at is None

    def test_at_most_one_write_per_window(self, client, test_user):
        with patch.object(auto_mode_notice, "_record", wraps=auto_mode_notice._record) as rec:
            for _ in range(4):
                _messages(client, safeguards={})
        assert rec.call_count == 1

    def test_the_update_is_conditional(self, db_session, test_user):
        recent = _now() - timedelta(minutes=5)
        _stamp(db_session, test_user, recent)
        auto_mode_notice._record(test_user.id)   # another worker within the window
        assert _reload(db_session, test_user).auto_mode_server_seen_at == recent

    def test_a_failed_write_never_fails_the_request(self, client, test_user):
        with patch.object(auto_mode_notice, "_record", side_effect=RuntimeError("db down")):
            resp, _ = _messages(client, safeguards={})
        assert resp.status_code == 200

    def test_cloud_messages_routes_are_watched_too(self):
        import inspect

        from app.routers import aws_api, azure_api

        assert "auto_mode_notice.observe_request" in inspect.getsource(azure_api.azure_messages)
        assert "auto_mode_notice.observe_request" in inspect.getsource(aws_api.bedrock_messages)


class TestWindow:
    def test_recent_is_active_old_is_not(self):
        now = _now()
        assert auto_mode_notice.is_active(now - timedelta(days=1), now)
        assert not auto_mode_notice.is_active(now - timedelta(days=auto_mode_notice.WINDOW_DAYS + 1), now)
        assert not auto_mode_notice.is_active(None, now)


class TestDashboard:
    def _page(self, client, user: User) -> str:
        return client.get("/dashboard", headers=web_auth_header(sub=user.username)).text

    def test_affected_user_sees_the_fix(self, client, db_session, test_user):
        _stamp(db_session, test_user, _now())
        body = self._page(client, test_user)
        assert 'id="autoModeNotice"' in body
        assert "CLAUDE_CODE_AUTO_MODE_SERVER" in body and "CLAUDE_CODE_AUTO_MODE_MODEL" in body

    def test_unaffected_user_sees_nothing(self, client, test_user):
        assert 'id="autoModeNotice"' not in self._page(client, test_user)

    def test_notice_clears_after_the_window(self, client, db_session, test_user):
        _stamp(db_session, test_user, _now() - timedelta(days=auto_mode_notice.WINDOW_DAYS + 1))
        assert 'id="autoModeNotice"' not in self._page(client, test_user)


class TestAdmin:
    def _page(self, client, admin: User) -> str:
        return client.get("/admin", headers=web_auth_header(sub=admin.username, scopes=["admin"])).text

    def test_lists_affected_accounts_only(self, client, db_session, admin_user, test_user):
        assert 'id="autoModeWaiting"' not in self._page(client, admin_user)
        _stamp(db_session, test_user, _now())
        body = self._page(client, admin_user)
        assert 'id="autoModeWaiting"' in body
        section = body.split('id="autoModeWaiting"', 1)[1][:3000]
        assert "?q=testuser#users" in section

    def test_old_stamps_are_not_listed(self, db_session, test_user):
        _stamp(db_session, test_user, _now() - timedelta(days=auto_mode_notice.WINDOW_DAYS + 1))
        assert auto_mode_notice.affected(db_session) == []


class TestInstaller:
    def test_example_installer_writes_both_settings(self):
        text = (Path(__file__).resolve().parent.parent / "setup" / "install-claude-code-example.bat").read_text(encoding="utf-8")
        assert "$e['CLAUDE_CODE_AUTO_MODE_SERVER']='0'" in text
        assert "$e['CLAUDE_CODE_AUTO_MODE_MODEL']='%AUTO_MODE_MODEL%'" in text
        assert 'set "AUTO_MODE_CLIENT_CHECK=1"' in text
