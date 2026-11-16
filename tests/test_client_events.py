"""``POST /v1/events`` (app/services/client_events.py): what a setup tool
reports about its user's machine, and the admin's *Client installs* card.

A tool that installs or configures a client says which account installed,
updated, removed or configured what, in which version, on which operating
system. It is telemetry: validated and bounded, never billed, and nothing
about the account changes by it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.models.schema import AuditLog, ClientEvent, UsageLog
from app.services import client_events
from tests.conftest import auth_header, web_auth_header

PATH = "/v1/events"


def _event(**fields) -> dict:
    body = {
        "event": "installed", "tool": "gateway-tui", "tool_version": "0.3.1",
        "component": "claude-code", "component_version": "2.1.310",
        "os": "windows", "os_version": "10.0.22631", "arch": "x64", "method": "npm", "hostname": "pc-1",
    }
    body.update(fields)
    return body


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _rows(db_session) -> list[ClientEvent]:
    return list(db_session.exec(select(ClientEvent).order_by(ClientEvent.id)).all())


class TestReport:
    def test_who_installed_what_in_which_version_on_which_os(self, client, db_session, test_user):
        resp = client.post(PATH, json=_event(), headers=auth_header())
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"object": "event", "recorded": True}
        (row,) = _rows(db_session)
        assert row.username == "testuser" and row.event == "installed" and row.ok is True
        assert (row.tool, row.tool_version) == ("gateway-tui", "0.3.1")
        assert (row.component, row.component_version, row.method) == ("claude-code", "2.1.310", "npm")
        assert (row.os, row.os_version, row.arch, row.hostname) == ("windows", "10.0.22631", "x64", "pc-1")

    @pytest.mark.parametrize("os_family", client_events.OS_FAMILIES)
    def test_every_os_is_recorded_as_its_own(self, client, db_session, test_user, os_family):
        assert client.post(PATH, json=_event(os=os_family), headers=auth_header()).status_code == 200
        assert _rows(db_session)[0].os == os_family

    def test_only_what_is_required(self, client, db_session, test_user):
        body = {"event": "settings_applied", "tool": "gateway-gui", "tool_version": "1.0", "component": "claude-code",
                "os": "linux"}
        assert client.post(PATH, json=body, headers=auth_header()).status_code == 200
        (row,) = _rows(db_session)
        assert row.component_version == "" and row.hostname == "" and row.message == "" and row.ok is True

    def test_an_update_says_from_which_version_and_a_failure_why(self, client, db_session, test_user):
        body = _event(event="updated", previous_version="2.1.280", ok=False, message="npm ERR! code EACCES")
        assert client.post(PATH, json=body, headers=auth_header()).status_code == 200
        (row,) = _rows(db_session)
        assert row.previous_version == "2.1.280" and row.ok is False and row.message == "npm ERR! code EACCES"

    def test_names_are_lowercased_and_unknown_keys_ignored(self, client, db_session, test_user):
        body = _event(tool="Gateway-TUI", os="Windows", arch="X64", hostname="PC-0421.corp.local",
                      extra={"a": 1}, username="someone-else")
        assert client.post(PATH, json=body, headers=auth_header()).status_code == 200
        (row,) = _rows(db_session)
        assert (row.tool, row.os, row.arch, row.hostname) == ("gateway-tui", "windows", "x64", "pc-0421.corp.local")
        assert row.username == "testuser"   # the account is the key's, never the body's

    def test_the_machines_name_is_optional(self, client, db_session, test_user):
        assert client.post(PATH, json=_event(hostname=None), headers=auth_header()).status_code == 200
        assert client.post(PATH, json=_event(hostname="DESKTOP_7"), headers=auth_header()).status_code == 200
        assert [row.hostname for row in _rows(db_session)] == ["", "desktop_7"]

    @pytest.mark.parametrize("change, said", [
        ({"event": "launched"}, "event must be one of"),
        ({"event": None}, "event must be one of"),
        ({"os": "solaris"}, "os must be one of"),
        ({"os": None}, "os must be one of"),
        ({"tool": ""}, "tool is required"),
        ({"tool": "a tool"}, "tool must be a short name"),
        ({"tool": "x" * 41}, "tool must be a short name"),
        ({"tool_version": None}, "tool_version is required"),
        ({"component": None}, "component is required"),
        ({"component_version": "2.1 <script>"}, "component_version must be a version"),
        ({"method": ["npm"]}, "method must be a short name"),
        ({"ok": "yes"}, "ok must be true or false"),
        ({"hostname": "pc 042"}, "hostname must be a machine name"),
        ({"hostname": "<b>pc</b>"}, "hostname must be a machine name"),
        ({"hostname": "h" * 101}, "hostname must be a machine name"),
        ({"hostname": ["pc"]}, "hostname must be a machine name"),
        ({"message": {"text": "x"}}, "message must be text"),
    ])
    def test_what_is_refused(self, client, db_session, test_user, change, said):
        resp = client.post(PATH, json=_event(**change), headers=auth_header())
        assert resp.status_code == 400 and said in resp.json()["detail"], resp.text
        assert _rows(db_session) == []

    def test_one_event_as_an_object(self, client, db_session, test_user):
        assert client.post(PATH, json=[_event()], headers=auth_header()).status_code in (400, 422)
        assert _rows(db_session) == []

    def test_free_text_is_bounded_and_never_holds_the_key(self, client, db_session, test_user):
        body = _event(ok=False, message=f"claude --key {test_user.api_key} failed\x00\x07 " + "x" * 2000,
                      os_version="W" * 500)
        assert client.post(PATH, json=body, headers=auth_header()).status_code == 200
        (row,) = _rows(db_session)
        assert test_user.api_key not in row.message and "<redacted>" in row.message
        assert len(row.message) == 500 and "\x00" not in row.message and len(row.os_version) == 80

    @pytest.mark.parametrize("field", ["hostname", "tool", "component_version"])
    def test_a_name_that_is_the_key_is_refused(self, client, db_session, test_user, field):
        resp = client.post(PATH, json=_event(**{field: test_user.api_key}), headers=auth_header())
        assert resp.status_code == 400 and test_user.api_key not in resp.text
        assert _rows(db_session) == []

    def test_an_account_cannot_fill_the_table(self, client, db_session, test_user):
        now = _now()
        for i in range(client_events.MAX_PER_HOUR):
            db_session.add(ClientEvent(username="testuser", created_at=now - timedelta(minutes=i % 50), **client_events.clean(_event())))
        db_session.commit()
        resp = client.post(PATH, json=_event(), headers=auth_header())
        assert resp.status_code == 429 and resp.headers["retry-after"] == "3600"
        assert len(_rows(db_session)) == client_events.MAX_PER_HOUR
        # Another account, and this one an hour later, are not held back.
        client_events.record(db_session, "someone-else", client_events.clean(_event()))
        client_events.record(db_session, "testuser", client_events.clean(_event()), now=now + timedelta(hours=2))

    def test_a_broken_store_is_told_without_its_text(self, client, test_user):
        from unittest.mock import patch

        with patch("app.routers.v1_api.record_client_event", side_effect=RuntimeError("no such table: client_events")):
            resp = client.post(PATH, json=_event(), headers=auth_header())
        assert resp.status_code == 503 and "client_events" not in resp.text


class TestTheAccountIsUntouched:
    def test_needs_a_key(self, client, db_session, test_user):
        assert client.post(PATH, json=_event()).status_code in (401, 403)
        assert client.post(PATH, json=_event(), headers=auth_header("sk-not-a-key")).status_code == 401
        assert _rows(db_session) == []

    def test_answers_over_budget_but_not_for_a_disabled_account(self, client, db_session, test_user):
        from decimal import Decimal

        test_user.daily_limit_usd = 1.0
        db_session.add(test_user)
        db_session.add(UsageLog(user_id=test_user.id, model="test-llm", model_type="llm", input_tokens=1,
                                output_tokens=1, cost_usd=Decimal("5"), endpoint="/v1/chat/completions"))
        db_session.commit()
        assert client.post(PATH, json=_event(), headers=auth_header()).status_code == 200
        test_user.is_disabled = True
        db_session.add(test_user)
        db_session.commit()
        assert client.post(PATH, json=_event(), headers=auth_header()).status_code == 403

    def test_never_billed_and_not_an_audit_row(self, client, db_session, test_user):
        client.post(PATH, json=_event(), headers=auth_header())
        assert db_session.exec(select(UsageLog)).all() == []
        assert db_session.exec(select(AuditLog)).all() == []

    def test_only_a_post_and_only_under_v1(self, client, test_user):
        for method in ("get", "put", "delete"):
            assert getattr(client, method)(PATH, headers=auth_header()).status_code == 405
        assert client.post("/events", json=_event(), headers=auth_header()).status_code == 404

    def test_the_rows_outlive_the_account(self, client, db_session, test_user, admin_user):
        client.post(PATH, json=_event(), headers=auth_header())
        headers = web_auth_header(sub=admin_user.username, scopes=["admin"])
        resp = client.post(f"/admin/users/{test_user.id}/delete", headers={**headers, "Accept": "application/json"})
        assert resp.status_code == 200, resp.text
        assert [row.username for row in _rows(db_session)] == ["testuser"]

    def test_runs_off_the_event_loop_and_opens_no_session_of_its_own(self):
        import inspect

        from app.routers import v1_api

        assert not inspect.iscoroutinefunction(v1_api.client_event)
        assert not hasattr(client_events, "engine")


class TestInstalledNow:
    def _add(self, db_session, username="testuser", minutes_ago=0, **fields) -> None:
        client_events.record(db_session, username, client_events.clean(_event(**fields)),
                             now=_now() - timedelta(minutes=minutes_ago))

    def _state(self, db_session) -> set[tuple]:
        return {(r.username, r.hostname, r.os, r.component, r.component_version) for r in client_events.installs(db_session)}

    def test_the_last_install_or_update_that_worked(self, db_session):
        self._add(db_session, minutes_ago=30, component_version="2.1.300")
        self._add(db_session, minutes_ago=20, event="updated", component_version="2.1.310")
        self._add(db_session, minutes_ago=10, event="updated", component_version="2.1.320", ok=False)   # failed
        self._add(db_session, minutes_ago=5, event="settings_applied", component_version="")           # not an install
        assert self._state(db_session) == {("testuser", "pc-1", "windows", "claude-code", "2.1.310")}

    def test_each_machine_and_component_apart(self, db_session):
        self._add(db_session, minutes_ago=3)
        self._add(db_session, minutes_ago=2, os="linux", hostname="server-9", component_version="2.1.305")
        self._add(db_session, minutes_ago=1, component="node", component_version="20.11.1")
        assert self._state(db_session) == {
            ("testuser", "pc-1", "windows", "claude-code", "2.1.310"),
            ("testuser", "server-9", "linux", "claude-code", "2.1.305"),
            ("testuser", "pc-1", "windows", "node", "20.11.1"),
        }

    def test_wsl_under_the_same_name_is_its_own_machine(self, db_session):
        # A WSL distribution reports its Windows host's name.
        self._add(db_session, minutes_ago=2)
        self._add(db_session, minutes_ago=1, os="linux", os_version="Ubuntu 22.04 (WSL2)", component_version="2.1.305")
        found = client_events.overview(db_session)
        assert {(r.os, r.component_version) for r in found["installs"]} == {("windows", "2.1.310"), ("linux", "2.1.305")}
        assert found["os"] == [{"os": "windows", "machines": 1}, {"os": "linux", "machines": 1}]

    def test_without_a_name_an_accounts_machines_of_one_os_count_as_one(self, db_session):
        self._add(db_session, minutes_ago=2, hostname=None)
        self._add(db_session, minutes_ago=1, hostname=None, event="updated", component_version="2.1.320")
        self._add(db_session, username="someone-else", hostname=None)
        assert self._state(db_session) == {
            ("testuser", "", "windows", "claude-code", "2.1.320"), ("someone-else", "", "windows", "claude-code", "2.1.310"),
        }

    def test_an_uninstall_takes_it_off_the_list(self, db_session):
        self._add(db_session, minutes_ago=2)
        self._add(db_session, minutes_ago=1, event="uninstalled", component_version="")
        assert self._state(db_session) == set()

    def test_counted_per_os_and_per_version(self, db_session):
        self._add(db_session)
        self._add(db_session, os="linux", hostname="server-9")
        self._add(db_session, username="someone-else", hostname="pc-7", component_version="2.1.300")
        found = client_events.overview(db_session)
        assert found["accounts"] == 2
        assert found["os"] == [{"os": "windows", "machines": 2}, {"os": "linux", "machines": 1}]
        assert [(v["version"], v["usernames"]) for v in found["versions"]] == [
            ("2.1.310", ["testuser"]), ("2.1.300", ["someone-else"]),
        ]


class TestAdminCard:
    def _page(self, client, admin_user) -> str:
        return client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text

    def test_empty(self, client, admin_user):
        body = self._page(client, admin_user)
        assert 'id="client-installs"' in body and "No tool has reported an install yet." in body

    def test_it_is_on_the_clients_tab(self, client, admin_user):
        body = self._page(client, admin_user)
        assert body.index('data-panel="clients"') < body.index('id="client-installs"') < body.index('data-panel="security"')

    def test_what_is_installed_and_what_happened(self, client, db_session, test_user, admin_user):
        client.post(PATH, json=_event(hostname="PC-0421"), headers=auth_header())
        client.post(PATH, json=_event(event="updated", component_version="2.1.320", previous_version="2.1.310",
                                      ok=False, message="npm ERR! <b>EACCES</b>"), headers=auth_header())
        body = self._page(client, admin_user)
        card = body[body.index('id="client-installs"'):body.index('data-panel="security"')]
        assert "1 account in" in card and "Windows</span> 1 machine" in card
        assert "2.1.310" in card and "gateway-tui" in card and "10.0.22631" in card
        assert ">pc-0421<" in card
        assert "1 failed" in card and "2.1.310 → 2.1.320" in card
        assert "&lt;b&gt;EACCES&lt;/b&gt;" in card and "<b>EACCES</b>" not in card   # what a client sent is escaped

    def test_a_missing_table_costs_the_card_only(self, client, admin_user):
        from unittest.mock import patch

        with patch("app.routers.admin.page._client_installs_view", side_effect=RuntimeError("no table")):
            body = self._page(client, admin_user)
        assert "Client installs could not be loaded" in body and 'id="client-rules"' in body
