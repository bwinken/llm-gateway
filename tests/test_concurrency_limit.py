"""Per-user concurrency limit (app/services/concurrency.py + deps._concurrency_slot).

What is pinned here:
  - which endpoints are limited (chat / responses / messages on all three
    surfaces) and which are not (count_tokens etc.);
  - the three modes, the admin / waived exemptions, fail-open on DB trouble;
  - the lease is held for the WHOLE response — mid-stream it is still there,
    after the last byte it is gone. That relies on FastAPI running a yield
    dependency's exit after the response is sent; if a FastAPI upgrade ever
    changes that, the limit silently stops limiting and this file says so;
  - a client disconnect still releases (shielded release);
  - the sweep that clears leases of dead workers after a restart;
  - the admin settings / waive endpoints.

A PostgreSQL-only race test (two workers admitting the same user at once)
runs when ``CONCURRENCY_TEST_PG_URL`` is set; SQLite ignores FOR UPDATE.
"""

from __future__ import annotations

import os
import threading
from datetime import timedelta
from unittest.mock import patch

import anyio
import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.core import deps
from app.models.schema import ConcurrencyLease, User
from app.services import concurrency
from tests.conftest import (
    FakeStreamResponse,
    _test_engine,
    auth_header,
    make_httpx_response,
    web_auth_header,
)

