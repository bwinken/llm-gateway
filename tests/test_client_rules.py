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

from app.models.schema import ClientRuleSetting, ClientVersion, ClientViolation, User
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
             patch.object(client_checks, "BY_ID", {c.id: c for c in retired}):
            client_rules.refresh()
            assert _messages(client, safeguards={})[0].status_code == 200
            assert _violators(db_session, AUTO) == []
            assert client_rules.get_rule(db_session, AUTO) is None
            assert AUTO not in [r.id for r in client_rules.rules(db_session)]
            resp = client.post(f"/admin/client-rules/{AUTO}", data=_FORM, headers=_admin(admin_user))
            assert resp.status_code == 404


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


class TestDashboard:
    def _page(self, client, user: User) -> str:
        return client.get("/dashboard", headers=web_auth_header(sub=user.username)).text

    def test_violating_account_sees_notice_patch_and_download(self, client, db_session, test_user):
        _violation(db_session, AUTO, "testuser", client_version="2.1.280")
        body = self._page(client, test_user)
        assert 'id="clientNotice-CC001"' in body and client_checks.BY_ID[AUTO].title in body
        assert "CLAUDE_CODE_AUTO_MODE_SERVER" in body
        assert "/dashboard/client-rules/CC001/patch.bat" in body

    def test_the_admins_wording_replaces_the_default(self, client, db_session, test_user):
        _configure(db_session, AUTO, title="Our own title", message="Our own words.")
        _violation(db_session, AUTO, "testuser")
        body = self._page(client, test_user)
        assert "Our own title" in body and "Our own words." in body and client_checks.BY_ID[AUTO].title not in body

    def test_the_patch_follows_the_accounts_access(self, client, db_session, test_user):
        _configure(db_session, AUTO, patch='{"MODEL": "local-m"}', patch_azure='{"MODEL": "azure-m"}', patch_aws='{"MODEL": "claude-m"}')
        _violation(db_session, AUTO, "testuser")
        headers = web_auth_header(sub=test_user.username)
        url = "/dashboard/client-rules/CC001/patch.bat"

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
        assert "data-client-notice" not in self._page(client, test_user)

    def test_notice_clears_after_the_window_or_when_the_check_is_off(self, client, db_session, test_user):
        row = _violation(db_session, AUTO, "testuser")
        row.last_seen_at = _now() - timedelta(days=client_rules.WINDOW_DAYS + 1)
        db_session.add(row)
        db_session.commit()
        assert "data-client-notice" not in self._page(client, test_user)
        row.last_seen_at = _now()
        db_session.add(row)
        db_session.commit()
        assert "data-client-notice" in self._page(client, test_user)
        _configure(db_session, AUTO, enabled=False)
        assert "data-client-notice" not in self._page(client, test_user)

    def test_message_is_escaped(self, client, db_session, test_user):
        _configure(db_session, AUTO, title="<script>alert(1)</script>")
        _violation(db_session, AUTO, "testuser")
        assert "<script>alert(1)</script>" not in self._page(client, test_user)

    def test_patch_download_only_for_an_account_with_the_violation(self, client, db_session, test_user):
        url = "/dashboard/client-rules/CC001/patch.bat"
        headers = web_auth_header(sub=test_user.username)
        assert client.get(url, headers=headers).status_code == 404
        _violation(db_session, AUTO, "testuser")
        resp = client.get(url, headers=headers)
        assert resp.status_code == 200 and "attachment" in resp.headers["content-disposition"]
        assert b"GW-PATCH-BEGIN" in resp.content and b"\r\n" in resp.content
        assert client.get("/dashboard/client-rules/CC999/patch.bat", headers=headers).status_code == 404

    def test_no_download_without_a_patch(self, client, db_session, test_user):
        _violation(db_session, NO_CLOUD_ACCESS, "testuser")           # this check has no default patch
        _configure(db_session, NO_CLOUD_ACCESS)
        headers = web_auth_header(sub=test_user.username)
        assert client.get(f"/dashboard/client-rules/{NO_CLOUD_ACCESS}/patch.bat", headers=headers).status_code == 404
        body = self._page(client, test_user)
        assert f'id="clientNotice-{NO_CLOUD_ACCESS}"' in body and "patch.bat" not in body


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
    ])
    def test_bad_input_is_refused(self, client, db_session, admin_user, rule_id, change, message):
        resp = client.post(f"/admin/client-rules/{rule_id}", data={**_FORM, **change}, headers=_admin(admin_user))
        assert resp.status_code == 400 and message in resp.json()["detail"]
        assert db_session.exec(select(ClientRuleSetting)).all() == []

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
        assert resp.json() == {"ok": True, "queued": 2}       # no app accounts, no accounts that are gone
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

    def test_mail_escapes_what_the_admin_wrote(self, mail_on):
        draft = SimpleNamespace(id="CC001", title="<b>t</b>", message="a\n<i>b</i>", email_subject="")
        subject, body = client_rules.render_mail(draft, "u<1>", {"A": "<x>"})
        assert "<b>t</b>" not in body and "<i>b</i>" not in body and "<x>" not in body and "u<1>" not in body
        assert "a<br>" in body and subject.endswith("<b>t</b>")


class TestAutoMail:
    def test_only_checks_that_ask_for_it(self, db_session, test_user, mail_on):
        _configure(db_session, HTTP, title="quiet one")
        _configure(db_session, AUTO, title="loud one", auto_email=True)
        _violation(db_session, HTTP, "testuser")
        _violation(db_session, AUTO, "testuser")
        assert "sent=1" in client_rules.run_auto_mail(db_session)
        assert len(mail_on) == 1 and "loud one" in mail_on[0][2]

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


class TestInstaller:
    def test_example_installer_writes_the_auto_mode_settings(self):
        text = (Path(__file__).resolve().parent.parent / "setup" / "install-claude-code-example.bat").read_text(encoding="utf-8")
        assert "$e['CLAUDE_CODE_AUTO_MODE_SERVER']='0'" in text
        assert "$e['CLAUDE_CODE_AUTO_MODE_MODEL']='%AUTO_MODE_MODEL%'" in text
        assert 'set "AUTO_MODE_CLIENT_CHECK=1"' in text
