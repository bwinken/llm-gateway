"""Client rules (app/services/client_rules.py) and mail (app/services/mailer.py).

An admin-written rule picks Messages requests (a body field, a Claude Code
version, a model). A matching account is recorded; it then sees the rule's
notice on its dashboard, can download a script that merges the rule's patch
into its settings.json, can be reminded once a day in the terminal, and can
be mailed (by hand or automatically). The Claude Code version of every
Messages request is recorded per account. None of it may fail a request,
except the reminder an admin chose.
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

from app.models.schema import ClientRule, ClientRuleHit, ClientVersion, User
from app.services import client_rules, mailer
from tests.conftest import auth_header, make_httpx_response, web_auth_header

_DOWNSTREAM = {
    "id": "chatcmpl-1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}
_UA = "claude-cli/2.1.280 (external, cli)"
_ENV = {"CLAUDE_CODE_AUTO_MODE_SERVER": "0"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _rule(db_session, **kw) -> ClientRule:
    values = dict(
        name="Auto mode", body_field="safeguards", title="Auto mode needs a setting", message="Apply the patch.",
        patch=json.dumps(_ENV), created_at=_now(), updated_at=_now(),
    )
    values.update(kw)
    rule = ClientRule(**values)
    db_session.add(rule)
    db_session.commit()
    db_session.refresh(rule)
    client_rules.refresh()
    return rule


def _messages(client, key: str = "sk-testkey123", ua: str | None = _UA, **extra):
    async def _post(*args, **kwargs):
        return make_httpx_response(200, _DOWNSTREAM)

    body = {"model": "test-llm", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}], **extra}
    headers = auth_header(key)
    if ua:
        headers["User-Agent"] = ua
    with patch.object(client.__httpx_mock__, "post", side_effect=_post) as post:
        resp = client.post("/v1/messages", json=body, headers=headers)
    return resp, post


def _hits(db_session, rule: ClientRule) -> list[ClientRuleHit]:
    db_session.expire_all()
    return list(db_session.exec(select(ClientRuleHit).where(ClientRuleHit.rule_id == rule.id)).all())


def _hit(db_session, rule: ClientRule, username: str, **kw) -> ClientRuleHit:
    hit = ClientRuleHit(rule_id=rule.id, username=username, first_seen_at=_now(), last_seen_at=_now(), **kw)
    db_session.add(hit)
    db_session.commit()
    db_session.refresh(hit)
    return hit


def _admin(admin: User) -> dict:
    return {**web_auth_header(sub=admin.username, scopes=["admin"]), "Accept": "application/json"}


_FORM = {
    "name": "Auto mode", "enabled": "on", "body_field": "safeguards", "title": "Fix auto mode",
    "message": "Apply the patch.", "patch": "CLAUDE_CODE_AUTO_MODE_SERVER=0", "action": "notice",
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


class TestVersionParsing:
    def test_claude_code_user_agent(self):
        assert client_rules.parse_version("claude-cli/2.1.302 (external, cli)") == "2.1.302"
        assert client_rules.parse_version("python-httpx/0.28") == ""
        assert client_rules.parse_version(None) == ""

    def test_versions_order_numerically(self):
        assert client_rules.version_key("2.1.99") < client_rules.version_key("2.1.302")


class TestMatching:
    def test_a_matching_request_is_recorded_and_still_served(self, client, db_session, test_user):
        rule = _rule(db_session)
        resp, post = _messages(client, safeguards={"x": 1})
        assert resp.status_code == 200 and resp.json()["content"][0]["text"] == "ok"
        [hit] = _hits(db_session, rule)
        assert hit.username == "testuser" and hit.client_version == "2.1.280"
        assert "safeguards" not in post.call_args.kwargs["json"]   # still not forwarded

    def test_an_ordinary_request_matches_nothing(self, client, db_session, test_user):
        rule = _rule(db_session)
        assert _messages(client)[0].status_code == 200
        assert _hits(db_session, rule) == []

    def test_version_condition(self, client, db_session, test_user):
        rule = _rule(db_session, body_field="", version_below="2.1.302")
        _messages(client, ua="claude-cli/2.1.302 (external, cli)")
        _messages(client, ua="curl/8")                       # other clients never match a version rule
        assert _hits(db_session, rule) == []
        _messages(client, ua="claude-cli/2.1.99 (external, cli)")
        assert len(_hits(db_session, rule)) == 1

    def test_model_condition(self, client, db_session, test_user):
        rule = _rule(db_session, body_field="", models=["retired-model"])
        _messages(client)
        assert _hits(db_session, rule) == []
        _messages(client, model="retired-model")
        assert len(_hits(db_session, rule)) == 1

    def test_plain_http_condition(self, client, db_session, test_user):
        rule = _rule(db_session, body_field="", plain_http=True)

        async def _post(*args, **kwargs):
            return make_httpx_response(200, _DOWNSTREAM)

        body = {"model": "test-llm", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}
        with patch.object(client.__httpx_mock__, "post", side_effect=_post):
            assert client.post("https://testserver/v1/messages", json=body, headers=auth_header()).status_code == 200
            assert _hits(db_session, rule) == []
            # A client cannot hide behind its own header: the scheme is the connection's.
            sneaky = {**auth_header(), "X-Forwarded-Proto": "https"}
            assert client.post("http://testserver/v1/messages", json=body, headers=sneaky).status_code == 200
        assert [h.username for h in _hits(db_session, rule)] == ["testuser"]

    def test_plain_http_alone_is_a_condition(self, client, db_session, admin_user):
        form = {**_FORM, "body_field": "", "plain_http": "on"}
        resp = client.post("/admin/client-rules", data=form, headers=_admin(admin_user))
        assert resp.status_code == 200, resp.text
        rule = db_session.get(ClientRule, resp.json()["id"])
        assert rule.plain_http and client_rules.condition_text(rule) == "sent over http://"

    def test_every_condition_must_hold(self, client, db_session, test_user):
        rule = _rule(db_session, version_below="2.0.0")
        _messages(client, safeguards={})                     # has the field, but 2.1.280 is not older
        assert _hits(db_session, rule) == []

    def test_scope_limits_who_matches(self, client, db_session, test_user):
        other = _rule(db_session, name="others", usernames=["somebody-else"])
        mine = _rule(db_session, name="mine", usernames=["testuser"])
        _messages(client, safeguards={})
        assert _hits(db_session, other) == [] and len(_hits(db_session, mine)) == 1

    def test_a_disabled_rule_matches_nothing(self, client, db_session, test_user):
        rule = _rule(db_session, enabled=False)
        _messages(client, safeguards={})
        assert _hits(db_session, rule) == []

    def test_at_most_one_write_per_window(self, client, db_session, test_user):
        _rule(db_session)
        with patch.object(client_rules, "_record", wraps=client_rules._record) as rec:
            for _ in range(4):
                _messages(client, safeguards={})
        assert rec.call_count == 1

    def test_a_broken_store_never_fails_the_request(self, client, db_session, test_user):
        _rule(db_session)
        with patch.object(client_rules, "_record", side_effect=RuntimeError("db down")):
            assert _messages(client, safeguards={})[0].status_code == 200

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
    def test_first_matching_request_of_the_day_carries_the_message(self, client, db_session, test_user):
        _rule(db_session, action="remind")
        first, post = _messages(client, safeguards={})
        assert first.status_code == 400
        error = first.json()
        assert error["type"] == "error" and "Auto mode needs a setting" in error["error"]["message"]
        assert "send your message again" in error["error"]["message"]
        post.assert_not_called()
        assert _messages(client, safeguards={})[0].status_code == 200   # the retry goes through

    def test_one_reminder_a_day_across_workers(self, client, db_session, test_user):
        _rule(db_session, action="remind")
        assert _messages(client, safeguards={})[0].status_code == 400
        client_rules.reset()            # another worker: no memory of the first reminder
        client_rules.refresh()
        assert _messages(client, safeguards={})[0].status_code == 200

    def test_notice_rules_never_fail_a_request(self, client, db_session, test_user):
        _rule(db_session, action="notice")
        assert _messages(client, safeguards={})[0].status_code == 200

    def test_app_accounts_are_never_reminded(self, client, db_session):
        db_session.add(User(username="app_batch", password_hash="x", api_key="sk-appkey", daily_limit_usd=10.0))
        db_session.commit()
        rule = _rule(db_session, action="remind")
        assert _messages(client, key="sk-appkey", safeguards={})[0].status_code == 200
        assert [h.username for h in _hits(db_session, rule)] == ["app_batch"]   # still recorded


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

    def test_matching_account_sees_notice_patch_and_download(self, client, db_session, test_user):
        rule = _rule(db_session)
        _hit(db_session, rule, "testuser", client_version="2.1.280")
        body = self._page(client, test_user)
        assert f'id="clientNotice-{rule.id}"' in body and "Auto mode needs a setting" in body
        assert "CLAUDE_CODE_AUTO_MODE_SERVER" in body
        assert f"/dashboard/client-rules/{rule.id}/patch.bat" in body

    def test_the_patch_follows_the_accounts_access(self, client, db_session, test_user):
        rule = _rule(db_session, patch='{"MODEL": "local-m"}', patch_azure='{"MODEL": "azure-m"}', patch_aws='{"MODEL": "claude-m"}')
        _hit(db_session, rule, "testuser")
        headers = web_auth_header(sub=test_user.username)
        url = f"/dashboard/client-rules/{rule.id}/patch.bat"

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
        rule = _rule(db_session)
        _hit(db_session, rule, "somebody-else")
        assert "data-client-notice" not in self._page(client, test_user)

    def test_notice_clears_after_the_window_or_when_the_rule_is_off(self, client, db_session, test_user):
        rule = _rule(db_session)
        hit = _hit(db_session, rule, "testuser")
        hit.last_seen_at = _now() - timedelta(days=client_rules.WINDOW_DAYS + 1)
        db_session.add(hit)
        db_session.commit()
        assert "data-client-notice" not in self._page(client, test_user)
        hit.last_seen_at = _now()
        rule.enabled = False
        db_session.add_all([hit, rule])
        db_session.commit()
        assert "data-client-notice" not in self._page(client, test_user)

    def test_message_is_escaped(self, client, db_session, test_user):
        rule = _rule(db_session, title="<script>alert(1)</script>")
        _hit(db_session, rule, "testuser")
        assert "<script>alert(1)</script>" not in self._page(client, test_user)

    def test_patch_download_only_for_an_account_that_matched(self, client, db_session, test_user):
        rule = _rule(db_session)
        url = f"/dashboard/client-rules/{rule.id}/patch.bat"
        headers = web_auth_header(sub=test_user.username)
        assert client.get(url, headers=headers).status_code == 404
        _hit(db_session, rule, "testuser")
        resp = client.get(url, headers=headers)
        assert resp.status_code == 200 and "attachment" in resp.headers["content-disposition"]
        assert b"GW-PATCH-BEGIN" in resp.content and b"\r\n" in resp.content

    def test_no_download_without_a_patch(self, client, db_session, test_user):
        rule = _rule(db_session, patch="")
        _hit(db_session, rule, "testuser")
        headers = web_auth_header(sub=test_user.username)
        assert client.get(f"/dashboard/client-rules/{rule.id}/patch.bat", headers=headers).status_code == 404
        assert "patch.bat" not in self._page(client, test_user)


class TestAdminRules:
    def test_add_edit_delete(self, client, db_session, admin_user, test_user):
        resp = client.post("/admin/client-rules", data=_FORM, headers=_admin(admin_user))
        assert resp.status_code == 200, resp.text
        rule_id = resp.json()["id"]
        rule = db_session.get(ClientRule, rule_id)
        assert rule.created_by == "admin" and json.loads(rule.patch) == _ENV and rule.enabled

        _messages(client, safeguards={})          # the new rule is live in this worker at once
        assert len(_hits(db_session, rule)) == 1

        edited = {**_FORM, "enabled": "off", "version_below": "v2.1.302", "auto_email": "on", "email_repeat_days": "7"}
        assert client.post(f"/admin/client-rules/{rule_id}", data=edited, headers=_admin(admin_user)).status_code == 200
        db_session.expire_all()
        rule = db_session.get(ClientRule, rule_id)
        assert (rule.enabled, rule.version_below, rule.auto_email, rule.email_repeat_days) == (False, "2.1.302", True, 7)

        assert client.post(f"/admin/client-rules/{rule_id}/delete", headers=_admin(admin_user)).status_code == 200
        db_session.expire_all()
        assert db_session.get(ClientRule, rule_id) is None
        assert db_session.exec(select(ClientRuleHit)).all() == []

    @pytest.mark.parametrize("change, message", [
        ({"body_field": ""}, "at least one condition"),
        ({"patch": "{broken"}, "not valid JSON"),
        ({"patch": "permissions.allow=x"}, "not an environment variable"),
        ({"patch_aws": "just words"}, "NAME=value"),
        ({"usernames": "nobody-here"}, "Unknown account"),
        ({"version_below": "latest"}, "Version"),
        ({"action": "block"}, "Action"),
        ({"title": ""}, "title"),
        ({"body_field": "a b"}, "field"),
    ])
    def test_bad_input_is_refused(self, client, db_session, admin_user, change, message):
        resp = client.post("/admin/client-rules", data={**_FORM, **change}, headers=_admin(admin_user))
        assert resp.status_code == 400 and message in resp.json()["detail"]
        assert db_session.exec(select(ClientRule)).all() == []

    def test_only_admins(self, client, test_user):
        headers = {**web_auth_header(sub=test_user.username), "Accept": "application/json"}
        assert client.post("/admin/client-rules", data=_FORM, headers=headers).status_code in (401, 403)

    def test_admin_page_shows_rules_versions_and_attention(self, client, db_session, admin_user, test_user):
        rule = _rule(db_session)
        _hit(db_session, rule, "testuser", client_version="2.1.280")
        db_session.add(ClientVersion(username="testuser", client="claude-code", version="2.1.280", first_seen_at=_now(), last_seen_at=_now()))
        db_session.commit()
        body = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        assert 'id="client-rules"' in body and f'id="client-rule-{rule.id}"' in body
        assert 'id="client-versions"' in body and "2.1.280" in body
        assert 'id="clientRuleAttention"' in body and "Auto mode · 1 account" in body
        assert "?q=testuser#users" in body

    def test_admin_can_preview_each_audiences_script(self, client, db_session, admin_user):
        rule = _rule(db_session, patch="", patch_aws='{"MODEL": "claude-m"}')
        headers = web_auth_header(sub=admin_user.username, scopes=["admin"])
        url = f"/admin/client-rules/{rule.id}/patch.bat"
        assert client.get(url, params={"audience": "aws"}, headers=headers).status_code == 200
        assert client.get(url, params={"audience": "local"}, headers=headers).status_code == 404   # nothing for them
        assert client.get(url, params={"audience": "everyone"}, headers=headers).status_code == 400

    def test_variants_are_saved_and_shown_back_as_lines(self, client, db_session, admin_user):
        form = {**_FORM, "patch": "SERVER=0\n-OLD", "patch_azure": '{"MODEL": "azure-m"}', "patch_aws": "MODEL=claude-m"}
        rule_id = client.post("/admin/client-rules", data=form, headers=_admin(admin_user)).json()["id"]
        rule = db_session.get(ClientRule, rule_id)
        assert json.loads(rule.patch) == {"SERVER": "0", "OLD": None}
        assert json.loads(rule.patch_azure) == {"MODEL": "azure-m"} and json.loads(rule.patch_aws) == {"MODEL": "claude-m"}
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
    def test_test_mail_goes_to_the_admin_by_default(self, client, admin_user, mail_on):
        resp = client.post("/admin/client-rules/mail-test", data={**_FORM, "body_field": ""}, headers=_admin(admin_user))
        assert resp.status_code == 200 and resp.json() == {"ok": True, "to": "admin@example.test", "detail": ""}
        [(to, subject, body)] = mail_on
        assert to == "admin@example.test" and subject.startswith("[TEST] ") and "Fix auto mode" in body
        assert "https://gw.example.test/dashboard" in body and "CLAUDE_CODE_AUTO_MODE_SERVER" in body

    def test_test_mail_address_and_audience_can_be_chosen(self, client, admin_user, mail_on):
        form = {**_FORM, "patch": "MODEL=local-m", "patch_aws": "MODEL=claude-m", "to": "someone@example.test"}
        for audience, model in (("local", "local-m"), ("azure", "local-m"), ("aws", "claude-m")):
            resp = client.post("/admin/client-rules/mail-test", data={**form, "audience": audience}, headers=_admin(admin_user))
            assert resp.json()["ok"] and resp.json()["to"] == "someone@example.test"
            assert mail_on[-1][0] == "someone@example.test" and model in mail_on[-1][2]

    def test_test_mail_stays_inside_the_company_domain(self, client, admin_user, mail_on):
        resp = client.post("/admin/client-rules/mail-test", data={**_FORM, "to": "x@elsewhere.test"}, headers=_admin(admin_user))
        assert resp.status_code == 400 and mail_on == []

    def test_test_mail_reports_a_failure(self, client, admin_user, mail_on):
        with patch.object(mailer, "_send_sync", side_effect=TimeoutError("timed out")):
            resp = client.post("/admin/client-rules/mail-test", data=_FORM, headers=_admin(admin_user))
        assert resp.json()["ok"] is False and resp.json()["detail"] == "TimeoutError: timed out"

    def test_mail_is_refused_while_unconfigured(self, client, db_session, admin_user):
        rule = _rule(db_session)
        assert client.post("/admin/client-rules/mail-test", data=_FORM, headers=_admin(admin_user)).status_code == 400
        assert client.post(f"/admin/client-rules/{rule.id}/mail", headers=_admin(admin_user)).status_code == 400

    def test_manual_send_reaches_matching_accounts_once(self, client, db_session, admin_user, test_user, mail_on):
        db_session.add(User(username="app_batch", password_hash="x", api_key="sk-appkey", daily_limit_usd=1.0))
        db_session.commit()
        rule = _rule(db_session)
        for name in ("testuser", "admin", "app_batch", "deleted-account"):
            _hit(db_session, rule, name)
        url = f"/admin/client-rules/{rule.id}/mail"
        resp = client.post(url, data={"only_unmailed": "on"}, headers=_admin(admin_user))
        assert resp.json() == {"ok": True, "queued": 2}       # no app accounts, no accounts that are gone
        assert sorted(to for to, _, _ in mail_on) == ["admin@example.test", "testuser@example.test"]
        assert all(h.mailed_at for h in _hits(db_session, rule) if h.username in ("testuser", "admin"))

        assert client.post(url, data={"only_unmailed": "on"}, headers=_admin(admin_user)).json()["queued"] == 0
        assert len(mail_on) == 2
        assert client.post(url, data={"only_unmailed": "off"}, headers=_admin(admin_user)).json()["queued"] == 2
        assert len(mail_on) == 4

    def test_each_recipient_gets_the_patch_for_their_access(self, db_session, test_user, admin_user, mail_on):
        test_user.can_use_azure = test_user.can_use_bedrock = False
        db_session.add(test_user)
        db_session.commit()
        rule = _rule(db_session, patch='{"MODEL": "local-m"}', patch_aws='{"MODEL": "claude-m"}')
        _hit(db_session, rule, "testuser")     # local-only
        _hit(db_session, rule, "admin")        # admins have every backend
        assert client_rules.send_rule_mail(rule.id) == {"sent": 2, "failed": 0}
        bodies = {to: body for to, _, body in mail_on}
        assert "local-m" in bodies["testuser@example.test"] and "claude-m" not in bodies["testuser@example.test"]
        assert "claude-m" in bodies["admin@example.test"]

    def test_a_failed_send_is_recorded_on_the_account(self, db_session, test_user, mail_on):
        rule = _rule(db_session)
        _hit(db_session, rule, "testuser")
        with patch.object(mailer, "_send_sync", side_effect=TimeoutError("timed out")):
            assert client_rules.send_rule_mail(rule.id) == {"sent": 0, "failed": 1}
        [hit] = _hits(db_session, rule)
        assert hit.mailed_at is None and hit.mail_error == "TimeoutError: timed out" and hit.mail_attempted_at

    def test_mail_escapes_what_the_admin_wrote(self, mail_on):
        draft = SimpleNamespace(name="n", title="<b>t</b>", message="a\n<i>b</i>", email_subject="")
        subject, body = client_rules.render_mail(draft, "u<1>", {"A": "<x>"})
        assert "<b>t</b>" not in body and "<i>b</i>" not in body and "<x>" not in body and "u<1>" not in body
        assert "a<br>" in body and subject.endswith("<b>t</b>")


class TestAutoMail:
    def test_only_rules_that_ask_for_it(self, db_session, test_user, mail_on):
        quiet = _rule(db_session, name="quiet")
        loud = _rule(db_session, name="loud", auto_email=True)
        _hit(db_session, quiet, "testuser")
        _hit(db_session, loud, "testuser")
        assert "sent=1" in client_rules.run_auto_mail(db_session)
        assert len(mail_on) == 1 and "loud" in mail_on[0][2]

    def test_once_unless_a_repeat_is_set(self, db_session, test_user, mail_on):
        rule = _rule(db_session, auto_email=True)
        _hit(db_session, rule, "testuser")
        now = _now()
        client_rules.run_auto_mail(db_session, now)
        client_rules.run_auto_mail(db_session, now + timedelta(days=30))
        assert len(mail_on) == 1

        rule.email_repeat_days = 7
        db_session.add(rule)
        db_session.commit()
        client_rules.run_auto_mail(db_session, now + timedelta(days=6))
        assert len(mail_on) == 1
        for hit in _hits(db_session, rule):          # still matching a week later
            hit.last_seen_at = now + timedelta(days=8)
            db_session.add(hit)
        db_session.commit()
        client_rules.run_auto_mail(db_session, now + timedelta(days=8))
        assert len(mail_on) == 2

    def test_accounts_outside_the_window_are_left_alone(self, db_session, test_user, mail_on):
        rule = _rule(db_session, auto_email=True)
        hit = _hit(db_session, rule, "testuser")
        hit.last_seen_at = _now() - timedelta(days=client_rules.WINDOW_DAYS + 1)
        db_session.add(hit)
        db_session.commit()
        client_rules.run_auto_mail(db_session)
        assert mail_on == []

    def test_a_failure_is_retried_later_not_every_run(self, db_session, test_user, mail_on):
        rule = _rule(db_session, auto_email=True)
        _hit(db_session, rule, "testuser")
        now = _now()
        with patch.object(mailer, "_send_sync", side_effect=TimeoutError("x")) as failing:
            client_rules.run_auto_mail(db_session, now)
            client_rules.run_auto_mail(db_session, now + timedelta(minutes=10))
            assert failing.call_count == 1
        client_rules.run_auto_mail(db_session, now + timedelta(seconds=client_rules.MAIL_RETRY_S + 60))
        assert len(mail_on) == 1
        assert _hits(db_session, rule)[0].mail_error == ""

    def test_job_is_idle_while_mail_is_unconfigured(self, db_session, test_user):
        rule = _rule(db_session, auto_email=True)
        _hit(db_session, rule, "testuser")
        assert client_rules.run_auto_mail(db_session) == "mail not configured"
        assert _hits(db_session, rule)[0].mail_attempted_at is None

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