_CHAT = {"model": "test-llm", "messages": [{"role": "user", "content": "Hi"}]}
_MESSAGES = {
    "model": "test-llm",
    "max_tokens": 16,
    "messages": [{"role": "user", "content": "Hi"}],
}
_OK_CHAT = {
    "id": "c1",
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
_SSE = [
    'data: {"id":"s1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"Hi"},"finish_reason":null}]}',
    'data: {"id":"s1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}',
    "data: [DONE]",
]


def _mode(mode: str, limit: int = 1):
    return patch("app.core.deps.get_concurrency_settings", return_value=(mode, limit))


def _leases(user_id: int | None = None) -> list[ConcurrencyLease]:
    with Session(_test_engine) as s:
        stmt = select(ConcurrencyLease)
        if user_id is not None:
            stmt = stmt.where(ConcurrencyLease.user_id == user_id)
        return list(s.exec(stmt).all())


def _add_lease(user_id: int, *, worker: str | None = None, expires_in: float = 120.0) -> str:
    now = concurrency._utcnow()
    lease_id = f"pre-{len(_leases())}-{user_id}"
    with Session(_test_engine) as s:
        s.add(ConcurrencyLease(
            id=lease_id,
            user_id=user_id,
            worker_id=worker or concurrency.worker_id(),
            endpoint="/v1/messages",
            created_at=now,
            expires_at=now + timedelta(seconds=expires_in),
        ))
        s.commit()
    return lease_id


# ── Endpoint behaviour ─────────────────────────────────────────────────────


class TestModes:
    def test_off_takes_no_lease(self, client, test_user):
        client.__httpx_mock__.post.return_value = make_httpx_response(200, _OK_CHAT)
        _add_lease(test_user.id)  # would block in enforce mode
        with _mode("off"):
            resp = client.post("/v1/chat/completions", json=_CHAT, headers=auth_header())
        assert resp.status_code == 200
        assert len(_leases(test_user.id)) == 1  # only the pre-existing one

    def test_enforce_rejects_over_limit_without_calling_downstream(self, client, test_user):
        _add_lease(test_user.id)
        with _mode("enforce", 1), patch.object(client.__httpx_mock__, "post") as post:
            resp = client.post("/v1/chat/completions", json=_CHAT, headers=auth_header())
        assert resp.status_code == 429
        assert resp.headers["retry-after"] == "5"
        assert "1 already in flight (limit 1)" in resp.json()["detail"]
        post.assert_not_called()
        assert len(_leases(test_user.id)) == 1

    def test_enforce_under_limit_serves_and_releases(self, client, test_user):
        client.__httpx_mock__.post.return_value = make_httpx_response(200, _OK_CHAT)
        _add_lease(test_user.id)
        with _mode("enforce", 2):
            resp = client.post("/v1/chat/completions", json=_CHAT, headers=auth_header())
        assert resp.status_code == 200
        assert len(_leases(test_user.id)) == 1  # ours was released

    def test_monitor_serves_over_limit_and_logs(self, client, test_user):
        client.__httpx_mock__.post.return_value = make_httpx_response(200, _OK_CHAT)
        _add_lease(test_user.id)
        with _mode("monitor", 1), patch("app.core.deps.logger") as log:
            resp = client.post("/v1/chat/completions", json=_CHAT, headers=auth_header())
        assert resp.status_code == 200
        msg, *args = log.warning.call_args.args
        assert msg.startswith("Concurrency limit {}")
        assert args[0] == "exceeded (monitor)"
        assert args[2] == 2  # real in-flight count, not capped at the limit
        assert len(_leases(test_user.id)) == 1

    def test_expired_lease_is_not_counted(self, client, test_user):
        client.__httpx_mock__.post.return_value = make_httpx_response(200, _OK_CHAT)
        _add_lease(test_user.id, expires_in=-1)
        with _mode("enforce", 1):
            resp = client.post("/v1/chat/completions", json=_CHAT, headers=auth_header())
        assert resp.status_code == 200

    def test_downstream_error_still_releases(self, client, test_user):
        # patch.object, not `.side_effect =`: the shared mock's reset_mock()
        # keeps side_effect, so a bare assignment would leak into later tests.
        with (
            _mode("enforce", 1),
            patch.object(client.__httpx_mock__, "post", side_effect=httpx.ConnectError("boom")),
        ):
            resp = client.post("/v1/chat/completions", json=_CHAT, headers=auth_header())
        assert resp.status_code >= 500
        assert _leases(test_user.id) == []

    def test_other_users_leases_do_not_count(self, client, test_user, admin_user):
        client.__httpx_mock__.post.return_value = make_httpx_response(200, _OK_CHAT)
        _add_lease(admin_user.id)
        with _mode("enforce", 1):
            resp = client.post("/v1/chat/completions", json=_CHAT, headers=auth_header())
        assert resp.status_code == 200


class TestExemptions:
    def test_waived_user_is_not_limited(self, client, db_session, test_user):
        test_user.concurrency_waived = True
        db_session.add(test_user)
        db_session.commit()
        client.__httpx_mock__.post.return_value = make_httpx_response(200, _OK_CHAT)
        _add_lease(test_user.id)
        with _mode("enforce", 1):
            resp = client.post("/v1/chat/completions", json=_CHAT, headers=auth_header())
        assert resp.status_code == 200
        assert len(_leases(test_user.id)) == 1  # no lease taken for a waived user

    def test_admin_is_not_limited(self, client, admin_user):
        client.__httpx_mock__.post.return_value = make_httpx_response(200, _OK_CHAT)
        _add_lease(admin_user.id)
        with _mode("enforce", 1):
            resp = client.post(
                "/v1/chat/completions", json=_CHAT, headers=auth_header("sk-adminkey456")
            )
        assert resp.status_code == 200

    def test_store_failure_fails_open(self, client, test_user):
        client.__httpx_mock__.post.return_value = make_httpx_response(200, _OK_CHAT)
        with (
            _mode("enforce", 1),
            patch.object(concurrency, "acquire", side_effect=RuntimeError("db down")),
            patch("app.core.deps.logger") as log,
        ):
            resp = client.post("/v1/chat/completions", json=_CHAT, headers=auth_header())
        assert resp.status_code == 200
        assert "allowing request" in log.warning.call_args.args[0]


class TestCoverage:
    """Which routes count. Every limited route 429s with a lease at the limit."""

    @pytest.mark.parametrize("path, body", [
        ("/v1/chat/completions", _CHAT),
        ("/chat/completions", _CHAT),
        ("/v1/responses", {"model": "test-llm", "input": "Hi"}),
        ("/v1/messages", _MESSAGES),
        ("/messages", _MESSAGES),
        ("/azure/v1/chat/completions", {**_CHAT, "model": "azure-gpt-4"}),
        ("/azure/v1/responses", {"model": "azure-gpt-4", "input": "Hi"}),
        ("/azure/v1/messages", {**_MESSAGES, "model": "azure-gpt-4"}),
        ("/aws/v1/chat/completions", {**_CHAT, "model": "bedrock-claude"}),
        ("/aws/v1/messages", {**_MESSAGES, "model": "bedrock-claude"}),
    ])
    def test_limited(self, client, test_user, path, body):
        _add_lease(test_user.id)
        with _mode("enforce", 1):
            resp = client.post(path, json=body, headers=auth_header())
        assert resp.status_code == 429, resp.text

    @pytest.mark.parametrize("path, body", [
        ("/v1/messages/count_tokens", _MESSAGES),
        ("/azure/v1/messages/count_tokens", {**_MESSAGES, "model": "azure-gpt-4"}),
        ("/aws/v1/messages/count_tokens", {**_MESSAGES, "model": "bedrock-claude"}),
        ("/v1/tokenize", {"model": "test-llm", "prompt": "Hi"}),
        ("/v1/embeddings", {"model": "test-embedding", "input": "Hi"}),
    ])
    def test_not_limited(self, client, test_user, path, body):
        client.__httpx_mock__.post.return_value = make_httpx_response(200, {"tokens": [1], "count": 1, "data": []})
        _add_lease(test_user.id)
        with _mode("enforce", 1):
            resp = client.post(path, json=body, headers=auth_header())
        assert resp.status_code != 429, resp.text

    def test_models_listing_not_limited(self, client, test_user):
        _add_lease(test_user.id)
        with _mode("enforce", 1):
            assert client.get("/v1/models", headers=auth_header()).status_code == 200


class TestLeaseSpansTheResponse:
    """The lease must be held until the last byte — not just until the
    handler returns the StreamingResponse."""

    def test_stream_holds_lease_until_done(self, client, test_user):
        seen: list[int] = []

        class ProbingStream(FakeStreamResponse):
            async def aiter_lines(self):
                async for line in super().aiter_lines():
                    seen.append(len(_leases(test_user.id)))
                    yield line

        mock = client.__httpx_mock__
        mock.build_request.return_value = httpx.Request("POST", "http://mock-llm:8000/v1/chat/completions")
        mock.send.return_value = ProbingStream(_SSE)
        with _mode("enforce", 1):
            resp = client.post(
                "/v1/chat/completions", json={**_CHAT, "stream": True}, headers=auth_header()
            )
        assert resp.status_code == 200
        assert seen and seen == [1] * len(seen), f"lease not held mid-stream: {seen}"
        assert _leases(test_user.id) == []

    def test_anthropic_stream_holds_lease_until_done(self, client, test_user):
        seen: list[int] = []

        class ProbingStream(FakeStreamResponse):
            async def aiter_lines(self):
                async for line in super().aiter_lines():
                    seen.append(len(_leases(test_user.id)))
                    yield line

        mock = client.__httpx_mock__
        mock.build_request.return_value = httpx.Request("POST", "http://mock-llm:8000/v1/chat/completions")
        mock.send.return_value = ProbingStream(_SSE)
        with _mode("enforce", 1):
            resp = client.post(
                "/v1/messages", json={**_MESSAGES, "stream": True}, headers=auth_header()
            )
        assert resp.status_code == 200
        assert "message_stop" in resp.text
        assert seen and seen == [1] * len(seen), f"lease not held mid-stream: {seen}"
        assert _leases(test_user.id) == []

    def test_second_request_blocked_while_first_streams(self, client, test_user):
        """While one stream is in flight, a second request (limit 1) is refused."""
        statuses: list[int] = []

        class ProbingStream(FakeStreamResponse):
            async def aiter_lines(self):
                first = True
                async for line in super().aiter_lines():
                    if first:
                        first = False
                        # Same user, same moment, straight through the store.
                        lease, active = concurrency.acquire(
                            test_user.id, 1, "/v1/messages", enforce=True,
                        )
                        statuses.append(active)
                        assert lease is None
                    yield line

        mock = client.__httpx_mock__
        mock.build_request.return_value = httpx.Request("POST", "http://mock-llm:8000/v1/chat/completions")
        mock.send.return_value = ProbingStream(_SSE)
        with _mode("enforce", 1):
            resp = client.post(
                "/v1/chat/completions", json={**_CHAT, "stream": True}, headers=auth_header()
            )
        assert resp.status_code == 200
        assert statuses == [1]


class TestClientDisconnect:
    def test_cancelled_request_still_releases(self, db_session, test_user):
        """Starlette cancels the response task group when the client goes
        away and anyio re-delivers the cancellation at every await. The
        release must be shielded or its DELETE never runs."""
        entered = anyio.Event()

        async def main():
            async with anyio.create_task_group() as tg:
                async def request():
                    async with deps._concurrency_slot(test_user, "/v1/messages"):
                        entered.set()
                        await anyio.sleep_forever()

                tg.start_soon(request)
                await entered.wait()
                assert len(_leases(test_user.id)) == 1
                tg.cancel_scope.cancel()

        with _mode("enforce", 1):
            anyio.run(main)
        assert _leases(test_user.id) == []


# ── Lease store ───────────────────────────────────────────────────────────


class TestStore:
    def test_acquire_enforce_and_monitor(self, db_session, test_user):
        lease, active = concurrency.acquire(test_user.id, 1, "/x", enforce=True)
        assert lease and active == 0
        refused, active = concurrency.acquire(test_user.id, 1, "/x", enforce=True)
        assert refused is None and active == 1
        over, active = concurrency.acquire(test_user.id, 1, "/x", enforce=False)
        assert over and active == 1
        assert len(_leases(test_user.id)) == 2
        concurrency.release(lease)
        concurrency.release(over)
        concurrency.release("already-gone")  # no-op
        assert _leases(test_user.id) == []

    def test_heartbeat_renews_own_leases(self, db_session, test_user):
        lease_id = _add_lease(test_user.id, expires_in=5)
        renewed, removed = concurrency.renew_and_sweep()
        assert (renewed, removed) == (1, 0)
        (lease,) = _leases(test_user.id)
        assert lease.id == lease_id
        assert lease.expires_at > concurrency._utcnow() + timedelta(seconds=150)

    def test_sweep_removes_expired(self, db_session, test_user):
        _add_lease(test_user.id, worker="otherhost:1:abc", expires_in=-1)
        _add_lease(test_user.id, worker="otherhost:1:abc", expires_in=60)
        _, removed = concurrency.renew_and_sweep()
        assert removed == 1
        assert len(_leases(test_user.id)) == 1  # another host: TTL decides

    def test_sweep_removes_dead_local_worker(self, db_session, test_user):
        """The restart case: a SIGKILLed worker's leases are cleared on the
        next sweep instead of blocking its users until the TTL."""
        host = concurrency.worker_id().rsplit(":", 2)[0]
        dead = f"{host}:999999:deadbeef"
        alive = f"{host}:{os.getppid()}:cafe"  # our parent certainly exists
        _add_lease(test_user.id, worker=dead)
        _add_lease(test_user.id, worker=alive)
        with patch.object(concurrency, "_pid_alive", side_effect=lambda pid: pid != 999999):
            concurrency.renew_and_sweep()
        assert [lease.worker_id for lease in _leases(test_user.id)] == [alive]

    def test_sweep_removes_recycled_pid(self, db_session, test_user):
        """A dead worker whose pid the kernel gave to *this* worker: same pid,
        different boot token."""
        host = concurrency.worker_id().rsplit(":", 2)[0]
        _add_lease(test_user.id, worker=f"{host}:{os.getpid()}:oldtoken")
        mine = _add_lease(test_user.id)
        concurrency.renew_and_sweep()
        assert [lease.id for lease in _leases(test_user.id)] == [mine]

    def test_pid_alive(self):
        assert concurrency._pid_alive(os.getpid()) is True
        with patch("os.kill", side_effect=ProcessLookupError):
            assert concurrency._pid_alive(12345) is False
        with patch("os.kill", side_effect=PermissionError):
            assert concurrency._pid_alive(12345) is True  # exists, not ours

    def test_pid_alive_never_signals_on_windows(self):
        with patch.object(concurrency.os, "name", "nt"), patch("os.kill") as kill:
            assert concurrency._pid_alive(12345) is True
        kill.assert_not_called()

    def test_unparseable_owner_is_kept(self, db_session, test_user):
        _add_lease(test_user.id, worker="garbage")
        concurrency.renew_and_sweep()
        assert len(_leases(test_user.id)) == 1

    def test_worker_id_changes_with_pid(self):
        first = concurrency.worker_id()
        assert concurrency.worker_id() == first
        with patch("os.getpid", return_value=os.getpid() + 1):
            other = concurrency.worker_id()
        assert other != first
        assert other.rsplit(":", 2)[1] == str(os.getpid() + 1)

    def test_sweep_safely_swallows_errors(self):
        with patch.object(concurrency, "renew_and_sweep", side_effect=RuntimeError("no table")):
            concurrency.sweep_safely()  # must not raise

    def test_in_flight_summary(self, db_session, test_user, admin_user):
        _add_lease(test_user.id)
        _add_lease(test_user.id)
        _add_lease(admin_user.id)
        _add_lease(admin_user.id, expires_in=-1)
        assert concurrency.in_flight_summary() == {"total": 3, "users": 2, "peak_user": 2}


# ── Settings ──────────────────────────────────────────────────────────────


class TestSettings:
    @pytest.mark.parametrize("app_cfg, expected", [
        ({}, ("off", 8)),
        ({"concurrency_limit_mode": "enforce", "concurrency_limit": 4}, ("enforce", 4)),
        ({"concurrency_limit_mode": "MONITOR", "concurrency_limit": "6"}, ("monitor", 6)),
        ({"concurrency_limit_mode": "bogus", "concurrency_limit": 4}, ("off", 4)),
        ({"concurrency_limit_mode": "enforce", "concurrency_limit": 0}, ("enforce", 8)),
        ({"concurrency_limit_mode": "enforce", "concurrency_limit": "x"}, ("enforce", 8)),
        ({"concurrency_limit_mode": "enforce", "concurrency_limit": True}, ("enforce", 8)),
    ])
    def test_get(self, app_cfg, expected):
        from app.core import config
        with patch.object(config, "APP_CONFIG", app_cfg), patch.object(config, "_check_auto_reload"):
            assert config.get_concurrency_settings() == expected

    @pytest.mark.parametrize("mode, limit", [("bogus", 4), ("enforce", 0), ("enforce", True), ("off", "4")])
    def test_set_rejects_bad_values(self, mode, limit):
        from app.core import config
        with patch.object(config, "_save_app_keys") as save:
            with pytest.raises(ValueError):
                config.set_concurrency_settings(mode, limit)
        save.assert_not_called()

    def test_set_persists(self):
        from app.core import config
        with patch.object(config, "_save_app_keys") as save:
            config.set_concurrency_settings("monitor", 4)
        save.assert_called_once_with({"concurrency_limit_mode": "monitor", "concurrency_limit": 4})


# ── Admin ─────────────────────────────────────────────────────────────────


class TestAdmin:
    @staticmethod
    def _hdr(admin_user, json: bool = False):
        h = web_auth_header(sub=admin_user.username, scopes=["admin"])
        if json:
            h["Accept"] = "application/json"
        return h

    def test_set_limit(self, client, admin_user):
        with patch("app.routers.admin.set_concurrency_settings") as save:
            resp = client.post(
                "/admin/concurrency-limit", data={"mode": "enforce", "limit": "4"},
                headers=self._hdr(admin_user), follow_redirects=False,
            )
        assert resp.status_code == 303
        save.assert_called_once_with("enforce", 4)

    def test_set_limit_json(self, client, admin_user):
        with patch("app.routers.admin.set_concurrency_settings"):
            resp = client.post(
                "/admin/concurrency-limit", data={"mode": "monitor", "limit": "6"},
                headers=self._hdr(admin_user, json=True),
            )
        assert resp.json() == {"ok": True, "mode": "monitor", "limit": 6}

    @pytest.mark.parametrize("form", [
        {"mode": "enforce", "limit": "0"},
        {"mode": "enforce", "limit": "-1"},
        {"mode": "enforce", "limit": "2.5"},
        {"mode": "enforce", "limit": ""},
        {"mode": "enforce"},
        {"mode": "unlimited", "limit": "4"},
        {"limit": "4"},
    ])
    def test_set_limit_rejects(self, client, admin_user, form):
        with patch("app.routers.admin.set_concurrency_settings") as save:
            resp = client.post("/admin/concurrency-limit", data=form, headers=self._hdr(admin_user))
        assert resp.status_code == 400
        save.assert_not_called()

    def test_requires_admin(self, client, test_user):
        with patch("app.routers.admin.set_concurrency_settings") as save:
            resp = client.post(
                "/admin/concurrency-limit", data={"mode": "off", "limit": "4"},
                headers=web_auth_header(sub=test_user.username),
            )
        assert resp.status_code in (401, 403)
        save.assert_not_called()

    def test_toggle_waive(self, client, db_session, admin_user, test_user):
        url = f"/admin/users/{test_user.id}/toggle-concurrency-waive"
        resp = client.post(url, headers=self._hdr(admin_user, json=True))
        assert resp.json() == {"ok": True, "concurrency_waived": True}
        db_session.refresh(test_user)
        assert test_user.concurrency_waived is True
        resp = client.post(url, headers=self._hdr(admin_user), follow_redirects=False)
        assert resp.status_code == 303
        db_session.refresh(test_user)
        assert test_user.concurrency_waived is False

    def test_patch_user_waive(self, client, db_session, admin_user, test_user):
        url = f"/admin/users/{test_user.id}"
        resp = client.patch(url, json={"concurrency_waived": True}, headers=self._hdr(admin_user))
        assert resp.status_code == 200
        db_session.refresh(test_user)
        assert test_user.concurrency_waived is True
        listed = {u["id"]: u for u in client.get("/admin/users", headers=self._hdr(admin_user)).json()}
        assert listed[test_user.id]["concurrency_waived"] is True

    def test_patch_user_waive_rejects_string(self, client, db_session, admin_user, test_user):
        """bool("false") is True — a string must not silently waive anyone."""
        resp = client.patch(
            f"/admin/users/{test_user.id}", json={"concurrency_waived": "false"},
            headers=self._hdr(admin_user),
        )
        assert resp.status_code == 400
        db_session.refresh(test_user)
        assert test_user.concurrency_waived is False

    def test_delete_user_removes_leases(self, client, db_session, admin_user, test_user):
        _add_lease(test_user.id)
        resp = client.post(
            f"/admin/users/{test_user.id}/delete", headers=self._hdr(admin_user),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _leases() == []

    def test_admin_page_renders_card(self, client, admin_user, test_user):
        _add_lease(test_user.id)
        with patch("app.routers.admin.get_concurrency_settings", return_value=("monitor", 6)):
            resp = client.get("/admin", headers=self._hdr(admin_user))
        assert resp.status_code == 200
        assert "Concurrency Limit" in resp.text
        assert 'value="monitor" selected' in resp.text
        assert 'name="limit" value="6"' in resp.text
        assert f"/admin/users/{test_user.id}/toggle-concurrency-waive" in resp.text

    def test_admin_page_survives_store_failure(self, client, admin_user):
        with patch.object(concurrency, "in_flight_summary", side_effect=RuntimeError("db")):
            resp = client.get("/admin", headers=self._hdr(admin_user))
        assert resp.status_code == 200
        assert "unavailable" in resp.text


# ── PostgreSQL: concurrent admissions across workers ───────────────────────

_PG_URL = os.getenv("CONCURRENCY_TEST_PG_URL")


@pytest.mark.skipif(not _PG_URL, reason="set CONCURRENCY_TEST_PG_URL to run against PostgreSQL")
def test_pg_concurrent_acquire_respects_limit():
    """Many threads (standing in for workers) admit the same user at once:
    with FOR UPDATE on the user row exactly ``limit`` of them get a lease.
    Without the lock each would count 0 and all would be admitted."""
    engine = create_engine(_PG_URL, pool_size=20, max_overflow=0)
    SQLModel.metadata.drop_all(engine, tables=[ConcurrencyLease.__table__])
    SQLModel.metadata.create_all(engine, tables=[User.__table__, ConcurrencyLease.__table__])
    with Session(engine) as s:
        user = s.exec(select(User).where(User.username == "race")).first()
        if user is None:
            user = User(username="race", api_key="sk-race")
            s.add(user)
            s.commit()
            s.refresh(user)
        user_id = user.id

    limit, threads = 3, 16
    barrier = threading.Barrier(threads)
    results: list[str | None] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        lease, _ = concurrency.acquire(user_id, limit, "/race", enforce=True)
        with lock:
            results.append(lease)

    try:
        with patch.object(concurrency, "engine", engine):
            ts = [threading.Thread(target=worker) for _ in range(threads)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        assert sum(1 for r in results if r) == limit
    finally:
        SQLModel.metadata.drop_all(engine, tables=[ConcurrencyLease.__table__])
        with Session(engine) as s:
            s.exec(select(User).where(User.username == "race"))
            s.delete(s.exec(select(User).where(User.username == "race")).one())
            s.commit()
        engine.dispose()


class TestAppAccountsStartWaived:
    """App accounts are batch/service callers: they start exempt from the
    concurrency limit, and an admin can still un-waive one."""

    @staticmethod
    def _hdr(admin_user):
        return web_auth_header(sub=admin_user.username, scopes=["admin"])

    def _get(self, db_session, username):
        db_session.expire_all()
        return db_session.exec(select(User).where(User.username == username)).one()

    def test_admin_form_creates_waived_app(self, client, db_session, admin_user):
        resp = client.post("/admin/users/create", data={"username": "batch", "daily_limit": "5"},
                           headers=self._hdr(admin_user), follow_redirects=False)
        assert resp.status_code in (200, 303)
        assert self._get(db_session, "app_batch").concurrency_waived is True

    def test_api_create_app_defaults_waived(self, client, db_session, admin_user):
        resp = client.post("/admin/users", json={"username": "app_etl"}, headers=self._hdr(admin_user))
        assert resp.status_code == 200
        assert self._get(db_session, "app_etl").concurrency_waived is True

    def test_api_create_person_not_waived(self, client, db_session, admin_user):
        client.post("/admin/users", json={"username": "dana"}, headers=self._hdr(admin_user))
        assert self._get(db_session, "dana").concurrency_waived is False

    def test_api_explicit_value_wins(self, client, db_session, admin_user):
        client.post("/admin/users", json={"username": "app_strict", "concurrency_waived": False},
                    headers=self._hdr(admin_user))
        assert self._get(db_session, "app_strict").concurrency_waived is False

    def test_unwaived_app_is_limited(self, client, db_session, admin_user):
        app = User(username="app_limited", api_key="sk-app-limited", concurrency_waived=False)
        db_session.add(app)
        db_session.commit()
        _add_lease(app.id)
        with _mode("enforce", 1):
            resp = client.post("/v1/chat/completions", json=_CHAT, headers=auth_header("sk-app-limited"))
        assert resp.status_code == 429
