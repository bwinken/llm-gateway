"""Client rules (app/services/client_checks.py, client_rules.py) and mail (mailer.py).

The checks are code, each with a violation id (CC001, ...). A Messages
request that trips an enabled check records a violation for the account;
the account then sees the check's notice on its dashboard, can download a
script that sets the check's environment variables in its settings.json,
can be reminded once a day in the terminal, and can be mailed (by hand or
automatically). An admin sets how each check is used, never what it
detects. The Claude Code version of every Messages request is recorded per
account. None of it may fail a request, except the reminder an admin chose.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlmodel import select

from app.models.schema import ClientMailLog, ClientRuleSetting, ClientVersion, ClientViolation, User
from app.services import client_checks, client_rules, mailer
from tests.conftest import auth_header, make_httpx_response, web_auth_header

_DOWNSTREAM = {
    "id": "chatcmpl-1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}
_UA = "claude-cli/2.1.280 (external, cli)"
_ENV = {"CLAUDE_CODE_AUTO_MODE_SERVER": "0"}
AUTO = "CC001"      # auto mode waits for a server-side check (request has `safeguards`); on by default
HTTP = "CC002"
OUTDATED = "CC003"
UNKNOWN_MODEL = "CC004"
NO_CLOUD_ACCESS = "CC005"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _configure(db_session, rule_id: str, **kw) -> client_rules.Rule:
    """Save settings for a check the way the admin form does (starting from
    its current ones), and load them into this worker."""
    rule = client_rules.get_rule(db_session, rule_id)
    values = dict(
        enabled=True, action=rule.action, title=rule.title, message=rule.message,
        patch=rule.patch, patch_azure=rule.patch_azure, patch_aws=rule.patch_aws,
        email_subject=rule.email_subject, auto_email=rule.auto_email, email_repeat_days=rule.email_repeat_days,
    )
    values.update(kw)
    rule = client_rules.save_settings(db_session, rule.check, values, updated_by="test")
    client_rules.refresh()
    return rule


def _messages(client, key: str = "sk-testkey123", ua: str | None = _UA, url: str = "https://testserver/v1/messages", **extra):
    async def _post(*args, **kwargs):
        return make_httpx_response(200, _DOWNSTREAM)

    body = {"model": "test-llm", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}], **extra}
    headers = auth_header(key)
    if ua:
        headers["User-Agent"] = ua
    with patch.object(client.__httpx_mock__, "post", side_effect=_post) as post:
        resp = client.post(url, json=body, headers=headers)
    return resp, post


def _violators(db_session, rule_id: str) -> list[str]:
    db_session.expire_all()
    return sorted(v.username for v in db_session.exec(select(ClientViolation).where(ClientViolation.rule_id == rule_id)).all())


def _violations(db_session, rule_id: str) -> list[ClientViolation]:
    db_session.expire_all()
    return list(db_session.exec(select(ClientViolation).where(ClientViolation.rule_id == rule_id)).all())


def _violation(db_session, rule_id: str, username: str, **kw) -> ClientViolation:
    row = ClientViolation(rule_id=rule_id, username=username, first_seen_at=_now(), last_seen_at=_now(), **kw)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _admin(admin: User) -> dict:
    return {**web_auth_header(sub=admin.username, scopes=["admin"]), "Accept": "application/json"}


_FORM = {
    "enabled": "on", "action": "notice", "title": "Fix auto mode", "message": "Apply the patch.",
    "patch": "CLAUDE_CODE_AUTO_MODE_SERVER=0",
}


@pytest.fixture(autouse=True)
def _no_baseline(monkeypatch):
    """Most tests are about one check's own patch: the settings every patch
    carries are switched off here and covered by ``TestBaseline``."""
    monkeypatch.setattr(client_checks, "BASELINE_ENV", "")


@pytest.fixture
def mail_on(monkeypatch):
    for name, value in (
        ("EXCHANGE_SERVER", "mail.example.test"), ("EXCHANGE_USER", "EX\\svc"), ("EXCHANGE_PASSWORD", "pw"),
        ("EXCHANGE_EMAIL", "noreply@example.test"), ("COMPANY_EMAIL_DOMAIN", "example.test"),
        ("PUBLIC_BASE_URL", "https://gw.example.test/"),
    ):
        monkeypatch.setenv(name, value)
    sent: list[tuple[str, str, str]] = []

    def _send(to, subject, body):
        sent.append((to, subject, body))

    with patch.object(mailer, "_send_sync", side_effect=_send):
        yield sent


def _facts(**kw) -> client_checks.Facts:
    values = dict(body={}, model="", version="", scheme="https", azure_access=False, aws_access=False)
    values.update(kw)
    return client_checks.Facts(**values)


class TestRegistry:
    def test_ids_are_unique_and_stable(self):
        ids = [c.id for c in client_checks.CHECKS]
        assert len(ids) == len(set(ids)) and all(i.startswith("CC") and len(i) == 5 for i in ids)
        # Never renumber: these ids are in the database and in mail people received.
        assert ids[:5] == ["CC001", "CC002", "CC003", "CC004", "CC005"]

    def test_every_check_can_tell_its_user_something(self):
        for check in client_checks.CHECKS:
            assert check.name and check.description and check.title and check.message, check.id
            client_rules.parse_env(check.env), client_rules.parse_env(check.env_azure), client_rules.parse_env(check.env_aws)

    def test_only_auto_mode_is_on_before_an_admin_decides(self):
        assert [c.id for c in client_checks.CHECKS if c.enabled] == [AUTO]

    def test_a_retired_check_is_no_longer_evaluated_or_shown(self, client, db_session, test_user, admin_user):
        from dataclasses import replace

        retired = tuple(replace(c, retired="superseded") if c.id == AUTO else c for c in client_checks.CHECKS)
        with patch.object(client_checks, "ACTIVE", tuple(c for c in retired if not c.retired)), \
             patch.object(client_checks, "RETIRED", tuple(c for c in retired if c.retired)), \
             patch.object(client_checks, "BY_ID", {c.id: c for c in retired}):
            client_rules.refresh()
            assert _messages(client, safeguards={})[0].status_code == 200
            assert _violators(db_session, AUTO) == []
            assert client_rules.get_rule(db_session, AUTO) is None
            assert AUTO not in [r.id for r in client_rules.rules(db_session)]
            resp = client.post(f"/admin/client-rules/{AUTO}", data=_FORM, headers=_admin(admin_user))
            assert resp.status_code == 404


class TestLifecycle:
    """Active -> deprecated -> retired, all set in code (client_checks.py)."""

    @staticmethod
    def _with(**changes):
        """The registry with CC001 changed, patched in for the block."""
        from contextlib import ExitStack
        from dataclasses import replace

        checks = tuple(replace(c, **changes) if c.id == AUTO else c for c in client_checks.CHECKS)
        stack = ExitStack()
        stack.enter_context(patch.object(client_checks, "ACTIVE", tuple(c for c in checks if not c.retired)))
        stack.enter_context(patch.object(client_checks, "RETIRED", tuple(c for c in checks if c.retired)))
        stack.enter_context(patch.object(client_checks, "BY_ID", {c.id: c for c in checks}))
        return stack

    def test_a_deprecated_check_still_records_and_shows_its_notice(self, client, db_session, test_user):
        with self._with(deprecated="Claude Code 3 falls back by itself."):
            client_rules.refresh()
            assert _messages(client, safeguards={})[0].status_code == 200
            assert _violators(db_session, AUTO) == ["testuser"]
            body = client.get("/dashboard", headers=web_auth_header(sub=test_user.username)).text
            assert 'id="clientNotice-CC001"' in body

    def test_a_deprecated_check_never_reminds(self, client, db_session, test_user):
        with self._with(deprecated="on its way out"):
            _configure(db_session, AUTO, action="remind")
            assert _messages(client, safeguards={})[0].status_code == 200
            assert _violators(db_session, AUTO) == ["testuser"]

    def test_a_deprecated_check_mails_nobody(self, client, db_session, admin_user, test_user, mail_on):
        with self._with(deprecated="on its way out"):
            _configure(db_session, AUTO, auto_email=True)
            _violation(db_session, AUTO, "testuser")
            assert "sent=0" in client_rules.run_auto_mail(db_session)
            assert client_rules.send_rule_mail(AUTO, only_unmailed=False) == {"sent": 0, "failed": 0}
            resp = client.post(f"/admin/client-rules/{AUTO}/mail", headers=_admin(admin_user))
            assert resp.status_code == 400 and "deprecated" in resp.json()["detail"]
        assert mail_on == []

    def test_admin_sees_the_stage_and_the_reason(self, client, db_session, admin_user):
        headers = web_auth_header(sub=admin_user.username, scopes=["admin"])
        body = client.get("/admin", headers=headers).text
        assert "data-cr-deprecated" not in body and 'id="client-rules-retired"' not in body
        with self._with(deprecated="Claude Code 3 falls back by itself."):
            body = client.get("/admin", headers=headers).text
            assert "data-cr-deprecated" in body and "Claude Code 3 falls back by itself." in body
        with self._with(retired="Nobody has tripped it since March."):
            _violation(db_session, AUTO, "testuser")
            body = client.get("/admin", headers=headers).text
            assert 'id="client-rule-CC001"' not in body
            retired = body.split('id="client-rules-retired"', 1)[1][:3000]
            assert "CC001" in retired and "Nobody has tripped it since March." in retired

    def test_history_says_when_a_check_was_last_tripped(self, db_session):
        now = _now()
        for name, days in (("a", 40), ("b", 35)):
            row = _violation(db_session, AUTO, name)
            row.last_seen_at = now - timedelta(days=days)
            db_session.add(row)
        _violation(db_session, HTTP, "c")
        db_session.commit()
        seen = client_rules.history(db_session, now)
        assert (seen[AUTO]["accounts"], seen[AUTO]["quiet_days"]) == (2, 35)      # all time, not the 7-day window
        assert seen[HTTP]["quiet_days"] == 0 and OUTDATED not in seen
        assert client_rules.violations(db_session, AUTO, now) == []               # while the active list is empty

    def test_a_check_quiet_for_a_month_is_suggested_for_retirement(self, client, db_session, admin_user):
        auto = client_rules.get_rule(db_session, AUTO)
        assert not client_rules.is_stale(auto, None)                              # never tripped: says nothing
        assert not client_rules.is_stale(auto, {"quiet_days": client_rules.STALE_DAYS - 1})
        assert client_rules.is_stale(auto, {"quiet_days": client_rules.STALE_DAYS})
        off = client_rules.get_rule(db_session, HTTP)
        assert not client_rules.is_stale(off, {"quiet_days": 400})                # a check that is off is not judged

        row = _violation(db_session, AUTO, "testuser")
        row.last_seen_at = _now() - timedelta(days=45)
        db_session.add(row)
        db_session.commit()
        body = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        card = body.split('id="client-rule-CC001"', 1)[1].split('id="client-rule-CC002"', 1)[0]
        assert "quiet for 45 days" in card and "45 days ago" in card and "1 account ever" in card
        assert "No violation has ever been recorded." in body.split('id="client-rule-CC002"', 1)[1]

    def test_a_check_is_never_both_stages_ambiguously(self):
        for check in client_checks.CHECKS:
            assert (check in client_checks.ACTIVE) != (check in client_checks.RETIRED)


class TestChecks:
    """One test per check: what trips it and what does not."""

    def test_cc001_auto_mode_server_review(self):
        detect = client_checks.BY_ID[AUTO].detect
        assert detect(_facts(body={"safeguards": {}})) and not detect(_facts(body={"model": "x"}))

    def test_cc002_plain_http(self):
        detect = client_checks.BY_ID[HTTP].detect
        assert detect(_facts(scheme="http")) and not detect(_facts(scheme="https"))

    def test_cc003_outdated_claude_code(self):
        detect = client_checks.BY_ID[OUTDATED].detect
        with patch.object(client_checks, "MIN_CLAUDE_CODE_VERSION", "2.1.302"):
            assert detect(_facts(version="2.1.99")) and detect(_facts(version="2.0"))
            assert not detect(_facts(version="2.1.302")) and not detect(_facts(version="2.2.0"))
            assert not detect(_facts(version=""))          # other clients are never listed
        assert client_checks.version_key(client_checks.MIN_CLAUDE_CODE_VERSION)   # the constant is a version

    def test_cc004_unknown_model(self):
        detect = client_checks.BY_ID[UNKNOWN_MODEL].detect
        assert detect(_facts(model="claude-haiku-9"))
        for known in ("test-llm", "azure-gpt-4", "bedrock-claude", ""):
            assert not detect(_facts(model=known)), known

    def test_cc005_cloud_model_without_access(self):
        detect = client_checks.BY_ID[NO_CLOUD_ACCESS].detect
        assert detect(_facts(model="azure-gpt-4")) and detect(_facts(model="bedrock-claude"))
        assert not detect(_facts(model="azure-gpt-4", azure_access=True))
        assert not detect(_facts(model="bedrock-claude", aws_access=True))
        assert detect(_facts(model="bedrock-claude", azure_access=True))      # the wrong cloud does not help
        assert not detect(_facts(model="test-llm")) and not detect(_facts(model="nope"))


class TestVersionParsing:
    def test_claude_code_user_agent(self):
        assert client_rules.parse_version("claude-cli/2.1.302 (external, cli)") == "2.1.302"
        assert client_rules.parse_version("python-httpx/0.28") == ""
        assert client_rules.parse_version(None) == ""

    def test_versions_order_numerically(self):
        assert client_checks.version_key("2.1.99") < client_checks.version_key("2.1.302")
        assert client_checks.version_key("latest") == ()


class TestRequestPath:
    def test_a_violation_is_recorded_under_its_id_and_the_request_is_still_served(self, client, db_session, test_user):
        client_rules.refresh()                       # CC001 is on by default: no settings row needed
        resp, post = _messages(client, safeguards={"x": 1})
        assert resp.status_code == 200 and resp.json()["content"][0]["text"] == "ok"
        [row] = _violations(db_session, AUTO)
        assert (row.rule_id, row.username, row.client_version) == ("CC001", "testuser", "2.1.280")
        assert "safeguards" not in post.call_args.kwargs["json"]   # still not forwarded

    def test_an_ordinary_request_violates_nothing(self, client, db_session, test_user):
        client_rules.refresh()
        assert _messages(client)[0].status_code == 200
        assert db_session.exec(select(ClientViolation)).all() == []

    def test_a_check_that_is_off_records_nothing(self, client, db_session, test_user):
        client_rules.refresh()
        _messages(client, url="http://testserver/v1/messages")     # CC002 is off by default
        assert _violators(db_session, HTTP) == []
        _configure(db_session, AUTO, enabled=False)
        _messages(client, safeguards={})
        assert _violators(db_session, AUTO) == []

    def test_plain_http_is_the_connections_scheme_not_a_header(self, client, db_session, test_user):
        _configure(db_session, HTTP)
        _messages(client)                                           # https
        assert _violators(db_session, HTTP) == []
        async def _post(*args, **kwargs):
            return make_httpx_response(200, _DOWNSTREAM)
        body = {"model": "test-llm", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}
        sneaky = {**auth_header(), "X-Forwarded-Proto": "https"}    # a client cannot hide behind its own header
        with patch.object(client.__httpx_mock__, "post", side_effect=_post):
            assert client.post("http://testserver/v1/messages", json=body, headers=sneaky).status_code == 200
        assert _violators(db_session, HTTP) == ["testuser"]

    def test_thresholds_are_code_not_settings(self, client, db_session, test_user):
        """The minimum version is a constant in client_checks.py; the settings
        row holds nothing a check decides with."""
        _configure(db_session, OUTDATED)
        with patch.object(client_checks, "MIN_CLAUDE_CODE_VERSION", "2.1.200"):
            _messages(client)                                       # 2.1.280 is new enough for 2.1.200
            assert _violators(db_session, OUTDATED) == []
        client_rules.reset()
        client_rules.refresh()
        with patch.object(client_checks, "MIN_CLAUDE_CODE_VERSION", "2.1.302"):
            _messages(client)
        assert _violators(db_session, OUTDATED) == ["testuser"]
        columns = set(ClientRuleSetting.__table__.columns.keys())
        assert columns == {
            "rule_id", "enabled", "action", "title", "message", "patch", "patch_azure", "patch_aws",
            "email_subject", "auto_email", "email_repeat_days", "updated_by", "updated_at",
        }

    def test_access_flags_reach_the_check(self, client, db_session, test_user):
        _configure(db_session, NO_CLOUD_ACCESS)
        _messages(client, model="bedrock-claude")                   # the test user has cloud access
        assert _violators(db_session, NO_CLOUD_ACCESS) == []
        db_session.add(User(username="local.only", password_hash="x", api_key="sk-localonly", daily_limit_usd=10.0))
        db_session.commit()
        assert _messages(client, key="sk-localonly", model="bedrock-claude")[0].status_code == 200
        assert _violators(db_session, NO_CLOUD_ACCESS) == ["local.only"]

    def test_one_request_can_trip_several_checks(self, client, db_session, test_user):
        _configure(db_session, UNKNOWN_MODEL)
        _configure(db_session, AUTO)
        _messages(client, model="claude-haiku-9", safeguards={})
        assert _violators(db_session, AUTO) == ["testuser"] and _violators(db_session, UNKNOWN_MODEL) == ["testuser"]

    def test_at_most_one_write_per_window(self, client, db_session, test_user):
        client_rules.refresh()
        with patch.object(client_rules, "_record", wraps=client_rules._record) as rec:
            for _ in range(4):
                _messages(client, safeguards={})
        assert rec.call_count == 1

    def test_a_broken_store_never_fails_the_request(self, client, db_session, test_user):
        client_rules.refresh()
        with patch.object(client_rules, "_record", side_effect=RuntimeError("db down")):
            assert _messages(client, safeguards={})[0].status_code == 200

    def test_a_check_that_raises_finds_nothing_and_spares_the_others(self, client, db_session, test_user):
        from dataclasses import replace

        def _boom(facts):
            raise RuntimeError("bug in a check")

        broken = tuple(replace(c, detect=_boom, enabled=True) if c.id == HTTP else c for c in client_checks.CHECKS)
        with patch.object(client_checks, "ACTIVE", broken):
            client_rules.refresh()
            assert _messages(client, safeguards={})[0].status_code == 200
        assert _violators(db_session, HTTP) == [] and _violators(db_session, AUTO) == ["testuser"]

    def test_cloud_messages_routes_are_watched_too(self):
        import inspect

        from app.routers import aws_api, azure_api

        assert "client_rules.observe_request" in inspect.getsource(azure_api.azure_messages)
        assert "client_rules.observe_request" in inspect.getsource(aws_api.bedrock_messages)


class TestVersions:
    def test_the_version_of_each_account_is_recorded(self, client, db_session, test_user):
        _messages(client, ua="claude-cli/2.1.280 (external, cli)")
        client_rules.reset()   # a later hour
        _messages(client, ua="claude-cli/2.1.302 (external, cli)")
        rows = db_session.exec(select(ClientVersion)).all()
        assert sorted(r.version for r in rows) == ["2.1.280", "2.1.302"]
        assert {r.username for r in rows} == {"testuser"} and {r.client for r in rows} == {"claude-code"}

    def test_other_clients_are_not_recorded(self, client, db_session, test_user):
        _messages(client, ua="python-httpx/0.28")
        assert db_session.exec(select(ClientVersion)).all() == []

    def test_overview_counts_each_account_under_its_newest_version(self, db_session):
        now = _now()
        for username, version, age in (("a", "2.1.280", 3), ("a", "2.1.302", 1), ("b", "2.1.280", 2), ("old", "2.0.1", 60)):
            when = now - timedelta(days=age)
            db_session.add(ClientVersion(username=username, client="claude-code", version=version, first_seen_at=when, last_seen_at=when))
        db_session.commit()
        view = client_rules.version_overview(db_session, now)
        assert view["accounts"] == 2
        assert [(v["version"], v["usernames"]) for v in view["versions"]] == [("2.1.302", ["a"]), ("2.1.280", ["b"])]


class TestReminder:
    def test_first_violating_request_of_the_day_carries_the_message(self, client, db_session, test_user):
        _configure(db_session, AUTO, action="remind", title="Auto mode needs a setting")
        first, post = _messages(client, safeguards={})
        assert first.status_code == 400 and first.headers["X-Gateway-Reminder"] == "CC001"
        error = first.json()
        assert error["type"] == "error" and "Auto mode needs a setting" in error["error"]["message"]
        assert "[CC001]" in error["error"]["message"] and "send your message again" in error["error"]["message"]
        post.assert_not_called()
        assert _messages(client, safeguards={})[0].status_code == 200   # the retry goes through

    def test_one_reminder_a_day_across_workers(self, client, db_session, test_user):
        _configure(db_session, AUTO, action="remind")
        assert _messages(client, safeguards={})[0].status_code == 400
        client_rules.reset()            # another worker: no memory of the first reminder
        client_rules.refresh()
        assert _messages(client, safeguards={})[0].status_code == 200

    def test_notice_never_fails_a_request(self, client, db_session, test_user):
        _configure(db_session, AUTO, action="notice")
        assert _messages(client, safeguards={})[0].status_code == 200

    def test_app_accounts_are_never_reminded(self, client, db_session):
        db_session.add(User(username="app_batch", password_hash="x", api_key="sk-appkey", daily_limit_usd=10.0))
        db_session.commit()
        _configure(db_session, AUTO, action="remind")
        assert _messages(client, key="sk-appkey", safeguards={})[0].status_code == 200
        assert _violators(db_session, AUTO) == ["app_batch"]   # still recorded


class TestPatch:
    def test_lines_or_json_only_ever_name_environment_variables(self):
        assert client_rules.parse_env("") == {}
        assert client_rules.parse_env("# why\nA=1\n B = two words \n-OLD\n") == {"A": "1", "B": "two words", "OLD": None}
        assert client_rules.parse_env('{"A": "1", "N": 3, "T": true, "OLD": null}') == {"A": "1", "N": "3", "T": "true", "OLD": None}
        assert client_rules.parse_env("URL=https://gw.example.test/v1?a=b") == {"URL": "https://gw.example.test/v1?a=b"}
        for bad in ("A", "1A=x", "A B=x", "{broken", '{"A": {"nested": 1}}', '{"A": [1]}', "[1]", "permissions.allow=x"):
            with pytest.raises(ValueError):
                client_rules.parse_env(bad)

    def test_stored_patch_round_trips_to_the_admins_lines(self):
        env = client_rules.parse_env("A=1\n-OLD")
        assert client_rules.env_lines(client_rules.load_env(client_rules.dump_env(env))) == "A=1\n-OLD"
        assert client_rules.dump_env({}) == "" and client_rules.load_env("{broken") == {}

    def test_only_the_env_block_changes(self):
        settings = {"env": {"KEEP": "1", "OLD": "x"}, "model": "a", "permissions": {"allow": ["x"]}}
        client_rules.apply_env(settings, {"NEW": "2", "OLD": None})
        assert settings == {"env": {"KEEP": "1", "NEW": "2"}, "model": "a", "permissions": {"allow": ["x"]}}

    def test_each_audience_gets_its_own_variables(self):
        rule = SimpleNamespace(
            patch='{"SERVER": "0", "MODEL": "local-m", "LOCAL_ONLY": "1"}',
            patch_azure='{"MODEL": "azure-m"}', patch_aws='{"MODEL": "claude-m", "LOCAL_ONLY": null}',
        )
        assert client_rules.effective_env(rule, False, False) == {"SERVER": "0", "MODEL": "local-m", "LOCAL_ONLY": "1"}
        assert client_rules.effective_env(rule, True, False)["MODEL"] == "azure-m"
        assert client_rules.effective_env(rule, False, True) == {"SERVER": "0", "MODEL": "claude-m", "LOCAL_ONLY": None}
        assert client_rules.effective_env(rule, True, True)["MODEL"] == "claude-m"     # AWS wins over Azure

    def test_admin_text_never_reaches_cmd(self):
        bat = client_rules.render_patch_bat('100% "fix" & <del> | %PATH%', {"A": '%PATH% & "q" 中文'})
        head = bat.split("//GW-NODE-BEGIN")[0]
        assert "%PATH%" not in head and "中文" not in bat and "<del>" not in bat
        assert "\n" not in bat.replace("\r\n", "")      # CRLF throughout, as cmd.exe needs

    @pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
    @pytest.mark.parametrize("before", [
        None,
        "",
        "\ufeff" + json.dumps({"env": {"KEEP": "1", "OLD": "x"}, "gone": 1}),
        json.dumps({"model": "m", "env": None}),
        json.dumps({
            "env": {"KEEP": "1", "OLD": "x", "A": "é中文"}, "model": "old",
            "permissions": {"allow": ["Bash(ls:*)"], "deny": [], "one": ["only"], "nested": [[1, 2], [], {"k": None}]},
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": 'echo "hi"'}]}]},
            "num": 1.5, "t": True, "nul": None, "emptyobj": {}, "date": "2026-11-10T08:00:00Z",
        }, indent=2),
    ])
    def test_the_script_changes_what_apply_env_changes(self, tmp_path, before):
        change = {"CLAUDE_CODE_AUTO_MODE_SERVER": "0", "M": "模型 x", "OLD": None, "Q": 'it\'s "q" %PATH% & <b>'}
        result = self._run(tmp_path, change, before)
        assert result.returncode == 0, result.stdout + result.stderr
        raw = (tmp_path / ".claude" / "settings.json").read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        was = json.loads(before.lstrip("\ufeff")) if before else {}
        got = json.loads(raw.decode("utf-8"))
        assert got == client_rules.apply_env(copy.deepcopy(was), change)
        assert {k: v for k, v in got.items() if k != "env"} == {k: v for k, v in was.items() if k != "env"}
        assert (tmp_path / ".claude" / "settings.json.bak").exists() == (before is not None)

    @pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
    @pytest.mark.parametrize("before", ["{not json", "[1, 2]", '{"env": "a string"}'])
    def test_the_script_leaves_a_file_it_cannot_read_alone(self, tmp_path, before):
        result = self._run(tmp_path, _ENV, before)
        assert result.returncode != 0
        assert (tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8") == before

    @pytest.mark.skipif(shutil.which("sh") is None, reason="sh not installed")
    @pytest.mark.parametrize("runtime", ["node", "python3"])
    @pytest.mark.parametrize("before", [
        None,
        "\ufeff" + json.dumps({"env": {"KEEP": "1", "OLD": "x"}, "gone": 1}),
        json.dumps({
            "env": {"KEEP": "1", "OLD": "x", "A": "é中文"}, "model": "old",
            "permissions": {"allow": ["Bash(ls:*)"], "deny": [], "nested": [[1, 2], [], {"k": None}]},
            "num": 1.5, "t": True, "nul": None, "emptyobj": {},
        }, indent=2),
    ])
    def test_the_shell_script_changes_what_apply_env_changes(self, tmp_path, runtime, before):
        change = {"CLAUDE_CODE_AUTO_MODE_SERVER": "0", "M": "模型 x", "OLD": None, "Q": 'it\'s "q" $HOME `id` & <b>'}
        result = self._run_sh(tmp_path, change, before, runtime)
        assert result.returncode == 0, result.stdout + result.stderr
        raw = (tmp_path / ".claude" / "settings.json").read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        was = json.loads(before.lstrip("\ufeff")) if before else {}
        got = json.loads(raw.decode("utf-8"))
        assert got == client_rules.apply_env(copy.deepcopy(was), change)
        assert {k: v for k, v in got.items() if k != "env"} == {k: v for k, v in was.items() if k != "env"}
        assert (tmp_path / ".claude" / "settings.json.bak").exists() == (before is not None)

    @pytest.mark.skipif(shutil.which("sh") is None, reason="sh not installed")
    @pytest.mark.parametrize("runtime", ["node", "python3"])
    @pytest.mark.parametrize("before", ["{not json", "[1, 2]", '{"env": "a string"}'])
    def test_the_shell_script_leaves_a_file_it_cannot_read_alone(self, tmp_path, runtime, before):
        result = self._run_sh(tmp_path, _ENV, before, runtime)
        assert result.returncode != 0 and "not applied" in result.stdout
        assert (tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8") == before

    def test_the_shell_script_says_what_it_needs_when_neither_runtime_is_there(self, tmp_path):
        result = self._run_sh(tmp_path, _ENV, None, None)
        assert result.returncode != 0 and "Node.js or Python 3" in result.stderr
        assert not (tmp_path / ".claude").exists()

    def test_admin_text_never_reaches_the_shell(self):
        sh = client_rules.render_patch_sh("$(rm -rf ~) `id` 'q' \"q\"", {"A": "$(id) `id` 'q' 中文"}, "x.sh")
        assert sh.isascii() and "$(" not in sh.split("node - <<")[0] and "`" not in sh.split("node - <<")[0]
        assert "\r" not in sh and sh.startswith("#!/bin/sh\n")

    def test_both_scripts_carry_the_same_node_logic(self):
        env = {"A": "1"}
        node = client_rules._NODE_APPLY.replace("__PATCH_B64__", "X")
        for script in (client_rules.render_patch_bat("r", env), client_rules.render_patch_sh("r", env)):
            import re
            body = re.sub(r"Buffer\.from\('[^']+'", "Buffer.from('X'", script.replace("\r\n", "\n"))
            assert node in body

    @staticmethod
    def _run_sh(home: Path, change: dict, before: str | None, runtime: str | None) -> subprocess.CompletedProcess:
        """Run the .sh as a user would, with only ``runtime`` on the PATH."""
        if runtime is not None and shutil.which(runtime) is None:
            pytest.skip(f"{runtime} not installed")
        if before is not None:
            (home / ".claude").mkdir()
            (home / ".claude" / "settings.json").write_text(before, encoding="utf-8")
        script = home / "claude-code-patch.sh"
        script.write_text(client_rules.render_patch_sh("rule", change, script.name), encoding="utf-8")
        bindir = home / "bin"
        bindir.mkdir()
        if runtime is not None:
            (bindir / runtime).symlink_to(shutil.which(runtime))
        import os
        env = {"HOME": str(home), "PATH": str(bindir), "LANG": os.environ.get("LANG", "C.UTF-8")}
        return subprocess.run([shutil.which("sh"), str(script)], env=env, capture_output=True, text=True, timeout=60)

    @staticmethod
    def _run(home: Path, change: dict, before: str | None) -> subprocess.CompletedProcess:
        """Run the .bat's own Node line (cmd.exe aside) with ``home`` as the user's home."""
        if before is not None:
            (home / ".claude").mkdir()
            (home / ".claude" / "settings.json").write_text(before, encoding="utf-8")
        bat = client_rules.render_patch_bat("rule", change)
        script = home / "patch.bat"
        script.write_bytes(bat.encode("utf-8"))
        [line] = [ln for ln in bat.split("\r\n") if ln.startswith('node -e "')]
        import os
        env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "GW_PATCH_SELF": str(script)}
        return subprocess.run(["node", "-e", line[len('node -e "'):-1]], env=env, capture_output=True, text=True, timeout=60)


class TestBaseline:
    """Settings every user should have ride along in every patch that has
    something to apply; there is no check for them."""

    DISCOVERY = "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"

    @pytest.fixture(autouse=True)
    def _baseline(self, monkeypatch):
        monkeypatch.setattr(client_checks, "BASELINE_ENV", f"{self.DISCOVERY}=1")

    def test_the_shipped_baseline_turns_model_discovery_on(self, monkeypatch):
        monkeypatch.undo()
        assert client_rules.baseline_env() == {self.DISCOVERY: "1"}

    def test_a_patch_carries_it(self):
        rule = SimpleNamespace(patch='{"SERVER": "0"}', patch_azure="", patch_aws='{"MODEL": "claude-m"}')
        assert client_rules.effective_env(rule, False, False) == {"SERVER": "0", self.DISCOVERY: "1"}
        assert client_rules.effective_env(rule, False, True) == {"SERVER": "0", "MODEL": "claude-m", self.DISCOVERY: "1"}

    def test_nothing_to_apply_stays_nothing(self):
        rule = SimpleNamespace(patch="", patch_azure='{"MODEL": "azure-m"}', patch_aws="")
        assert client_rules.effective_env(rule, False, False) == {}          # no patch just for the baseline
        assert client_rules.effective_env(rule, True, False) == {"MODEL": "azure-m", self.DISCOVERY: "1"}

    def test_the_checks_own_value_wins(self):
        rule = SimpleNamespace(patch=f'{{"{self.DISCOVERY}": "0"}}', patch_azure="", patch_aws="")
        assert client_rules.effective_env(rule, False, False) == {self.DISCOVERY: "0"}
        removing = SimpleNamespace(patch=f'{{"X": "1", "{self.DISCOVERY}": null}}', patch_azure="", patch_aws="")
        assert client_rules.effective_env(removing, False, False) == {"X": "1", self.DISCOVERY: None}

    def test_the_user_gets_it_on_the_dashboard_in_the_download_and_in_mail(self, client, db_session, test_user, mail_on):
        _configure(db_session, AUTO, auto_email=True)                       # CC001's default patch
        _configure(db_session, OUTDATED)                                    # no patch of its own
        _violation(db_session, AUTO, "testuser")
        _violation(db_session, OUTDATED, "testuser")
        headers = web_auth_header(sub=test_user.username)
        body = client.get("/dashboard", headers=headers).text
        assert self.DISCOVERY in body
        for url in ("/dashboard/client-rules/all/patch.bat", "/dashboard/client-rules/all/patch.sh"):
            import base64
            import re
            text = client.get(url, headers=headers).text
            env = json.loads(base64.b64decode(re.search(r"Buffer\.from\('([^']+)'", text).group(1)))
            assert env == {"CLAUDE_CODE_AUTO_MODE_SERVER": "0", self.DISCOVERY: "1"}, url
        client_rules.run_auto_mail(db_session)
        assert self.DISCOVERY in mail_on[0][2]

    def test_it_is_not_a_conflict_between_checks(self, db_session):
        _configure(db_session, AUTO, patch='{"A": "1"}')
        _configure(db_session, HTTP, patch='{"B": "2"}')
        assert client_rules.env_conflicts(client_rules.rules(db_session)) == []

    def test_the_admin_sees_what_every_patch_carries(self, client, admin_user):
        body = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        line = body.split('id="client-rules-baseline"', 1)[1][:1200]
        assert f"{self.DISCOVERY}=1" in line


class TestDashboard:
    def _page(self, client, user: User) -> str:
        return client.get("/dashboard", headers=web_auth_header(sub=user.username)).text

    def test_violating_account_sees_notice_patch_and_download(self, client, db_session, test_user):
        _violation(db_session, AUTO, "testuser", client_version="2.1.280")
        body = self._page(client, test_user)
        assert 'id="clientNotice-CC001"' in body and client_checks.BY_ID[AUTO].title in body
        assert "CLAUDE_CODE_AUTO_MODE_SERVER" in body
        assert "/dashboard/client-rules/all/patch.bat" in body and "/dashboard/client-rules/all/patch.sh" in body
        assert 'action="/dashboard/client-rules/CC001/resolved"' in body

    def test_the_admins_wording_replaces_the_default(self, client, db_session, test_user):
        _configure(db_session, AUTO, title="Our own title", message="Our own words.")
        _violation(db_session, AUTO, "testuser")
        body = self._page(client, test_user)
        assert "Our own title" in body and "Our own words." in body and client_checks.BY_ID[AUTO].title not in body

    def test_the_patch_follows_the_accounts_access(self, client, db_session, test_user):
        _configure(db_session, AUTO, patch='{"MODEL": "local-m"}', patch_azure='{"MODEL": "azure-m"}', patch_aws='{"MODEL": "claude-m"}')
        _violation(db_session, AUTO, "testuser")
        headers = web_auth_header(sub=test_user.username)
        url = "/dashboard/client-rules/all/patch.bat"

        def script_env() -> dict:
            import base64
            import re
            text = client.get(url, headers=headers).text
            return json.loads(base64.b64decode(re.search(r"Buffer\.from\('([^']+)'", text).group(1)))

        for azure, aws, model in ((False, False, "local-m"), (True, False, "azure-m"), (False, True, "claude-m"), (True, True, "claude-m")):
            test_user.can_use_azure, test_user.can_use_bedrock = azure, aws
            db_session.add(test_user)
            db_session.commit()
            body = self._page(client, test_user)
            assert f"&#34;MODEL&#34;: &#34;{model}&#34;" in body, (azure, aws)
            assert script_env() == {"MODEL": model}

    def test_others_see_nothing(self, client, db_session, test_user):
        _violation(db_session, AUTO, "somebody-else")
        assert 'id="clientNotice' not in self._page(client, test_user)

    def test_notice_clears_after_the_window_or_when_the_check_is_off(self, client, db_session, test_user):
        row = _violation(db_session, AUTO, "testuser")
        row.last_seen_at = _now() - timedelta(days=client_rules.WINDOW_DAYS + 1)
        db_session.add(row)
        db_session.commit()
        assert 'id="clientNotice' not in self._page(client, test_user)
        row.last_seen_at = _now()
        db_session.add(row)
        db_session.commit()
        assert 'id="clientNotice-CC001"' in self._page(client, test_user)
        _configure(db_session, AUTO, enabled=False)
        assert 'id="clientNotice' not in self._page(client, test_user)

    def test_message_is_escaped(self, client, db_session, test_user):
        _configure(db_session, AUTO, title="<script>alert(1)</script>")
        _violation(db_session, AUTO, "testuser")
        assert "<script>alert(1)</script>" not in self._page(client, test_user)

    def test_patch_download_only_for_an_account_with_something_open(self, client, db_session, test_user):
        url = "/dashboard/client-rules/all/patch.bat"
        headers = web_auth_header(sub=test_user.username)
        assert client.get(url, headers=headers).status_code == 404
        _violation(db_session, AUTO, "somebody-else")
        assert client.get(url, headers=headers).status_code == 404
        _violation(db_session, AUTO, "testuser")
        resp = client.get(url, headers=headers)
        assert resp.status_code == 200 and "attachment" in resp.headers["content-disposition"]
        assert b"GW-PATCH-BEGIN" in resp.content and b"\r\n" in resp.content
        linux = client.get("/dashboard/client-rules/all/patch.sh", headers=headers)
        assert linux.status_code == 200 and linux.content.startswith(b"#!/bin/sh\n") and b"\r" not in linux.content
        assert 'filename="claude-code-patch.sh"' in linux.headers["content-disposition"]
        assert b"fixes for CC001" in linux.content
        assert client.get("/dashboard/client-rules/all/patch.exe", headers=headers).status_code == 404

    def test_there_is_no_download_per_check(self, client, db_session, test_user):
        _violation(db_session, AUTO, "testuser")
        headers = web_auth_header(sub=test_user.username)
        for fmt in ("bat", "sh"):
            assert client.get(f"/dashboard/client-rules/CC001/patch.{fmt}", headers=headers).status_code in (404, 405)
        assert "/dashboard/client-rules/CC001/patch" not in self._page(client, test_user)

    def test_no_download_without_a_patch(self, client, db_session, test_user):
        _violation(db_session, NO_CLOUD_ACCESS, "testuser")           # this check has no default patch
        _configure(db_session, NO_CLOUD_ACCESS)
        headers = web_auth_header(sub=test_user.username)
        assert client.get("/dashboard/client-rules/all/patch.bat", headers=headers).status_code == 404
        body = self._page(client, test_user)
        assert f'id="clientNotice-{NO_CLOUD_ACCESS}"' in body and "client-rules/all/patch." not in body

    def test_a_browser_with_nothing_left_to_apply_goes_to_the_dashboard(self, client, db_session, test_user):
        """The mail links to the download; by the time it is clicked the
        account may have fixed everything."""
        headers = {**web_auth_header(sub=test_user.username), "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
        resp = client.get("/dashboard/client-rules/all/patch.bat", headers=headers, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/dashboard"
        _violation(db_session, AUTO, "testuser")
        assert client.get("/dashboard/client-rules/all/patch.bat", headers=headers, follow_redirects=False).status_code == 200


class TestResolved:
    """"I've applied it": the gateway cannot see a settings file, so the
    account says so, and the next violating request says otherwise."""

    _URL = "/dashboard/client-rules/CC001/resolved"

    def _page(self, client, user: User) -> str:
        return client.get("/dashboard", headers=web_auth_header(sub=user.username)).text

    def _json(self, user: User) -> dict:
        return {**web_auth_header(sub=user.username), "Accept": "application/json"}

    def test_the_notice_goes_away_at_once(self, client, db_session, test_user):
        _violation(db_session, AUTO, "testuser")
        assert 'id="clientNotice-CC001"' in self._page(client, test_user)
        resp = client.post(self._URL, headers=self._json(test_user))
        assert resp.status_code == 200 and resp.json() == {"ok": True, "rule_id": "CC001"}
        assert 'id="clientNotice' not in self._page(client, test_user)
        [row] = _violations(db_session, AUTO)
        assert row.resolved_at is not None and client_rules.is_resolved(row)

    def test_a_later_violation_brings_it_back(self, client, db_session, test_user):
        client_rules.refresh()
        _messages(client, safeguards={})
        client.post(self._URL, headers=self._json(test_user))
        assert 'id="clientNotice' not in self._page(client, test_user)
        client_rules.reset()            # the next hour: the write throttle has passed
        client_rules.refresh()
        _messages(client, safeguards={})
        [row] = _violations(db_session, AUTO)
        assert not client_rules.is_resolved(row)
        assert 'id="clientNotice-CC001"' in self._page(client, test_user)

    def test_violating_again_after_saying_fixed_is_a_new_case_for_mail(self, client, db_session, test_user, admin_user, mail_on):
        _configure(db_session, AUTO, auto_email=True)                    # no repeat: one mail per case
        row = _violation(db_session, AUTO, "testuser")
        start = row.last_seen_at
        assert "sent=1" in client_rules.run_auto_mail(db_session, start)
        client_rules.resolve(db_session, AUTO, "testuser", start + timedelta(minutes=5))
        assert "sent=0" in client_rules.run_auto_mail(db_session, start + timedelta(hours=7))
        again = start + timedelta(days=2)
        client_rules._touch_violation(db_session, AUTO, "testuser", "2.1.0", again)
        [row] = _violations(db_session, AUTO)
        assert not client_rules.is_resolved(row) and row.mailed_at is None and row.resolved_at is not None
        assert [v.username for v in client_rules.mail_targets(db_session, AUTO, only_unmailed=True, now=again)] == ["testuser"]
        body = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        card = body.split('id="client-rule-CC001"', 1)[1].split('id="client-rule-CC002"', 1)[0]
        assert "said fixed" in card and "not yet" in card
        assert "sent=1" in client_rules.run_auto_mail(db_session, again)
        assert len(mail_on) == 2

    def test_an_open_violation_seen_again_keeps_its_mail_state(self, db_session, test_user):
        row = _violation(db_session, AUTO, "testuser", mailed_at=_now(), mail_attempted_at=_now())
        client_rules._touch_violation(db_session, AUTO, "testuser", "", row.last_seen_at + timedelta(hours=2))
        assert _violations(db_session, AUTO)[0].mailed_at is not None

    def test_only_that_check_and_only_that_account(self, client, db_session, test_user, admin_user):
        _violation(db_session, AUTO, "testuser")
        _violation(db_session, AUTO, "admin")
        _configure(db_session, OUTDATED)
        _violation(db_session, OUTDATED, "testuser")
        client.post(self._URL, headers=self._json(test_user))
        body = self._page(client, test_user)
        assert 'id="clientNotice-CC001"' not in body and 'id="clientNotice-CC003"' in body
        assert 'id="clientNotice-CC001"' in self._page(client, admin_user)

    def test_nothing_to_mark_is_a_404(self, client, db_session, test_user):
        assert client.post(self._URL, headers=self._json(test_user)).status_code == 404
        _violation(db_session, AUTO, "somebody-else")
        assert client.post(self._URL, headers=self._json(test_user)).status_code == 404
        assert not client_rules.is_resolved(_violations(db_session, AUTO)[0])

    def test_without_javascript_it_goes_back_to_the_dashboard(self, client, db_session, test_user):
        _violation(db_session, AUTO, "testuser")
        resp = client.post(self._URL, headers=web_auth_header(sub=test_user.username), follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/dashboard"

    def test_an_account_that_says_fixed_is_not_mailed(self, db_session, test_user, admin_user, mail_on):
        _configure(db_session, AUTO, auto_email=True)
        _violation(db_session, AUTO, "testuser")
        _violation(db_session, AUTO, "admin")
        assert client_rules.resolve(db_session, AUTO, "testuser")
        assert "sent=1" in client_rules.run_auto_mail(db_session)
        assert [to for to, _, _ in mail_on] == ["admin@example.test"]
        assert client_rules.send_rule_mail(AUTO, only_unmailed=False) == {"sent": 1, "failed": 0}
        assert [to for to, _, _ in mail_on] == ["admin@example.test"] * 2

    def test_admin_sees_who_says_fixed_and_counts_only_the_open_ones(self, client, db_session, admin_user, test_user):
        _violation(db_session, AUTO, "testuser")
        _violation(db_session, AUTO, "admin")
        client_rules.resolve(db_session, AUTO, "testuser")
        body = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        card = body.split('id="client-rule-CC001"', 1)[1].split('id="client-rule-CC002"', 1)[0]
        assert "1 account" in card and "1 say fixed" in card and "says fixed" in card
        strip = body.split('id="clientRuleAttention"', 1)[1][:1500]
        assert "CC001" in strip and "· 1 account<" in strip

    def test_a_check_everyone_reports_fixed_needs_no_attention(self, client, db_session, admin_user, test_user):
        _violation(db_session, AUTO, "testuser")
        client_rules.resolve(db_session, AUTO, "testuser")
        body = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        assert 'id="clientRuleAttention"' not in body


class TestAdminSettings:
    def test_defaults_come_from_code_until_an_admin_saves(self, client, db_session, admin_user, test_user):
        rule = client_rules.get_rule(db_session, AUTO)
        assert not rule.customized and rule.enabled and rule.title == client_checks.BY_ID[AUTO].title
        assert json.loads(rule.patch) == _ENV
        assert db_session.exec(select(ClientRuleSetting)).all() == []

        form = {**_FORM, "patch_aws": "CLAUDE_CODE_AUTO_MODE_MODEL=claude-m", "auto_email": "on", "email_repeat_days": "7"}
        resp = client.post(f"/admin/client-rules/{AUTO}", data=form, headers=_admin(admin_user))
        assert resp.status_code == 200 and resp.json() == {"ok": True, "id": "CC001"}
        db_session.expire_all()
        rule = client_rules.get_rule(db_session, AUTO)
        assert rule.customized and rule.updated_by == "admin" and rule.title == "Fix auto mode"
        assert (rule.auto_email, rule.email_repeat_days) == (True, 7)
        assert json.loads(rule.patch_aws) == {"CLAUDE_CODE_AUTO_MODE_MODEL": "claude-m"}

        _messages(client, safeguards={})          # still live in this worker
        assert _violators(db_session, AUTO) == ["testuser"]

    def test_turning_a_check_on_takes_effect_at_once(self, client, db_session, admin_user, test_user):
        form = {"enabled": "on", "action": "notice", "title": "Use https", "patch": "ANTHROPIC_BASE_URL=https://gw.example.test"}
        assert client.post(f"/admin/client-rules/{HTTP}", data=form, headers=_admin(admin_user)).status_code == 200
        _messages(client, url="http://testserver/v1/messages")
        assert _violators(db_session, HTTP) == ["testuser"]

    def test_reset_goes_back_to_the_code_and_keeps_the_violations(self, client, db_session, admin_user):
        _configure(db_session, AUTO, title="Changed")
        _violation(db_session, AUTO, "testuser")
        assert client.post(f"/admin/client-rules/{AUTO}/reset", headers=_admin(admin_user)).status_code == 200
        db_session.expire_all()
        rule = client_rules.get_rule(db_session, AUTO)
        assert not rule.customized and rule.title == client_checks.BY_ID[AUTO].title
        assert _violators(db_session, AUTO) == ["testuser"]

    def test_what_a_check_detects_is_not_in_the_form(self, client, db_session, admin_user, test_user):
        """Posting condition-like fields changes nothing: detection is code."""
        form = {**_FORM, "body_field": "anything", "plain_http": "on", "param": "9.9.9", "version_below": "9.9.9"}
        assert client.post(f"/admin/client-rules/{AUTO}", data=form, headers=_admin(admin_user)).status_code == 200
        assert client.post(f"/admin/client-rules/{OUTDATED}", data=form, headers=_admin(admin_user)).status_code == 200
        _messages(client, url="http://testserver/v1/messages", ua="claude-cli/9.0.0 (external, cli)")
        assert _violators(db_session, AUTO) == [] and _violators(db_session, OUTDATED) == []

    @pytest.mark.parametrize("rule_id, change, message", [
        (AUTO, {"patch": "{broken"}, "not valid JSON"),
        (AUTO, {"patch": "permissions.allow=x"}, "not an environment variable"),
        (AUTO, {"patch_aws": "just words"}, "NAME=value"),
        (AUTO, {"action": "block"}, "Action"),
        (AUTO, {"title": ""}, "title"),
        (AUTO, {"email_repeat_days": "999"}, "between 0 and 365"),
        (AUTO, {"patch": ""}, "needs a settings patch"),
        (AUTO, {"patch": "", "patch_aws": "MODEL=claude-m"}, "Everyone box"),        # local-only accounts would get nothing
        (HTTP, {"patch": "   "}, "needs a settings patch"),
        (UNKNOWN_MODEL, {"patch": "# nothing yet"}, "needs a settings patch"),
    ])
    def test_bad_input_is_refused(self, client, db_session, admin_user, rule_id, change, message):
        resp = client.post(f"/admin/client-rules/{rule_id}", data={**_FORM, **change}, headers=_admin(admin_user))
        assert resp.status_code == 400 and message in resp.json()["detail"]
        assert db_session.exec(select(ClientRuleSetting)).all() == []

    def test_a_check_cannot_be_turned_on_without_its_patch(self, client, db_session, admin_user, test_user):
        """CC001, CC002 and CC004 are fixed by a setting: no patch, nothing to tell people."""
        assert [c.id for c in client_checks.CHECKS if c.needs_patch] == [AUTO, HTTP, UNKNOWN_MODEL]
        url, headers = f"/admin/client-rules/{HTTP}", _admin(admin_user)
        empty = {"enabled": "on", "action": "notice", "title": "Use https", "patch": ""}
        resp = client.post(url, data=empty, headers=headers)
        assert resp.status_code == 400 and "CC002" in resp.json()["detail"]
        client_rules.refresh()
        _messages(client, url="http://testserver/v1/messages")
        assert _violators(db_session, HTTP) == []                               # still off
        # Saving it switched off, to fill the patch in later, is fine ...
        assert client.post(url, data={**empty, "enabled": "off"}, headers=headers).status_code == 200
        # ... and so is turning it on once the patch is there.
        assert client.post(url, data={**empty, "patch": "ANTHROPIC_BASE_URL=https://gw.example.test"}, headers=headers).status_code == 200

    def test_checks_without_a_setting_to_change_need_no_patch(self, client, admin_user):
        for rule_id in (OUTDATED, NO_CLOUD_ACCESS):
            form = {"enabled": "on", "action": "notice", "title": "t", "patch": ""}
            assert client.post(f"/admin/client-rules/{rule_id}", data=form, headers=_admin(admin_user)).status_code == 200, rule_id

    def test_a_test_mail_of_an_unfinished_form_is_still_possible(self, client, admin_user, mail_on):
        form = {"enabled": "on", "action": "notice", "title": "Use https", "patch": ""}
        assert client.post(f"/admin/client-rules/{HTTP}/mail-test", data=form, headers=_admin(admin_user)).json()["ok"]

    def test_an_older_row_without_a_patch_is_flagged(self, client, db_session, admin_user):
        _configure(db_session, HTTP, patch="")                                  # as an older row could be
        assert client_rules.get_rule(db_session, HTTP).missing_patch
        body = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        card = body.split('id="client-rule-CC002"', 1)[1].split('id="client-rule-CC003"', 1)[0]
        assert ">no patch<" in card and "data-needs-patch" in card and "required to turn this rule on" in card
        assert "data-needs-patch" not in body.split('id="client-rule-CC003"', 1)[1].split('id="client-rule-CC004"', 1)[0]

    def test_unknown_ids_are_404(self, client, admin_user):
        for url in ("/admin/client-rules/CC999", "/admin/client-rules/CC999/mail", "/admin/client-rules/CC999/reset"):
            assert client.post(url, data=_FORM, headers=_admin(admin_user)).status_code == 404

    def test_there_is_no_way_to_add_or_delete_a_check(self, client, admin_user):
        assert client.post("/admin/client-rules", data=_FORM, headers=_admin(admin_user)).status_code in (404, 405)
        assert client.post(f"/admin/client-rules/{AUTO}/delete", headers=_admin(admin_user)).status_code in (404, 405)

    def test_only_admins(self, client, test_user):
        headers = {**web_auth_header(sub=test_user.username), "Accept": "application/json"}
        assert client.post(f"/admin/client-rules/{AUTO}", data=_FORM, headers=headers).status_code in (401, 403)

    def test_admin_page_lists_every_check_with_its_id(self, client, db_session, admin_user, test_user):
        _violation(db_session, AUTO, "testuser", client_version="2.1.280")
        db_session.add(ClientVersion(username="testuser", client="claude-code", version="2.1.280", first_seen_at=_now(), last_seen_at=_now()))
        db_session.commit()
        body = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        assert 'id="client-rules"' in body
        for check in client_checks.ACTIVE:
            assert f'id="client-rule-{check.id}"' in body and check.name in body
        assert 'id="client-versions"' in body and "2.1.280" in body
        assert 'id="clientRuleAttention"' in body and "1 account" in body
        assert "?q=testuser#users" in body
        assert 'name="param"' not in body                                  # thresholds are not editable
        assert client_checks.MIN_CLAUDE_CODE_VERSION in body                 # but the admin can see them

    def test_admin_can_preview_each_audiences_script(self, client, db_session, admin_user):
        _configure(db_session, AUTO, patch="", patch_aws='{"MODEL": "claude-m"}')
        headers = web_auth_header(sub=admin_user.username, scopes=["admin"])
        url = f"/admin/client-rules/{AUTO}/patch.bat"
        assert client.get(url, params={"audience": "aws"}, headers=headers).status_code == 200
        assert client.get(url, params={"audience": "local"}, headers=headers).status_code == 404   # nothing for them
        assert client.get(url, params={"audience": "everyone"}, headers=headers).status_code == 400
        linux = client.get(url.replace(".bat", ".sh"), params={"audience": "aws"}, headers=headers)
        assert linux.status_code == 200 and linux.content.startswith(b"#!/bin/sh") and b"claude-code-patch-CC001-aws.sh" in linux.content
        assert client.get(url.replace(".bat", ".zip"), params={"audience": "aws"}, headers=headers).status_code == 404

    def test_variants_are_saved_and_shown_back_as_lines(self, client, db_session, admin_user):
        form = {**_FORM, "patch": "SERVER=0\n-OLD", "patch_azure": '{"MODEL": "azure-m"}', "patch_aws": "MODEL=claude-m"}
        assert client.post(f"/admin/client-rules/{AUTO}", data=form, headers=_admin(admin_user)).status_code == 200
        row = db_session.get(ClientRuleSetting, AUTO)
        assert json.loads(row.patch) == {"SERVER": "0", "OLD": None}
        assert json.loads(row.patch_azure) == {"MODEL": "azure-m"} and json.loads(row.patch_aws) == {"MODEL": "claude-m"}
        body = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        assert "SERVER=0\n-OLD</textarea>" in body and "MODEL=azure-m</textarea>" in body


class TestMailer:
    def test_off_until_every_setting_is_present(self, monkeypatch, mail_on):
        assert mailer.is_enabled()
        monkeypatch.delenv("EXCHANGE_PASSWORD")
        assert not mailer.is_enabled() and mailer.status()["missing"] == ["EXCHANGE_PASSWORD"]
        assert mailer.send("a@example.test", "s", "b") == (False, "Mail is not configured.")

    def test_addresses(self, mail_on):
        assert mailer.address_for("kane.beh") == "kane.beh@example.test"
        assert mailer.in_company_domain("kane.beh@EXAMPLE.test")
        for other in ("x@evil.test", "x@example.test.evil", "x", "a b@example.test", "@example.test"):
            assert not mailer.in_company_domain(other)

    def test_a_failure_is_a_summary_never_an_exception(self, mail_on):
        with patch.object(mailer, "_send_sync", side_effect=ConnectionError("no route\nsecret detail")):
            ok, error = mailer.send("a@example.test", "s", "b")
        assert not ok and error == "ConnectionError: no route"

    def test_status_never_carries_the_password(self, mail_on):
        assert "pw" not in json.dumps(mailer.status())


class TestMail:
    _TEST = f"/admin/client-rules/{AUTO}/mail-test"
    _SEND = f"/admin/client-rules/{AUTO}/mail"

    def test_test_mail_goes_to_the_admin_by_default(self, client, admin_user, mail_on):
        resp = client.post(self._TEST, data=_FORM, headers=_admin(admin_user))
        assert resp.status_code == 200 and resp.json() == {"ok": True, "to": "admin@example.test", "detail": ""}
        [(to, subject, body)] = mail_on
        assert to == "admin@example.test" and subject.startswith("[TEST] ") and "Fix auto mode" in body
        assert "https://gw.example.test/dashboard" in body and "CLAUDE_CODE_AUTO_MODE_SERVER" in body
        assert "Reference: CC001" in body

    def test_test_mail_uses_the_form_as_it_stands_not_what_is_saved(self, client, db_session, admin_user, mail_on):
        client.post(self._TEST, data={**_FORM, "title": "Unsaved wording"}, headers=_admin(admin_user))
        assert "Unsaved wording" in mail_on[-1][2]
        assert db_session.exec(select(ClientRuleSetting)).all() == []

    def test_test_mail_address_and_audience_can_be_chosen(self, client, admin_user, mail_on):
        form = {**_FORM, "patch": "MODEL=local-m", "patch_aws": "MODEL=claude-m", "to": "someone@example.test"}
        for audience, model in (("local", "local-m"), ("azure", "local-m"), ("aws", "claude-m")):
            resp = client.post(self._TEST, data={**form, "audience": audience}, headers=_admin(admin_user))
            assert resp.json()["ok"] and resp.json()["to"] == "someone@example.test"
            assert mail_on[-1][0] == "someone@example.test" and model in mail_on[-1][2]

    def test_the_combined_test_mail_lists_every_rule_that_is_on_with_one_merged_patch(self, client, db_session, admin_user, mail_on):
        _configure(db_session, AUTO, title="Fix auto mode", patch='{"SERVER": "0", "MODEL": "local-m"}', patch_aws='{"MODEL": "claude-m"}')
        _configure(db_session, HTTP, title="Use https", patch='{"ANTHROPIC_BASE_URL": "https://gw.example.test"}')
        _configure(db_session, OUTDATED, title="Update Claude Code")
        _configure(db_session, UNKNOWN_MODEL, enabled=False, title="Off, so not in it")
        url = "/admin/client-rules/mail-test-all"
        resp = client.post(url, data={"to": "someone@example.test", "audience": "aws"}, headers=_admin(admin_user))
        assert resp.json() == {"ok": True, "to": "someone@example.test", "detail": "", "rules": ["CC001", "CC002", "CC003"]}
        [(to, subject, body)] = mail_on
        assert to == "someone@example.test" and subject.startswith("[TEST] ") and "3 things to change" in subject
        for title in ("1. Fix auto mode", "2. Use https", "3. Update Claude Code"):
            assert title in body
        assert "Off, so not in it" not in body
        assert body.count("<pre") == 1 and "claude-m" in body and "local-m" not in body      # one block, the AWS variant
        assert "ANTHROPIC_BASE_URL" in body and "One script fixes 2 of them" in body and "CC003 needs something else" in body
        assert client.post(url, headers=_admin(admin_user)).json()["to"] == "admin@example.test"   # the admin's own by default
        assert "local-m" in mail_on[1][2]
        db_session.expire_all()
        log = db_session.exec(select(ClientMailLog).order_by(ClientMailLog.id)).all()
        assert [(row.kind, row.rule_ids, row.actor) for row in log] == [("test", "CC001,CC002,CC003", "admin")] * 2
        assert _violations(db_session, AUTO) == []                    # a test mail touches nobody's violations

    def test_the_combined_test_mail_is_refused_when_it_cannot_be_sent(self, client, db_session, admin_user, test_user, mail_on):
        url = "/admin/client-rules/mail-test-all"
        assert client.post(url, data={"to": "x@elsewhere.test"}, headers=_admin(admin_user)).status_code == 400
        assert client.post(url, data={"audience": "everyone"}, headers=_admin(admin_user)).status_code == 400
        assert client.post(url, headers={**web_auth_header(sub=test_user.username), "Accept": "application/json"}).status_code == 403
        _configure(db_session, AUTO, enabled=False)
        resp = client.post(url, headers=_admin(admin_user))
        assert resp.status_code == 400 and "No rule is on" in resp.json()["detail"]
        assert mail_on == []

    def test_the_admin_page_offers_the_combined_test_mail(self, client, admin_user, mail_on):
        body = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        assert 'id="client-rules-mail-test-all"' in body and 'data-cr-test="/admin/client-rules/mail-test-all"' in body

    def test_test_mail_stays_inside_the_company_domain(self, client, admin_user, mail_on):
        resp = client.post(self._TEST, data={**_FORM, "to": "x@elsewhere.test"}, headers=_admin(admin_user))
        assert resp.status_code == 400 and mail_on == []

    def test_test_mail_reports_a_failure(self, client, admin_user, mail_on):
        with patch.object(mailer, "_send_sync", side_effect=TimeoutError("timed out")):
            resp = client.post(self._TEST, data=_FORM, headers=_admin(admin_user))
        assert resp.json()["ok"] is False and resp.json()["detail"] == "TimeoutError: timed out"

    def test_mail_is_refused_while_unconfigured(self, client, db_session, admin_user):
        assert client.post(self._TEST, data=_FORM, headers=_admin(admin_user)).status_code == 400
        assert client.post(self._SEND, headers=_admin(admin_user)).status_code == 400

    def test_manual_send_reaches_violating_accounts_once(self, client, db_session, admin_user, test_user, mail_on):
        db_session.add(User(username="app_batch", password_hash="x", api_key="sk-appkey", daily_limit_usd=1.0))
        db_session.commit()
        for name in ("testuser", "admin", "app_batch", "deleted-account"):
            _violation(db_session, AUTO, name)
        _violation(db_session, HTTP, "testuser")               # another check's violation is not this mail's business
        resp = client.post(self._SEND, data={"only_unmailed": "on"}, headers=_admin(admin_user))
        assert resp.json()["queued"] == 2                     # no app accounts, no accounts that are gone
        assert sorted(to for to, _, _ in mail_on) == ["admin@example.test", "testuser@example.test"]
        assert all(v.mailed_at for v in _violations(db_session, AUTO) if v.username in ("testuser", "admin"))
        assert _violations(db_session, HTTP)[0].mailed_at is None

        assert client.post(self._SEND, data={"only_unmailed": "on"}, headers=_admin(admin_user)).json()["queued"] == 0
        assert len(mail_on) == 2
        assert client.post(self._SEND, data={"only_unmailed": "off"}, headers=_admin(admin_user)).json()["queued"] == 2
        assert len(mail_on) == 4

    def test_each_recipient_gets_the_patch_for_their_access(self, db_session, test_user, admin_user, mail_on):
        test_user.can_use_azure = test_user.can_use_bedrock = False
        db_session.add(test_user)
        db_session.commit()
        _configure(db_session, AUTO, patch='{"MODEL": "local-m"}', patch_aws='{"MODEL": "claude-m"}')
        _violation(db_session, AUTO, "testuser")     # local-only
        _violation(db_session, AUTO, "admin")        # admins have every backend
        assert client_rules.send_rule_mail(AUTO) == {"sent": 2, "failed": 0}
        bodies = {to: body for to, _, body in mail_on}
        assert "local-m" in bodies["testuser@example.test"] and "claude-m" not in bodies["testuser@example.test"]
        assert "claude-m" in bodies["admin@example.test"]

    def test_a_failed_send_is_recorded_on_the_account(self, db_session, test_user, mail_on):
        _violation(db_session, AUTO, "testuser")
        with patch.object(mailer, "_send_sync", side_effect=TimeoutError("timed out")):
            assert client_rules.send_rule_mail(AUTO) == {"sent": 0, "failed": 1}
        [row] = _violations(db_session, AUTO)
        assert row.mailed_at is None and row.mail_error == "TimeoutError: timed out" and row.mail_attempted_at

    def test_the_mail_names_the_platforms_people_here_use(self, mail_on):
        draft = SimpleNamespace(id="CC001", title="t", message="", email_subject="")
        body = client_rules.render_mail(draft, "u", {"A": "1"})[1]
        assert "Windows (.bat)" in body and "Linux (.sh)" in body and "macOS" not in body

    def test_every_mail_says_it_is_automated_and_not_to_reply(self, db_session, test_user, mail_on):
        _configure(db_session, AUTO, auto_email=True)
        _configure(db_session, OUTDATED)
        _violation(db_session, AUTO, "testuser")
        client_rules.run_auto_mail(db_session)                       # one check
        _violation(db_session, OUTDATED, "testuser")
        client_rules.send_rule_mail(OUTDATED, only_unmailed=False)   # two checks, by an admin's click
        draft = SimpleNamespace(id="CC001", title="t", message="", email_subject="")
        bodies = [body for _, _, body in mail_on] + [client_rules.render_mail(draft, "u", {})[1]]    # and a test mail
        assert len(bodies) == 3
        for body in bodies:
            assert "This is an automated message" in body and "Please do not reply" in body

    def test_the_mail_points_at_the_support_link_when_there_is_one(self, mail_on):
        draft = SimpleNamespace(id="CC001", title="t", message="", email_subject="")
        links = {"support_bot_url": "", "install_guide_url": ""}
        with patch.object(client_rules.config, "get_site_links", return_value=links):
            assert "For help" not in client_rules.render_mail(draft, "u", {})[1]
            links["support_bot_url"] = "https://chat.example.test/bot?a=1&b=2"
            assert 'href="https://chat.example.test/bot?a=1&amp;b=2"' in client_rules.render_mail(draft, "u", {})[1]
            links["support_bot_url"] = "/help"                       # site-relative: under PUBLIC_BASE_URL
            assert 'href="https://gw.example.test/help"' in client_rules.render_mail(draft, "u", {})[1]
            links["support_bot_url"] = "javascript:alert(1)"
            assert "For help" not in client_rules.render_mail(draft, "u", {})[1]
        with patch.object(client_rules.config, "get_site_links", side_effect=RuntimeError("config")):
            assert "Please do not reply" in client_rules.render_mail(draft, "u", {})[1]

    def test_mail_escapes_what_the_admin_wrote(self, mail_on):
        draft = SimpleNamespace(id="CC001", title="<b>t</b>", message="a\n<i>b</i>", email_subject="")
        subject, body = client_rules.render_mail(draft, "u<1>", {"A": "<x>"})
        assert "<b>t</b>" not in body and "<i>b</i>" not in body and "<x>" not in body and "u<1>" not in body
        assert "a<br>" in body and subject.endswith("<b>t</b>")


class TestOneMailPerAccount:
    """An account with several open checks is told once, about all of them,
    with one patch that covers them."""

    def _three(self, db_session):
        _configure(db_session, AUTO, auto_email=True, title="Fix auto mode", patch='{"SERVER": "0", "MODEL": "local-m"}',
                   patch_aws='{"MODEL": "claude-m"}')
        _configure(db_session, HTTP, title="Use https", patch='{"ANTHROPIC_BASE_URL": "https://gw.example.test"}')
        _configure(db_session, OUTDATED, title="Update Claude Code")
        for rule_id in (AUTO, HTTP, OUTDATED):
            _violation(db_session, rule_id, "testuser")

    def test_several_open_checks_make_one_mail(self, db_session, test_user, mail_on):
        self._three(db_session)
        assert "sent=1" in client_rules.run_auto_mail(db_session)
        [(to, subject, body)] = mail_on
        assert to == "testuser@example.test" and "3 things to change in your Claude Code setup" in subject
        for title in ("1. Fix auto mode", "2. Use https", "3. Update Claude Code"):
            assert title in body
        assert body.count("<pre") == 1                                   # one block, not one per check
        for name in ("SERVER", "MODEL", "ANTHROPIC_BASE_URL"):
            assert name in body
        assert "claude-m" in body and "local-m" not in body              # the test user has AWS access
        assert "Reference: CC001, CC002, CC003." in body

    def test_the_mail_links_straight_to_the_one_patch(self, db_session, test_user, mail_on, monkeypatch):
        self._three(db_session)
        client_rules.run_auto_mail(db_session)
        body = mail_on[0][2]
        for fmt in ("bat", "sh"):
            assert body.count(f'href="https://gw.example.test/dashboard/client-rules/all/patch.{fmt}"') == 1
        assert "client-rules/CC00" not in body                           # never a download per check
        assert 'href="https://gw.example.test/dashboard"' in body
        monkeypatch.delenv("PUBLIC_BASE_URL")                            # no address to link to: the text alone
        draft = SimpleNamespace(id="CC001", title="t", message="", email_subject="")
        plain = client_rules.render_mail(draft, "u", {"A": "1"})[1]
        assert "href=" not in plain and "<pre" in plain
        assert "href=" not in client_rules.render_mail(draft, "u", {})[1]

    def test_the_mail_says_what_the_script_does_not_fix(self, db_session, test_user, mail_on):
        self._three(db_session)                                          # CC003 (update Claude Code) has no patch
        client_rules.run_auto_mail(db_session)
        body = mail_on[0][2]
        assert "One script fixes 2 of them" in body and "CC003 needs something else" in body
        assert "fixes them all" not in body
        client_rules.resolve(db_session, OUTDATED, "testuser")
        client_rules.send_rule_mail(AUTO, only_unmailed=False)
        assert "One script fixes them all" in mail_on[1][2] and "something else" not in mail_on[1][2]

    def test_a_mail_about_nothing_a_script_can_fix_offers_no_script(self, db_session, test_user, mail_on):
        _configure(db_session, OUTDATED, title="Update Claude Code")
        _configure(db_session, NO_CLOUD_ACCESS, title="Ask for access")
        _violation(db_session, OUTDATED, "testuser")
        _violation(db_session, NO_CLOUD_ACCESS, "testuser")
        client_rules.send_rule_mail(OUTDATED)
        body = mail_on[0][2]
        assert "all/patch" not in body and "One script" not in body and "<pre" not in body
        assert 'href="https://gw.example.test/dashboard"' in body

    def test_the_mail_covers_checks_without_automatic_mail_and_marks_them_all(self, db_session, test_user, mail_on):
        self._three(db_session)                                          # only CC001 has automatic mail
        client_rules.run_auto_mail(db_session)
        for rule_id in (AUTO, HTTP, OUTDATED):
            assert _violations(db_session, rule_id)[0].mailed_at is not None, rule_id
        client_rules.run_auto_mail(db_session, _now() + timedelta(days=3))
        assert len(mail_on) == 1                                         # nothing new to say

    def test_one_open_check_keeps_its_own_subject(self, db_session, test_user, mail_on):
        _configure(db_session, AUTO, auto_email=True, title="Fix auto mode", email_subject="Please fix auto mode")
        _violation(db_session, AUTO, "testuser")
        client_rules.run_auto_mail(db_session)
        [(_, subject, body)] = mail_on
        assert subject == "Please fix auto mode" and "1. " not in body and "Reference: CC001." in body

    def test_a_new_violation_later_sends_the_whole_list_again(self, db_session, test_user, mail_on):
        _configure(db_session, AUTO, auto_email=True, title="Fix auto mode")
        _configure(db_session, HTTP, auto_email=True, title="Use https")
        _violation(db_session, AUTO, "testuser")
        now = _now()
        client_rules.run_auto_mail(db_session, now)
        _violation(db_session, HTTP, "testuser")
        client_rules.run_auto_mail(db_session, now + timedelta(hours=1))
        assert len(mail_on) == 2
        assert "Fix auto mode" in mail_on[1][2] and "Use https" in mail_on[1][2]

    def test_what_is_fixed_or_deprecated_is_left_out(self, db_session, test_user, mail_on):
        self._three(db_session)
        client_rules.resolve(db_session, HTTP, "testuser")
        with TestLifecycle._with(deprecated="on its way out"):          # CC001, the one with automatic mail
            assert "sent=0" in client_rules.run_auto_mail(db_session)   # nothing left that asks for mail
            assert client_rules.send_rule_mail(OUTDATED) == {"sent": 1, "failed": 0}
        [(_, _, body)] = mail_on
        assert "Update Claude Code" in body and "Use https" not in body and "Fix auto mode" not in body

    def test_a_manual_send_from_one_check_is_the_same_mail(self, client, db_session, admin_user, test_user, mail_on):
        self._three(db_session)
        _violation(db_session, OUTDATED, "admin")                        # admin has only this one open
        resp = client.post(f"/admin/client-rules/{OUTDATED}/mail", data={"only_unmailed": "on"}, headers=_admin(admin_user))
        assert resp.json()["queued"] == 2
        bodies = {to: (subject, body) for to, subject, body in mail_on}
        assert "3 things to change" in bodies["testuser@example.test"][0]
        assert "Fix auto mode" in bodies["testuser@example.test"][1]
        assert "Update Claude Code" in bodies["admin@example.test"][0] and "Fix auto mode" not in bodies["admin@example.test"][1]
        assert _violations(db_session, AUTO)[0].mailed_at is not None    # covered by that mail, so marked

    def test_a_click_on_one_check_queues_the_account_in_every_list(self, db_session, test_user, mail_on):
        self._three(db_session)                  # the mail it asks for covers all three
        assert client_rules.request_mail(db_session, OUTDATED) == 1
        for rule_id in (AUTO, HTTP, OUTDATED):
            [row] = _violations(db_session, rule_id)
            assert client_rules.is_queued(row), rule_id
        client_rules.send_batch(db_session.get_bind())
        for rule_id in (AUTO, HTTP, OUTDATED):
            [row] = _violations(db_session, rule_id)
            assert row.mailed_at is not None and not client_rules.is_queued(row), rule_id
        assert len(mail_on) == 1

    def test_the_other_lists_show_it_queued_then_mailed(self, client, db_session, admin_user, test_user, mail_on):
        self._three(db_session)
        client_rules.request_mail(db_session, OUTDATED)
        def page() -> str:
            return client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text

        card = page().split('id="client-rule-CC001"', 1)[1].split('id="client-rule-CC002"', 1)[0]
        assert "queued" in card and "not yet" not in card
        client_rules.send_batch(db_session.get_bind())
        card = page().split('id="client-rule-CC001"', 1)[1].split('id="client-rule-CC002"', 1)[0]
        assert "queued" not in card and "not yet" not in card and "Mail the 0 not mailed yet" in card

    def test_mail_everyone_reaches_every_check_at_once(self, client, db_session, admin_user, test_user, mail_on):
        _configure(db_session, AUTO, title="Fix auto mode")
        _configure(db_session, HTTP, title="Use https")
        _violation(db_session, AUTO, "testuser")
        _violation(db_session, HTTP, "admin")
        _violation(db_session, HTTP, "app_bot")                          # no mailbox
        _violation(db_session, HTTP, "gone")                             # no account
        body = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        strip = body.split('id="client-rules-mail-all"', 1)[1][:1800]
        assert "Mail the 2 not mailed yet" in strip and "Mail all 2" in strip
        resp = client.post("/admin/client-rules/mail-all", data={"only_unmailed": "on"}, headers=_admin(admin_user))
        assert resp.status_code == 200 and resp.json()["queued"] == 2
        assert sorted(to for to, _, _ in mail_on) == ["admin@example.test", "testuser@example.test"]
        # Everyone has been told: "not mailed yet" has nobody left, "all" still has both.
        assert client_rules.request_mail_all(db_session, only_unmailed=True) == 0
        resp = client.post("/admin/client-rules/mail-all", data={"only_unmailed": "off"}, headers=_admin(admin_user))
        assert resp.json()["queued"] == 2 and len(mail_on) == 4

    def test_mail_everyone_skips_fixed_and_deprecated(self, db_session, test_user, admin_user, mail_on):
        _configure(db_session, AUTO)
        _configure(db_session, HTTP)
        _violation(db_session, AUTO, "testuser")
        _violation(db_session, HTTP, "admin")
        client_rules.resolve(db_session, HTTP, "admin")
        assert client_rules.mail_all_targets(db_session, only_unmailed=False) == ["testuser"]
        with TestLifecycle._with(deprecated="on its way out"):          # CC001
            assert client_rules.mail_all_targets(db_session, only_unmailed=False) == []

    def test_mail_everyone_needs_mail_configured(self, client, db_session, admin_user):
        resp = client.post("/admin/client-rules/mail-all", data={"only_unmailed": "on"}, headers=_admin(admin_user))
        assert resp.status_code == 400

    def test_a_later_check_wins_where_two_set_the_same_variable(self):
        first = SimpleNamespace(patch='{"MODEL": "a", "KEEP": "1"}', patch_azure="", patch_aws="")
        second = SimpleNamespace(patch='{"MODEL": "b"}', patch_azure="", patch_aws='{"MODEL": "c", "KEEP": null}')
        assert client_rules.combined_env([first, second], False, False) == {"MODEL": "b", "KEEP": "1"}
        assert client_rules.combined_env([first, second], False, True) == {"MODEL": "c", "KEEP": None}

    def test_the_admin_is_told_when_two_checks_disagree(self, client, db_session, admin_user):
        headers = web_auth_header(sub=admin_user.username, scopes=["admin"])
        _configure(db_session, AUTO, patch='{"MODEL": "a", "SAME": "1"}')
        _configure(db_session, HTTP, patch='{"SAME": "1"}', patch_aws='{"MODEL": "b"}')
        [conflict] = client_rules.env_conflicts(client_rules.rules(db_session))
        assert (conflict["name"], conflict["winner"], conflict["audiences"]) == ("MODEL", HTTP, ["aws"])
        body = client.get("/admin", headers=headers).text
        strip = body.split('id="client-rules-conflicts"', 1)[1][:2000]
        assert "MODEL" in strip and "CC001=a" in strip and "CC002=b" in strip and "CC002 wins" in strip
        _configure(db_session, HTTP, enabled=False, patch='{"SAME": "1"}', patch_aws='{"MODEL": "b"}')
        assert client_rules.env_conflicts(client_rules.rules(db_session)) == []      # a check that is off sets nothing
        assert 'id="client-rules-conflicts"' not in client.get("/admin", headers=headers).text


class TestEverythingAtOnce:
    """The dashboard's one patch for everything the account has open, and
    one button for all its notices."""

    def _two(self, db_session):
        _configure(db_session, AUTO, patch='{"SERVER": "0"}')
        _configure(db_session, HTTP, patch='{"ANTHROPIC_BASE_URL": "https://gw.example.test"}')
        _violation(db_session, AUTO, "testuser")
        _violation(db_session, HTTP, "testuser")

    def _page(self, client, user: User) -> str:
        return client.get("/dashboard", headers=web_auth_header(sub=user.username)).text

    def _script_env(self, client, user: User, fmt: str) -> dict:
        import base64
        import re
        text = client.get(f"/dashboard/client-rules/all/patch.{fmt}", headers=web_auth_header(sub=user.username)).text
        return json.loads(base64.b64decode(re.search(r"Buffer\.from\('([^']+)'", text).group(1)))

    def test_two_notices_offer_one_patch_for_both(self, client, db_session, test_user):
        self._two(db_session)
        body = self._page(client, test_user)
        assert 'id="clientNoticesAll"' in body
        assert "/dashboard/client-rules/all/patch.bat" in body and "/dashboard/client-rules/all/patch.sh" in body
        want = {"SERVER": "0", "ANTHROPIC_BASE_URL": "https://gw.example.test"}
        assert self._script_env(client, test_user, "bat") == want and self._script_env(client, test_user, "sh") == want

    def test_one_notice_gets_the_same_single_download(self, client, db_session, test_user):
        _configure(db_session, AUTO, patch='{"SERVER": "0"}')
        _violation(db_session, AUTO, "testuser")
        body = self._page(client, test_user)
        assert 'id="clientNoticesAll"' in body and "all/resolved" not in body      # its own button is enough
        assert self._script_env(client, test_user, "bat") == {"SERVER": "0"}

    def test_several_notices_still_offer_one_download(self, client, db_session, test_user):
        self._two(db_session)
        body = self._page(client, test_user)
        assert body.count("/dashboard/client-rules/all/patch.bat") == 1
        assert body.count("/dashboard/client-rules/all/patch.sh") == 1
        assert "client-rules/CC001/patch" not in body and "client-rules/CC002/patch" not in body
        assert "Fix everything with one script" in body

    def test_the_panel_says_what_the_script_does_not_cover(self, client, db_session, test_user):
        self._two(db_session)
        _configure(db_session, OUTDATED)                              # fixed by updating, not by a setting
        _violation(db_session, OUTDATED, "testuser")
        combined = client_rules.combined_for(db_session, test_user)
        assert (combined["count"], combined["covered"], combined["manual"]) == (3, ["CC001", "CC002"], ["CC003"])
        body = self._page(client, test_user)
        assert "One script fixes 2 of these 3" in body and "Fix everything with one script" not in body
        note = body.split("data-client-manual", 1)[1][:300]
        assert "CC003" in note and "CC001" not in note
        assert b"fixes for CC001, CC002" in client.get(
            "/dashboard/client-rules/all/patch.sh", headers=web_auth_header(sub=test_user.username)).content

    def test_the_panel_alone_follows_what_is_still_open(self, client, db_session, test_user):
        """What the page swaps in after "I've applied it"."""
        self._two(db_session)
        headers = web_auth_header(sub=test_user.username)
        panel = client.get("/dashboard/client-notices", headers=headers).text
        assert panel.lstrip().startswith("<div") and "<html" not in panel
        assert 'id="clientNotice-CC001"' in panel and 'id="clientNotice-CC002"' in panel
        client.post("/dashboard/client-rules/CC001/resolved", headers={**headers, "Accept": "application/json"})
        panel = client.get("/dashboard/client-notices", headers=headers).text
        assert 'id="clientNotice-CC001"' not in panel and 'id="clientNotice-CC002"' in panel
        assert self._script_env(client, test_user, "sh") == {"ANTHROPIC_BASE_URL": "https://gw.example.test"}
        client.post("/dashboard/client-rules/CC002/resolved", headers={**headers, "Accept": "application/json"})
        assert client.get("/dashboard/client-notices", headers=headers).text.strip() == ""

    def test_without_any_patch_there_is_still_one_button(self, client, db_session, test_user):
        _configure(db_session, AUTO, patch="")
        _configure(db_session, OUTDATED)
        _violation(db_session, AUTO, "testuser")
        _violation(db_session, OUTDATED, "testuser")
        body = self._page(client, test_user)
        assert 'id="clientNoticesAll"' in body and "all/patch." not in body and "all/resolved" in body

    def test_applied_everything_clears_them_all_and_each_returns_alone(self, client, db_session, test_user):
        self._two(db_session)
        headers = {**web_auth_header(sub=test_user.username), "Accept": "application/json"}
        resp = client.post("/dashboard/client-rules/all/resolved", headers=headers)
        assert resp.json() == {"ok": True, "resolved": 2}
        assert 'id="clientNotice' not in self._page(client, test_user)
        row = _violations(db_session, HTTP)[0]
        row.last_seen_at = _now() + timedelta(minutes=5)             # http again
        db_session.add(row)
        db_session.commit()
        body = self._page(client, test_user)
        assert 'id="clientNotice-CC002"' in body and 'id="clientNotice-CC001"' not in body

    def test_only_this_accounts_notices(self, client, db_session, test_user, admin_user):
        self._two(db_session)
        _violation(db_session, AUTO, "admin")
        client.post("/dashboard/client-rules/all/resolved", headers=web_auth_header(sub=test_user.username))
        assert not client_rules.is_resolved([v for v in _violations(db_session, AUTO) if v.username == "admin"][0])


class TestPacedMail:
    """Mail never reaches the mail server as one burst: an admin's click
    queues, and every send is a batch per run with a pause between mails."""

    def _people(self, db_session, count: int) -> list[str]:
        names = [f"user{i:02d}" for i in range(count)]
        for name in names:
            db_session.add(User(username=name, password_hash="x", api_key=f"sk-{name}", daily_limit_usd=1.0))
            db_session.add(ClientViolation(rule_id=AUTO, username=name, first_seen_at=_now(), last_seen_at=_now()))
        db_session.commit()
        return names

    def test_a_click_queues_everyone_and_sends_only_the_first_batch(self, client, db_session, admin_user, mail_on, monkeypatch):
        monkeypatch.setenv("CLIENT_RULE_MAIL_BATCH", "3")
        self._people(db_session, 8)
        resp = client.post(f"/admin/client-rules/{AUTO}/mail", data={"only_unmailed": "on"}, headers=_admin(admin_user))
        assert resp.json() == {"ok": True, "queued": 8, "batch": 3, "interval_minutes": 10.0}
        assert len(mail_on) == 3
        rows = _violations(db_session, AUTO)
        assert sum(1 for v in rows if client_rules.is_queued(v)) == 5 and len(client_rules.due_accounts(db_session)) == 5

    def test_the_job_drains_the_queue_a_batch_per_run(self, db_session, admin_user, mail_on, monkeypatch):
        monkeypatch.setenv("CLIENT_RULE_MAIL_BATCH", "3")
        names = self._people(db_session, 8)
        assert client_rules.request_mail(db_session, AUTO) == 8
        assert client_rules.run_auto_mail(db_session) == "sent=3 failed=0 waiting=5"
        assert client_rules.run_auto_mail(db_session) == "sent=3 failed=0 waiting=2"
        assert client_rules.run_auto_mail(db_session) == "sent=2 failed=0 waiting=0"
        assert client_rules.run_auto_mail(db_session) == "sent=0 failed=0 waiting=0"
        assert sorted(to for to, _, _ in mail_on) == sorted(f"{n}@example.test" for n in names)   # each exactly once

    def test_automatic_mail_is_paced_the_same_way(self, db_session, mail_on, monkeypatch):
        monkeypatch.setenv("CLIENT_RULE_MAIL_BATCH", "4")
        self._people(db_session, 6)
        _configure(db_session, AUTO, auto_email=True)
        assert client_rules.run_auto_mail(db_session) == "sent=4 failed=0 waiting=2"
        assert client_rules.run_auto_mail(db_session) == "sent=2 failed=0 waiting=0"

    def test_what_an_admin_asked_for_goes_first(self, db_session, mail_on, monkeypatch):
        monkeypatch.setenv("CLIENT_RULE_MAIL_BATCH", "1")
        self._people(db_session, 2)                       # user00, user01: due by automatic mail
        _configure(db_session, AUTO, auto_email=True)
        _configure(db_session, HTTP)
        db_session.add(User(username="zoe", password_hash="x", api_key="sk-zoe", daily_limit_usd=1.0))
        db_session.commit()
        _violation(db_session, HTTP, "zoe")
        client_rules.request_mail(db_session, HTTP)       # an admin's click, for zoe
        client_rules.run_auto_mail(db_session)
        assert [to for to, _, _ in mail_on] == ["zoe@example.test"]

    def test_there_is_a_pause_between_mails_not_before_the_first(self, db_session, mail_on, monkeypatch):
        monkeypatch.setenv("CLIENT_RULE_MAIL_PAUSE_S", "1.5")
        self._people(db_session, 3)
        client_rules.request_mail(db_session, AUTO)
        with patch.object(client_rules.time, "sleep") as sleep:
            client_rules.run_auto_mail(db_session)
        assert [call.args[0] for call in sleep.call_args_list] == [1.5, 1.5] and len(mail_on) == 3

    def test_two_senders_never_mail_the_same_account(self, db_session, test_user, mail_on):
        """The claim is a compare-and-set: whoever finds the row changed since it looked, skips."""
        row = _violation(db_session, AUTO, "testuser")
        client_rules.request_mail(db_session, AUTO)

        real_open_items = client_rules.open_items

        def other_worker_got_there_first(session, now=None, usernames=None):
            items = real_open_items(session, now, usernames)      # this sender has looked...
            from sqlmodel import Session

            with Session(client_rules.engine) as other:           # ...and another one claims the account
                other.execute(
                    client_rules.update(ClientViolation).where(ClientViolation.id == row.id)
                    .values(mail_attempted_at=_now() + timedelta(seconds=1))
                )
                other.commit()
            return items

        with patch.object(client_rules, "open_items", side_effect=other_worker_got_there_first):
            assert client_rules._send_digests(db_session, ["testuser"]) == (0, 0)
        assert mail_on == []

    def test_a_queued_account_that_reports_the_fix_is_dropped(self, db_session, test_user, mail_on):
        _violation(db_session, AUTO, "testuser")
        client_rules.request_mail(db_session, AUTO)
        client_rules.resolve(db_session, AUTO, "testuser")
        assert client_rules.run_auto_mail(db_session) == "sent=0 failed=0 waiting=0" and mail_on == []

    def test_a_failed_queued_mail_waits_before_the_next_try(self, db_session, test_user, mail_on):
        _violation(db_session, AUTO, "testuser")
        now = _now()
        client_rules.request_mail(db_session, AUTO, now=now)
        with patch.object(mailer, "_send_sync", side_effect=TimeoutError("x")):
            client_rules.run_auto_mail(db_session, now)
        assert client_rules.due_accounts(db_session, now + timedelta(minutes=10)) == []
        assert client_rules.is_queued(_violations(db_session, AUTO)[0])              # still shown as waiting
        client_rules.run_auto_mail(db_session, now + timedelta(seconds=client_rules.MAIL_RETRY_S + 60))
        assert len(mail_on) == 1 and not client_rules.is_queued(_violations(db_session, AUTO)[0])

    def test_admin_sees_the_pace_and_who_is_waiting(self, client, db_session, admin_user, mail_on, monkeypatch):
        monkeypatch.setenv("CLIENT_RULE_MAIL_BATCH", "2")
        self._people(db_session, 5)
        client.post(f"/admin/client-rules/{AUTO}/mail", data={"only_unmailed": "on"}, headers=_admin(admin_user))
        body = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        pace = body.split('id="client-rules-mail-status"', 1)[1][:1500]
        assert "at most 2 mails" in pace and "3 waiting" in pace
        card = body.split('id="client-rule-CC001"', 1)[1].split('id="client-rule-CC002"', 1)[0]
        assert card.count(">queued<") == 3


class TestQuietAccounts:
    """An account that has not tripped a check for 15 days (it stopped using
    Claude Code, or fixed it without saying so) is left alone, and starts
    over as a new case when it trips the check again."""

    def test_the_window_is_fifteen_days(self):
        assert client_rules.WINDOW_DAYS == 15

    def test_after_fifteen_quiet_days_nothing_is_shown_or_mailed(self, client, db_session, test_user, mail_on):
        _configure(db_session, AUTO, auto_email=True, email_repeat_days=7)
        row = _violation(db_session, AUTO, "testuser")
        page = lambda: client.get("/dashboard", headers=web_auth_header(sub=test_user.username)).text  # noqa: E731
        row.last_seen_at = _now() - timedelta(days=14)
        db_session.add(row)
        db_session.commit()
        assert 'id="clientNotice-CC001"' in page() and client_rules.due_accounts(db_session) == ["testuser"]
        row.last_seen_at = _now() - timedelta(days=15, minutes=1)
        db_session.add(row)
        db_session.commit()
        assert 'id="clientNotice' not in page() and client_rules.due_accounts(db_session) == []
        assert client_rules.violations(db_session, AUTO) == []                 # gone from the admin's list too

    def test_coming_back_is_a_new_case(self, client, db_session, test_user, mail_on):
        """Mailed once, reported fixed, then quiet for a month: on the next
        violation the account is told again, as if for the first time."""
        _configure(db_session, AUTO, auto_email=True)                           # "Once": no reminders
        long_ago = _now() - timedelta(days=40)
        _violation(db_session, AUTO, "testuser", mailed_at=long_ago, mail_attempted_at=long_ago,
                   resolved_at=long_ago, mail_requested_at=long_ago)
        row = _violations(db_session, AUTO)[0]
        row.first_seen_at = row.last_seen_at = long_ago - timedelta(hours=1)
        db_session.add(row)
        db_session.commit()
        assert client_rules.run_auto_mail(db_session) == "sent=0 failed=0 waiting=0"

        _messages(client, safeguards={})                                        # back, and still doing it
        [row] = _violations(db_session, AUTO)
        assert (row.mailed_at, row.mail_attempted_at, row.resolved_at, row.mail_requested_at) == (None, None, None, None)
        assert abs((row.first_seen_at - (long_ago - timedelta(hours=1))).total_seconds()) < 1   # the first time stays on record
        assert 'id="clientNotice-CC001"' in client.get("/dashboard", headers=web_auth_header(sub=test_user.username)).text
        assert client_rules.run_auto_mail(db_session) == "sent=1 failed=0 waiting=0"

    def test_a_violation_inside_the_window_keeps_its_state(self, client, db_session, test_user):
        recent = _now() - timedelta(days=3)
        _violation(db_session, AUTO, "testuser", mailed_at=recent, mail_attempted_at=recent)
        row = _violations(db_session, AUTO)[0]
        row.last_seen_at = recent
        db_session.add(row)
        db_session.commit()
        client_rules.refresh()
        _messages(client, safeguards={})
        assert _violations(db_session, AUTO)[0].mailed_at is not None           # not a new case: no second "first" mail

    def test_a_reminder_only_goes_to_an_account_seen_again_since_its_last_mail(self, db_session, test_user, mail_on):
        _configure(db_session, AUTO, auto_email=True, email_repeat_days=7)
        _violation(db_session, AUTO, "testuser")
        start = _now()
        client_rules.run_auto_mail(db_session, start)
        # Day 8: inside the 15-day window, a week since the mail, but not seen doing it since: it fixed it.
        assert client_rules.run_auto_mail(db_session, start + timedelta(days=8)) == "sent=0 failed=0 waiting=0"
        row = _violations(db_session, AUTO)[0]
        row.last_seen_at = start + timedelta(days=8, minutes=30)                # no, still doing it
        db_session.add(row)
        db_session.commit()
        assert client_rules.run_auto_mail(db_session, start + timedelta(days=8, hours=1)) == "sent=1 failed=0 waiting=0"


class TestMonthlyReminder:
    """A switch on the Clients card: while on, the 1st of every month from
    09:00 (Asia/Taipei) queues everyone who still has something open."""

    FIRST = datetime(2026, 12, 1, 1, 30)        # naive UTC = 2026-12-01 09:30 in Taipei

    @pytest.fixture
    def monthly(self):
        with patch.object(client_rules.config, "get_client_monthly_mail", return_value=True):
            yield

    def _open(self, db_session, when: datetime, *names: str, rule_id: str = AUTO, **kw) -> None:
        for name in names:
            if db_session.exec(select(User).where(User.username == name)).first() is None:
                db_session.add(User(username=name, password_hash="x", api_key=f"sk-{name}", daily_limit_usd=1.0))
            db_session.add(ClientViolation(rule_id=rule_id, username=name, first_seen_at=when, last_seen_at=when, **kw))
        db_session.commit()

    def test_off_by_default(self, db_session, test_user, mail_on):
        from app.core import config

        assert config.get_client_monthly_mail() is False
        self._open(db_session, self.FIRST - timedelta(hours=2), "testuser")
        client_rules.refresh()
        assert client_rules.queue_monthly(db_session, self.FIRST) == 0
        assert client_rules.run_auto_mail(db_session, self.FIRST) == "sent=0 failed=0 waiting=0"

    @pytest.mark.parametrize("when, queued", [
        (datetime(2026, 11, 30, 15, 59), 0),     # Nov 30 23:59 Taipei
        (datetime(2026, 11, 30, 16, 30), 0),     # Dec 1 00:30 Taipei: the day, before the hour
        (datetime(2026, 12, 1, 0, 59), 0),       # Dec 1 08:59
        (datetime(2026, 12, 1, 1, 0), 2),        # Dec 1 09:00
        (datetime(2026, 12, 1, 15, 30), 2),      # Dec 1 23:30
        (datetime(2026, 12, 1, 16, 5), 0),       # Dec 2 00:05
    ])
    def test_it_runs_on_the_first_from_nine_local_time(self, db_session, monthly, when, queued):
        self._open(db_session, when - timedelta(hours=2), "ann", "ben")
        assert client_rules.queue_monthly(db_session, when) == queued

    def test_everyone_with_something_open_gets_one_mail_in_batches(self, db_session, monthly, mail_on, monkeypatch):
        monkeypatch.setenv("CLIENT_RULE_MAIL_BATCH", "2")
        _configure(db_session, OUTDATED)                                        # neither check has automatic mail
        seen = self.FIRST - timedelta(days=3)
        self._open(db_session, seen, "ann", "ben", "cat")
        self._open(db_session, seen, "ann", rule_id=OUTDATED)
        assert client_rules.run_auto_mail(db_session, self.FIRST) == "sent=2 failed=0 waiting=1 monthly_queued=3"
        assert client_rules.run_auto_mail(db_session, self.FIRST + timedelta(minutes=10)) == "sent=1 failed=0 waiting=0"
        assert client_rules.run_auto_mail(db_session, self.FIRST + timedelta(minutes=20)) == "sent=0 failed=0 waiting=0"
        assert sorted(to for to, _, _ in mail_on) == ["ann@example.test", "ben@example.test", "cat@example.test"]
        ann = [body for to, _, body in mail_on if to == "ann@example.test"][0]
        assert "Reference: CC001, CC003." in ann                                # everything ann has open, in one mail

    def test_it_leaves_out_who_is_quiet_fixed_or_told_and_not_seen_since(self, db_session, monthly, mail_on):
        recent = self.FIRST - timedelta(days=2)
        self._open(db_session, recent, "still")                                             # open, never mailed
        self._open(db_session, self.FIRST - timedelta(days=20), "quiet")                    # outside the 15 days
        self._open(db_session, recent, "fixed", resolved_at=recent + timedelta(hours=1))    # said "I've applied it"
        self._open(db_session, recent, "told", mailed_at=recent + timedelta(hours=1),
                   mail_attempted_at=recent + timedelta(hours=1))                           # mailed after its last violation
        self._open(db_session, recent, "again", mailed_at=recent - timedelta(days=5),
                   mail_attempted_at=recent - timedelta(days=5))                            # mailed, and seen doing it since
        db_session.add(ClientViolation(rule_id=AUTO, username="app_batch", first_seen_at=recent, last_seen_at=recent))
        db_session.commit()
        client_rules.run_auto_mail(db_session, self.FIRST)
        assert sorted(to for to, _, _ in mail_on) == ["again@example.test", "still@example.test"]

    def test_once_a_month(self, db_session, monthly, mail_on):
        self._open(db_session, self.FIRST - timedelta(days=1), "ann")
        client_rules.run_auto_mail(db_session, self.FIRST)
        for row in _violations(db_session, AUTO):                               # still doing it, later the same day
            row.last_seen_at = self.FIRST + timedelta(hours=3)
            db_session.add(row)
        db_session.commit()
        assert client_rules.queue_monthly(db_session, self.FIRST + timedelta(hours=4)) == 0
        assert len(mail_on) == 1
        january = datetime(2027, 1, 1, 2, 0)
        for row in _violations(db_session, AUTO):
            row.last_seen_at = january - timedelta(days=1)
            db_session.add(row)
        db_session.commit()
        client_rules.run_auto_mail(db_session, january)
        assert len(mail_on) == 2

    def test_a_deprecated_check_is_not_part_of_it(self, db_session, monthly, mail_on):
        self._open(db_session, self.FIRST - timedelta(days=1), "ann")
        with TestLifecycle._with(deprecated="on its way out"):
            assert client_rules.run_auto_mail(db_session, self.FIRST) == "sent=0 failed=0 waiting=0"

    def test_the_switch(self, client, admin_user, mail_on):
        headers = web_auth_header(sub=admin_user.username, scopes=["admin"])
        body = client.get("/admin", headers=headers).text
        switch = body.split('id="client-rules-monthly"', 1)[1][:1500]
        assert ">Turn on<" in switch and 'name="enabled" value="on"' in switch
        with patch("app.routers.admin.set_client_monthly_mail") as save:        # never a real config write in tests
            resp = client.post("/admin/client-rules/monthly-mail", data={"enabled": "on"}, headers=_admin(admin_user))
            assert resp.json() == {"ok": True, "enabled": True} and save.call_args.args == (True,)
            client.post("/admin/client-rules/monthly-mail", data={"enabled": "off"}, headers=_admin(admin_user))
            assert save.call_args.args == (False,)
        assert mail_on == []                                                    # the click itself sends nothing
        with patch("app.routers.admin.get_client_monthly_mail", return_value=True):
            switch = client.get("/admin", headers=headers).text.split('id="client-rules-monthly"', 1)[1][:1500]
        assert ">Turn off<" in switch and "Next:" in switch

    def test_only_admins_can_switch_it(self, client, test_user):
        headers = {**web_auth_header(sub=test_user.username), "Accept": "application/json"}
        with patch("app.routers.admin.set_client_monthly_mail") as save:
            assert client.post("/admin/client-rules/monthly-mail", data={"enabled": "on"}, headers=headers).status_code in (401, 403)
        save.assert_not_called()

    def test_the_setting_is_read_like_the_other_app_switches(self):
        from app.core import config

        for raw, expected in (("true", True), (True, True), ("no", False), ([1], False)):
            with patch.dict("app.core.config.APP_CONFIG", {"client_rules_monthly_mail": raw}):
                assert config.get_client_monthly_mail() is expected, raw


class TestRegularReminders:
    """Weekly or monthly: a check's "remind again" is a repeat in days."""

    def _still_violating(self, db_session, when: datetime) -> None:
        for row in _violations(db_session, AUTO):
            row.last_seen_at = when
            db_session.add(row)
        db_session.commit()

    @pytest.mark.parametrize("days", [7, 30])
    def test_an_account_that_keeps_violating_is_reminded_every_period(self, db_session, test_user, mail_on, days):
        _configure(db_session, AUTO, auto_email=True, email_repeat_days=days)
        _violation(db_session, AUTO, "testuser")
        start = _now()
        client_rules.run_auto_mail(db_session, start)
        for period in (1, 2, 3):
            almost = start + timedelta(days=days * period - 1)
            self._still_violating(db_session, almost)
            client_rules.run_auto_mail(db_session, almost)
            assert len(mail_on) == period                      # not yet
            due = start + timedelta(days=days * period, minutes=5)
            self._still_violating(db_session, due)
            client_rules.run_auto_mail(db_session, due)
            assert len(mail_on) == period + 1
            # the period is counted from the mail before, whenever it went out
            for row in _violations(db_session, AUTO):
                row.mailed_at = row.mail_attempted_at = start + timedelta(days=days * period)
                db_session.add(row)
            db_session.commit()

    def test_an_account_that_stopped_is_not_reminded(self, db_session, test_user, mail_on):
        _configure(db_session, AUTO, auto_email=True, email_repeat_days=7)
        _violation(db_session, AUTO, "testuser")
        start = _now()
        client_rules.run_auto_mail(db_session, start)
        client_rules.run_auto_mail(db_session, start + timedelta(days=8))    # last violation is 8 days old: outside the window
        assert len(mail_on) == 1

    def test_the_form_offers_weekly_and_monthly(self, client, db_session, admin_user):
        _configure(db_session, AUTO, email_repeat_days=30)
        _configure(db_session, HTTP, email_repeat_days=10)       # a value saved before the choices existed
        body = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        auto = body.split('id="client-rule-CC001"', 1)[1].split('id="client-rule-CC002"', 1)[0]
        for value, label in (("0", "Once"), ("7", "Every week"), ("30", "Every month")):
            assert f'<option value="{value}"' in auto and label in auto
        assert '<option value="30" selected' in auto
        http = body.split('id="client-rule-CC002"', 1)[1].split('id="client-rule-CC003"', 1)[0]
        assert '<option value="10" selected' in http and "Every 10 days" in http


class TestAutoMail:
    def test_only_a_check_that_asks_for_it_starts_a_mail(self, db_session, test_user, admin_user, mail_on):
        _configure(db_session, HTTP, title="quiet one")
        _configure(db_session, AUTO, title="loud one", auto_email=True)
        _violation(db_session, HTTP, "testuser")           # only the check without automatic mail
        _violation(db_session, AUTO, "admin")
        assert "sent=1" in client_rules.run_auto_mail(db_session)
        assert [to for to, _, _ in mail_on] == ["admin@example.test"]

    def test_a_check_that_is_off_mails_nobody(self, db_session, test_user, mail_on):
        _configure(db_session, AUTO, auto_email=True, enabled=False)
        _violation(db_session, AUTO, "testuser")
        client_rules.run_auto_mail(db_session)
        assert mail_on == []

    def test_once_unless_a_repeat_is_set(self, db_session, test_user, mail_on):
        _configure(db_session, AUTO, auto_email=True)
        _violation(db_session, AUTO, "testuser")
        now = _now()
        client_rules.run_auto_mail(db_session, now)
        client_rules.run_auto_mail(db_session, now + timedelta(days=30))
        assert len(mail_on) == 1

        _configure(db_session, AUTO, auto_email=True, email_repeat_days=7)
        client_rules.run_auto_mail(db_session, now + timedelta(days=6))
        assert len(mail_on) == 1
        for row in _violations(db_session, AUTO):          # still violating a week later
            row.last_seen_at = now + timedelta(days=8)
            db_session.add(row)
        db_session.commit()
        client_rules.run_auto_mail(db_session, now + timedelta(days=8))
        assert len(mail_on) == 2

    def test_accounts_outside_the_window_are_left_alone(self, db_session, test_user, mail_on):
        _configure(db_session, AUTO, auto_email=True)
        row = _violation(db_session, AUTO, "testuser")
        row.last_seen_at = _now() - timedelta(days=client_rules.WINDOW_DAYS + 1)
        db_session.add(row)
        db_session.commit()
        client_rules.run_auto_mail(db_session)
        assert mail_on == []

    def test_a_failure_is_retried_later_not_every_run(self, db_session, test_user, mail_on):
        _configure(db_session, AUTO, auto_email=True)
        _violation(db_session, AUTO, "testuser")
        now = _now()
        with patch.object(mailer, "_send_sync", side_effect=TimeoutError("x")) as failing:
            client_rules.run_auto_mail(db_session, now)
            client_rules.run_auto_mail(db_session, now + timedelta(minutes=10))
            assert failing.call_count == 1
        client_rules.run_auto_mail(db_session, now + timedelta(seconds=client_rules.MAIL_RETRY_S + 60))
        assert len(mail_on) == 1
        assert _violations(db_session, AUTO)[0].mail_error == ""

    def test_job_is_idle_while_mail_is_unconfigured(self, db_session, test_user):
        _configure(db_session, AUTO, auto_email=True)
        _violation(db_session, AUTO, "testuser")
        assert client_rules.run_auto_mail(db_session) == "mail not configured"
        assert _violations(db_session, AUTO)[0].mail_attempted_at is None

    def test_it_is_a_scheduled_job(self):
        from app.services import scheduler

        assert client_rules.MAIL_JOB.name == "client_rule_mail" and client_rules.MAIL_JOB.interval() > 0
        assert scheduler.run_if_due(client_rules.MAIL_JOB) == "ok"


class TestMailLog:
    """Every mail the gateway tried to send is written down with the address
    and how it ended, and the admin can review it."""

    _LOG = "/admin/client-rules/mail-log"

    def _log(self, db_session) -> list[ClientMailLog]:
        db_session.expire_all()
        return list(db_session.exec(select(ClientMailLog).order_by(ClientMailLog.id)).all())

    def _person(self, db_session, name: str, rule_id: str = AUTO) -> None:
        db_session.add(User(username=name, password_hash="x", api_key=f"sk-{name}", daily_limit_usd=1.0))
        db_session.commit()
        _violation(db_session, rule_id, name)

    def test_a_sent_mail_is_recorded_with_its_address_and_rules(self, db_session, test_user, mail_on):
        _configure(db_session, HTTP, patch="ANTHROPIC_BASE_URL=https://gw.example.test")
        _violation(db_session, AUTO, "testuser")
        _violation(db_session, HTTP, "testuser")
        client_rules.request_mail(db_session, AUTO)
        client_rules.run_auto_mail(db_session)
        [row] = self._log(db_session)
        assert (row.username, row.address, row.ok, row.error) == ("testuser", "testuser@example.test", True, "")
        assert row.rule_ids == f"{AUTO},{HTTP}" and row.kind == "requested" and row.actor == ""
        assert row.subject == mail_on[0][1]

    def test_a_failed_mail_is_recorded_with_the_error_and_each_retry_adds_a_line(self, db_session, test_user, mail_on):
        _violation(db_session, AUTO, "testuser")
        client_rules.request_mail(db_session, AUTO)
        with patch.object(mailer, "_send_sync", side_effect=TimeoutError("timed out")):
            client_rules.run_auto_mail(db_session)
        [failed] = self._log(db_session)
        assert not failed.ok and failed.error == "TimeoutError: timed out" and failed.address == "testuser@example.test"
        later = _now() + timedelta(seconds=client_rules.MAIL_RETRY_S + 60)
        client_rules.run_auto_mail(db_session, now=later)
        assert [(row.ok, row.error) for row in self._log(db_session)] == [(False, "TimeoutError: timed out"), (True, "")]

    def test_the_log_says_why_each_mail_went_out(self, db_session, mail_on):
        self._person(db_session, "asked", HTTP)
        self._person(db_session, "auto", AUTO)
        _configure(db_session, HTTP, patch="ANTHROPIC_BASE_URL=https://gw.example.test")
        _configure(db_session, AUTO, auto_email=True)
        client_rules.request_mail(db_session, HTTP)
        client_rules.run_auto_mail(db_session)
        assert {row.username: row.kind for row in self._log(db_session)} == {"asked": "requested", "auto": "automatic"}

    def test_test_mail_is_recorded_under_the_admin_who_sent_it(self, client, db_session, admin_user, mail_on):
        url = f"/admin/client-rules/{AUTO}/mail-test"
        client.post(url, data={**_FORM, "to": "someone@example.test"}, headers=_admin(admin_user))
        with patch.object(mailer, "_send_sync", side_effect=ConnectionError("refused")):
            client.post(url, data=_FORM, headers=_admin(admin_user))
        sent, failed = self._log(db_session)
        assert (sent.kind, sent.address, sent.ok, sent.actor, sent.username) == ("test", "someone@example.test", True, "admin", "")
        assert sent.subject.startswith("[TEST] ") and sent.rule_ids == AUTO
        assert (failed.address, failed.ok, failed.error) == ("admin@example.test", False, "ConnectionError: refused")

    def test_a_mail_that_was_never_tried_is_not_in_the_log(self, client, db_session, admin_user, test_user):
        """Mail not configured, or an address outside the company: nothing was sent, nothing is recorded."""
        client.post(f"/admin/client-rules/{AUTO}/mail-test", data=_FORM, headers=_admin(admin_user))
        _violation(db_session, AUTO, "testuser")
        client_rules.request_mail(db_session, AUTO)
        client_rules.run_auto_mail(db_session)
        assert self._log(db_session) == []

    def test_a_broken_log_never_stops_the_mail_or_its_stamp(self, db_session, test_user, mail_on):
        _violation(db_session, AUTO, "testuser")
        client_rules.request_mail(db_session, AUTO)
        with patch.object(client_rules, "ClientMailLog", side_effect=RuntimeError("no table")):
            assert client_rules.run_auto_mail(db_session) == "sent=1 failed=0 waiting=0"
        assert len(mail_on) == 1 and _violations(db_session, AUTO)[0].mailed_at is not None

    def _history(self, db_session) -> None:
        now = _now()
        for i in range(30):
            db_session.add(ClientMailLog(
                created_at=now - timedelta(minutes=i), username=f"user{i:02d}", address=f"user{i:02d}@example.test",
                subject="s", rule_ids=AUTO, kind="automatic", ok=i % 10 != 0, error="" if i % 10 else "TimeoutError: timed out",
            ))
        db_session.add(ClientMailLog(
            created_at=now - timedelta(days=40), address="admin@example.test", subject="[TEST] s", rule_ids=AUTO,
            kind="test", ok=True, actor="admin",
        ))
        db_session.commit()

    def test_the_log_is_read_newest_first_a_page_at_a_time(self, client, db_session, admin_user):
        self._history(db_session)
        first = client.get(self._LOG, headers=_admin(admin_user)).json()
        size = client_rules.MAIL_LOG_PAGE
        pages = -(-31 // size)
        assert (first["total"], first["page"], first["pages"], len(first["rows"])) == (31, 1, pages, size)
        assert first["rows"][0]["username"] == "user00" and first["rows"][0]["rules"] == [AUTO]
        second = client.get(self._LOG, params={"page": 2}, headers=_admin(admin_user)).json()
        assert second["rows"][0]["username"] == f"user{size:02d}"
        last = client.get(self._LOG, params={"page": 99}, headers=_admin(admin_user)).json()    # past the end: the last page
        assert last["page"] == pages and len(last["rows"]) == 31 - size * (pages - 1) and last["rows"][-1]["kind"] == "test"

    def test_the_log_can_be_narrowed_to_failures_tests_and_one_account(self, client, db_session, admin_user):
        self._history(db_session)

        def rows(**params):
            return client.get(self._LOG, params=params, headers=_admin(admin_user)).json()

        failed = rows(status="failed")
        assert failed["total"] == 3 and all(not r["ok"] and r["error"] for r in failed["rows"])
        assert rows(status="sent")["total"] == 28
        assert [r["address"] for r in rows(status="test")["rows"]] == ["admin@example.test"]
        assert [r["username"] for r in rows(q="USER07")["rows"]] == ["user07"]
        assert rows(q="admin@")["total"] == 1                  # the address is searched too
        assert rows(q="%")["total"] == 0                       # a wildcard is matched as text

    def test_the_counts_cover_the_last_thirty_days_whatever_the_filter(self, client, db_session, admin_user):
        self._history(db_session)
        log = client.get(self._LOG, params={"status": "test"}, headers=_admin(admin_user)).json()
        assert (log["sent"], log["failed"], log["days"]) == (27, 3, client_rules.MAIL_LOG_DAYS)   # the 40-day-old test mail is not counted

    def test_only_admins_read_the_log(self, client, db_session, test_user):
        resp = client.get(self._LOG, headers=web_auth_header(sub="testuser", scopes=[]))
        assert resp.status_code in (401, 403)

    def test_the_admin_page_shows_the_log(self, client, db_session, admin_user, mail_on):
        db_session.add(ClientMailLog(
            username="ghost", address="ghost@example.test", subject="<b>s</b>", rule_ids=f"{AUTO},{HTTP}",
            kind="requested", ok=False, error="ErrorNonExistentMailbox: <no such mailbox>",
        ))
        db_session.commit()
        body = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        section = body[body.index('id="client-mail-log"'):body.index('id="mail-log-prev"')]
        assert "ghost@example.test" in section and f"{AUTO}, {HTTP}" in section
        assert "0 sent" in section and "1 failed" in section
        assert "ErrorNonExistentMailbox: &lt;no such mailbox&gt;" in section and "<no such mailbox>" not in section

    def test_deleting_an_account_keeps_its_mail_history(self, client, db_session, admin_user, mail_on):
        self._person(db_session, "leaver")
        client_rules.request_mail(db_session, AUTO)
        client_rules.run_auto_mail(db_session)
        leaver = db_session.exec(select(User).where(User.username == "leaver")).one()
        resp = client.post(f"/admin/users/{leaver.id}/delete", headers=_admin(admin_user))
        assert resp.status_code < 400
        assert [row.username for row in self._log(db_session)] == ["leaver"]


class TestKeyPatch:
    """A regenerated key reaches Claude Code as a patch of its own: the one
    variable the key lives in is set, the older second one is removed,
    nothing else."""

    @staticmethod
    def _want(key: str) -> dict:
        return {"ANTHROPIC_AUTH_TOKEN": key, "ANTHROPIC_API_KEY": None}

    @staticmethod
    def _env(text: str) -> dict:
        import base64
        import re
        return json.loads(base64.b64decode(re.search(r"Buffer\.from\('([^']+)'", text).group(1)))

    def test_it_sets_the_accounts_own_key_and_nothing_else(self, client, db_session, test_user, admin_user):
        assert client_rules.API_KEY_ENV == "ANTHROPIC_AUTH_TOKEN"
        assert client_rules.API_KEY_ENV_RETIRED == ("ANTHROPIC_API_KEY",)
        headers = web_auth_header(sub=test_user.username)
        for fmt in ("bat", "sh"):
            resp = client.get(f"/dashboard/api-key/patch.{fmt}", headers=headers)
            assert resp.status_code == 200
            assert f'filename="claude-code-key.{fmt}"' in resp.headers["content-disposition"]
            assert resp.headers["cache-control"] == "no-store"
            assert self._env(resp.text) == self._want(test_user.api_key)
            assert test_user.api_key not in resp.text                # only inside the encoded patch
        assert client.get("/dashboard/api-key/patch.exe", headers=headers).status_code == 404
        other = client.get("/dashboard/api-key/patch.sh", headers=web_auth_header(sub=admin_user.username))
        assert self._env(other.text) == self._want(admin_user.api_key)

    def test_after_regenerating_it_carries_the_new_key(self, client, db_session, test_user):
        headers = web_auth_header(sub=test_user.username)
        old = test_user.api_key
        new = client.post("/dashboard/refresh-key", headers=headers).json()["api_key"]
        assert new != old
        assert self._env(client.get("/dashboard/api-key/patch.sh", headers=headers).text) == self._want(new)

    def test_it_needs_a_login(self, client):
        assert client.get("/dashboard/api-key/patch.bat").status_code in (401, 403)

    def test_it_is_not_mixed_with_open_notices_or_the_baseline(self, client, db_session, test_user, monkeypatch):
        monkeypatch.setattr(client_checks, "BASELINE_ENV", "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1")
        _violation(db_session, AUTO, "testuser")
        headers = web_auth_header(sub=test_user.username)
        assert self._env(client.get("/dashboard/api-key/patch.bat", headers=headers).text) == self._want(test_user.api_key)
        fix = self._env(client.get("/dashboard/client-rules/all/patch.bat", headers=headers).text)
        assert "ANTHROPIC_AUTH_TOKEN" not in fix                     # a notice's patch never carries the key

    @pytest.mark.skipif(shutil.which("sh") is None, reason="sh not installed")
    @pytest.mark.parametrize("runtime", ["node", "python3"])
    def test_the_script_replaces_the_key_drops_the_old_copy_and_leaves_the_rest(self, tmp_path, runtime):
        before = {"model": "m", "env": {
            "ANTHROPIC_API_KEY": "sk-old", "ANTHROPIC_AUTH_TOKEN": "sk-old", "ANTHROPIC_BASE_URL": "https://gw", "KEEP": "1"}}
        result = TestPatch._run_sh(tmp_path, client_rules.key_env("sk-new"), json.dumps(before), runtime)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "sk-new" not in result.stdout + result.stderr          # the script never prints the key
        got = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert got == {"model": "m", "env": {"ANTHROPIC_AUTH_TOKEN": "sk-new", "ANTHROPIC_BASE_URL": "https://gw", "KEEP": "1"}}

    def test_the_dashboard_offers_it_next_to_the_key(self, client, test_user):
        body = client.get("/dashboard", headers=web_auth_header(sub=test_user.username)).text
        modal = body.split('id="apiKeyModal"', 1)[1]
        assert "/dashboard/api-key/patch.bat" in modal and "/dashboard/api-key/patch.sh" in modal
        assert "ANTHROPIC_AUTH_TOKEN" in modal and "ANTHROPIC_API_KEY" in modal      # set / removed, both named

    def test_the_download_is_in_the_audit_log_without_the_key(self, client, db_session, test_user):
        from app.models.schema import AuditLog
        client.get("/dashboard/api-key/patch.sh", headers=web_auth_header(sub=test_user.username))
        db_session.expire_all()
        [entry] = db_session.exec(select(AuditLog)).all()
        assert (entry.actor, entry.method, entry.status) == ("testuser", "GET", 200)
        assert entry.path == "/dashboard/api-key/patch.sh" and test_user.api_key not in entry.detail


class TestInstaller:
    def test_example_installer_keeps_the_key_in_one_variable(self):
        """ANTHROPIC_AUTH_TOKEN only: a second copy in ANTHROPIC_API_KEY would
        go stale on the next regenerate, so an older one is taken out."""
        text = (Path(__file__).resolve().parent.parent / "setup" / "install-claude-code-example.bat").read_text(encoding="utf-8")
        assert "$e['ANTHROPIC_AUTH_TOKEN']='%API_KEY%'" in text
        assert "$e['ANTHROPIC_API_KEY']" not in text and "$e.Remove('ANTHROPIC_API_KEY')" in text

    def test_example_installer_writes_the_auto_mode_settings(self):
        text = (Path(__file__).resolve().parent.parent / "setup" / "install-claude-code-example.bat").read_text(encoding="utf-8")
        assert "$e['CLAUDE_CODE_AUTO_MODE_SERVER']='0'" in text
        assert "$e['CLAUDE_CODE_AUTO_MODE_MODEL']='%AUTO_MODE_MODEL%'" in text
        assert 'set "AUTO_MODE_CLIENT_CHECK=1"' in text

    def test_example_installer_blocks_nonessential_traffic_with_or_without_discovery(self):
        """Model discovery has its own switch; the master flag no longer has
        to be removed for it, so telemetry and the auto-updater stay blocked."""
        text = (Path(__file__).resolve().parent.parent / "setup" / "install-claude-code-example.bat").read_text(encoding="utf-8")
        assert "\"$e['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC']='1';\" ^" in text
        assert "$e.Remove('CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC')" not in text
        assert "{$e['CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY']='1'}else{$e.Remove('CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY')}" in text
