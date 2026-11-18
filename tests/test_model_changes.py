"""Model changes (app/services/model_changes.py): telling accounts that a model
is being replaced.

An admin announces a change: a notice, the renames (old model name, new
model name, and the tier each is for) and when it is shown (from, until).
Every account the renames are for sees it on its dashboard between the two,
with a script that replaces the names in its Claude Code
settings, and can be mailed about it through the paced mail job.

It is its own flow, apart from the client-rule notices: nothing was detected,
so a change is not a violation. Its own panel, its own script, its own
endpoint (``GET /v1/model-changes``).
"""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlmodel import select

from app.core.timeutil import LOCAL_TZ
from app.models.schema import ClientMailLog, ClientViolation, ModelChange, ModelChangeState, User
from app.services import client_mail, client_patch, mailer, model_changes
from tests.conftest import auth_header, web_auth_header


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _at(days: float = 30) -> str:
    """A time ``days`` from now, as the form's ``datetime-local`` value (Asia/Taipei)."""
    return (datetime.now(LOCAL_TZ) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")


def _rows(*rows: tuple[str, str, str], **kw) -> dict:
    """The admin form as the page posts it: one row per model (old name, new
    name, tier)."""
    rows = rows or (("old-llm", "test-llm", "everyone"),)
    form = {
        "title": "test-llm is being replaced", "message": "It is switched off next month.", "show_until": _at(),
        "old_model": [r[0] for r in rows], "new_model": [r[1] for r in rows], "tier": [r[2] for r in rows],
    }
    form.update(kw)
    return form


def _form(**kw) -> dict:
    form = {"title": "test-llm is being replaced", "message": "It is switched off next month.",
            "renames": "old-llm -> test-llm", "show_until": _at()}
    form.update(kw)
    return form


def _change(db_session, **kw) -> ModelChange:
    """A change saved the way the admin form saves it."""
    return model_changes.save(db_session, model_changes.clean_fields(_form(**kw)), by="admin")


def _person(db_session, name: str, **kw) -> User:
    user = User(username=name, password_hash="x", api_key=f"sk-{name}", daily_limit_usd=1.0, **kw)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _admin(admin: User) -> dict:
    return {**web_auth_header(sub=admin.username, scopes=["admin"]), "Accept": "application/json"}


def _payload(script: str) -> dict:
    """What a downloaded script carries (base64 in the script)."""
    return json.loads(base64.b64decode(re.search(r"Buffer\.from\('([^']+)'", script).group(1)))


def _states(db_session, change: ModelChange) -> dict[str, ModelChangeState]:
    db_session.expire_all()
    return {
        row.username: row
        for row in db_session.exec(select(ModelChangeState).where(ModelChangeState.change_id == change.id)).all()
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


class TestRenames:
    """The rename half of a patch (client_patch): text, storage, and what it
    does to a settings file."""

    def test_one_rename_per_line(self):
        assert client_patch.parse_renames("") == {}
        text = "# retiring\nold-a -> new-a\n old-b=>new-b \nold-c = new-c\nold-d → new-d\n"
        assert client_patch.parse_renames(text) == {"old-a": "new-a", "old-b": "new-b", "old-c": "new-c", "old-d": "new-d"}
        assert client_patch.parse_renames("org/model:v1.2 -> us.vendor.model-2@x") == {"org/model:v1.2": "us.vendor.model-2@x"}

    @pytest.mark.parametrize("bad", [
        "just-a-name", "old ->", "-> new", "two words -> new", "old -> new[1m]", 'old -> "new"',
        "same -> same", "old -> a\nold -> b", "old -> $(id)", "old -> a;b",
    ])
    def test_what_is_not_a_rename(self, bad):
        with pytest.raises(ValueError):
            client_patch.parse_renames(bad)

    def test_too_many_is_refused(self):
        with pytest.raises(ValueError):
            client_patch.parse_renames("\n".join(f"old-{i} -> new-{i}" for i in range(21)))

    def test_stored_renames_round_trip_to_the_admins_lines(self):
        renames = client_patch.parse_renames("old-a -> new-a\nold-b -> new-b")
        assert client_patch.rename_lines(client_patch.load_renames(client_patch.dump_renames(renames))) == "old-a -> new-a\nold-b -> new-b"
        assert client_patch.dump_renames({}) == "" and client_patch.load_renames("{broken") == {}
        assert client_patch.load_renames('{"ok": "fine", "bad name": "x", "n": 3}') == {"ok": "fine"}

    def test_only_a_models_name_changes_and_only_where_it_is_exactly_that(self):
        settings = {
            "model": "old", "theme": "old",
            "env": {
                "ANTHROPIC_MODEL": "old", "ANTHROPIC_SMALL_FAST_MODEL": "old[1m]", "CLAUDE_CODE_SUBAGENT_MODEL": "mine",
                "anthropic_default_haiku_model": "old", "ANTHROPIC_BASE_URL": "old", "NOTE_MODEL": "older", "N_MODEL": 3,
            },
            "permissions": {"allow": ["old"]},
        }
        assert client_patch.apply_renames(settings, {"old": "new"}) == 4
        assert settings == {
            "model": "new", "theme": "old",
            "env": {
                "ANTHROPIC_MODEL": "new", "ANTHROPIC_SMALL_FAST_MODEL": "new[1m]", "CLAUDE_CODE_SUBAGENT_MODEL": "mine",
                "anthropic_default_haiku_model": "new", "ANTHROPIC_BASE_URL": "old", "NOTE_MODEL": "older", "N_MODEL": 3,
            },
            "permissions": {"allow": ["old"]},
        }

    def test_nothing_is_added_and_renames_do_not_chain(self):
        settings = {"permissions": {}}
        assert client_patch.apply_renames(settings, {"old": "new"}) == 0
        assert settings == {"permissions": {}}                     # no "model", no "env" appears
        chained = {"model": "a", "env": {"ANTHROPIC_MODEL": "b"}}
        client_patch.apply_renames(chained, {"a": "b", "b": "c"})
        assert chained == {"model": "b", "env": {"ANTHROPIC_MODEL": "c"}}

    def test_renames_come_before_the_variables(self):
        settings = {"env": {"ANTHROPIC_MODEL": "old", "KEEP": "1"}}
        client_patch.apply_patch(settings, {"ANTHROPIC_MODEL": "set-by-a-notice", "X": "1"}, {"old": "new"})
        assert settings == {"env": {"ANTHROPIC_MODEL": "set-by-a-notice", "KEEP": "1", "X": "1"}}
        bare = {"model": "old"}
        assert client_patch.apply_patch(bare, {}, {"old": "new"}) == {"model": "new"}   # no "env" block for nothing

    BEFORE = [
        None,
        "",
        json.dumps({"model": "old"}),
        "﻿" + json.dumps({"model": "other", "env": {"ANTHROPIC_MODEL": "old[1m]", "KEEP": "1"}}),
        json.dumps({
            "model": "old", "theme": "old",
            "env": {"ANTHROPIC_MODEL": "old", "ANTHROPIC_SMALL_FAST_MODEL": "second", "CLAUDE_CODE_SUBAGENT_MODEL": "mine",
                    "ANTHROPIC_BASE_URL": "old", "A": "é中文", "OLD": "x"},
            "permissions": {"allow": ["Bash(ls:*)", "old"], "nested": [[1, 2], [], {"k": None}]},
            "num": 1.5, "t": True, "nul": None, "emptyobj": {},
        }, indent=2),
    ]
    RENAMES = {"old": "new-model", "second": "org/second:2"}

    @pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
    @pytest.mark.parametrize("env", [{}, {"X": "1", "OLD": None}])
    @pytest.mark.parametrize("before", BEFORE)
    def test_the_windows_scripts_node_part_does_what_apply_patch_does(self, tmp_path, before, env):
        result = self._run_bat_node(tmp_path, env, self.RENAMES, before)
        assert result.returncode == 0, result.stdout + result.stderr
        self._same_as_reference(tmp_path, before, env)

    @pytest.mark.skipif(shutil.which("sh") is None, reason="sh not installed")
    @pytest.mark.parametrize("runtime", ["node", "python3"])
    @pytest.mark.parametrize("env", [{}, {"X": "1", "OLD": None}])
    @pytest.mark.parametrize("before", BEFORE)
    def test_the_shell_script_does_what_apply_patch_does(self, tmp_path, runtime, before, env):
        result = self._run_sh(tmp_path, env, self.RENAMES, before, runtime)
        assert result.returncode == 0, result.stdout + result.stderr
        self._same_as_reference(tmp_path, before, env)
        assert "Model names replaced:" in result.stdout

    @pytest.mark.skipif(shutil.which("sh") is None, reason="sh not installed")
    @pytest.mark.parametrize("runtime", ["node", "python3"])
    def test_a_file_it_cannot_read_is_left_alone(self, tmp_path, runtime):
        before = '{"model": "old", "env": "a string"}'
        result = self._run_sh(tmp_path, {}, self.RENAMES, before, runtime)
        assert result.returncode != 0
        assert (tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8") == before

    def test_the_powershell_part_gets_the_same_payload(self):
        bat = client_patch.render_patch_bat("MC1", {"X": "1"}, {"old": "new"})
        powershell = bat.split("#GW-PATCH-BEGIN#")[1]
        payload = re.search(r"FromBase64String\('([^']+)'\)", powershell).group(1)
        assert json.loads(base64.b64decode(payload)) == {"env": {"X": "1"}, "models": {"old": "new"}} == _payload(bat)
        assert "GwRenamed" in powershell and "$patch['models']" in powershell

    def _same_as_reference(self, home: Path, before: str | None, env: dict) -> None:
        raw = (home / ".claude" / "settings.json").read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        was = json.loads(before.lstrip("﻿")) if before else {}
        assert json.loads(raw.decode("utf-8")) == client_patch.apply_patch(copy.deepcopy(was), env, self.RENAMES)
        assert (home / ".claude" / "settings.json.bak").exists() == (before is not None)

    @staticmethod
    def _write(home: Path, before: str | None) -> None:
        if before is not None:
            (home / ".claude").mkdir()
            (home / ".claude" / "settings.json").write_text(before, encoding="utf-8")

    def _run_bat_node(self, home: Path, env: dict, renames: dict, before: str | None) -> subprocess.CompletedProcess:
        """Run the .bat's own Node line (cmd.exe aside) with ``home`` as the user's home."""
        self._write(home, before)
        bat = client_patch.render_patch_bat("MC1", env, renames)
        script = home / "patch.bat"
        script.write_bytes(bat.encode("utf-8"))
        [line] = [ln for ln in bat.split("\r\n") if ln.startswith('node -e "')]
        run_env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "GW_PATCH_SELF": str(script)}
        return subprocess.run(["node", "-e", line[len('node -e "'):-1]], env=run_env, capture_output=True, text=True, timeout=60)

    def _run_sh(self, home: Path, env: dict, renames: dict, before: str | None, runtime: str) -> subprocess.CompletedProcess:
        """Run the .sh as a user would, with only ``runtime`` on the PATH."""
        if shutil.which(runtime) is None:
            pytest.skip(f"{runtime} not installed")
        self._write(home, before)
        script = home / "claude-code-patch.sh"
        script.write_text(client_patch.render_patch_sh("MC1", env, script.name, renames), encoding="utf-8")
        bindir = home / "bin"
        bindir.mkdir()
        (bindir / runtime).symlink_to(shutil.which(runtime))
        run_env = {"HOME": str(home), "PATH": str(bindir), "LANG": os.environ.get("LANG", "C.UTF-8")}
        return subprocess.run([shutil.which("sh"), str(script)], env=run_env, capture_output=True, text=True, timeout=60)


class TestWhoIsTold:
    """A change is for the accounts its renames are for: everyone, or a tier."""

    def test_each_tier_gets_its_own_renames(self):
        change = SimpleNamespace(
            renames='{"old": "local-new", "everyone": "x"}', renames_azure='{"old": "azure-new"}',
            renames_aws='{"old": "aws-new", "aws-old": "aws-next"}',
        )
        assert model_changes.effective_renames(change, False, False) == {"old": "local-new", "everyone": "x"}
        assert model_changes.effective_renames(change, True, False)["old"] == "azure-new"
        assert model_changes.effective_renames(change, False, True) == {"old": "aws-new", "everyone": "x", "aws-old": "aws-next"}
        assert model_changes.effective_renames(change, True, True)["old"] == "aws-new"      # AWS wins over Azure

    def test_a_change_for_everyone(self, db_session):
        local = _person(db_session, "local")
        change = _change(db_session)
        [item] = model_changes.for_account(db_session, local)
        assert item["ref"] == f"MC{change.id}" and item["renames"] == {"old-llm": "test-llm"} and item["applied"] is False
        assert model_changes.audience_label(change) == "everyone"

    def test_a_change_for_one_tier_only(self, db_session):
        local = _person(db_session, "local")
        azure = _person(db_session, "azure", can_use_azure=True)
        aws = _person(db_session, "aws", can_use_bedrock=True)
        change = _change(db_session, renames="", renames_azure="azure-old -> azure-gpt-4")
        assert model_changes.for_account(db_session, local) == []
        assert model_changes.for_account(db_session, aws) == []
        assert model_changes.for_account(db_session, azure)[0]["renames"] == {"azure-old": "azure-gpt-4"}
        assert model_changes.audience(db_session, change) == ["azure"]
        assert model_changes.audience_label(change) == "Azure accounts"
        assert model_changes.renames_for(db_session, local) == {}

    def test_an_admin_on_the_web_ui_sees_every_tier(self, db_session):
        admin = _person(db_session, "boss")
        admin.is_admin = True       # as get_web_user sets it from the token, for one request
        _change(db_session, renames="", renames_aws="bedrock-old -> bedrock-nova")
        assert model_changes.for_account(db_session, admin)[0]["renames"] == {"bedrock-old": "bedrock-nova"}

    def test_it_is_shown_until_the_time_the_admin_set(self, db_session):
        user = _person(db_session, "local")
        until = _at(3)
        change = _change(db_session, show_until=until)
        assert model_changes.local_input(change.show_until) == until
        assert model_changes.for_account(db_session, user, change.show_until - timedelta(seconds=1))
        assert model_changes.for_account(db_session, user, change.show_until) == []
        assert model_changes.renames_for(db_session, user, change.show_until) == {}

    def test_it_is_not_shown_before_it_opens(self, db_session):
        user = _person(db_session, "local")
        change = _change(db_session, show_from=_at(1), show_until=_at(2))
        assert model_changes.state_of(change) == "scheduled" and not model_changes.is_showing(change)
        assert model_changes.for_account(db_session, user) == []
        assert model_changes.request_mail(db_session, change) == 0          # nobody is mailed about what is not open
        found = model_changes.overview(db_session)
        assert found["showing"] == [] and [c.id for c in found["scheduled"]] == [change.id]
        opened = change.show_from + timedelta(seconds=1)
        assert model_changes.state_of(change, opened) == "showing"
        assert model_changes.for_account(db_session, user, opened)[0]["renames"] == {"old-llm": "test-llm"}
        model_changes.end(db_session, change)                               # cancelled: it never opens
        assert model_changes.state_of(change, opened) == "ended"

    def test_ending_it_takes_it_away_at_once(self, db_session):
        user = _person(db_session, "local")
        change = _change(db_session)
        model_changes.end(db_session, change, by="admin")
        assert model_changes.for_account(db_session, user) == []
        found = model_changes.overview(db_session)
        assert found["showing"] == [] and [c.id for c in found["ended"]] == [change.id]

    def test_i_have_switched_folds_it_but_it_stays(self, db_session):
        user = _person(db_session, "local")
        other = _person(db_session, "other")
        change = _change(db_session)
        assert model_changes.set_applied(db_session, change.id, user) is True
        assert model_changes.for_account(db_session, user)[0]["applied"] is True
        assert model_changes.renames_for(db_session, user) == {"old-llm": "test-llm"}     # the script still has it
        assert model_changes.for_account(db_session, other)[0]["applied"] is False
        assert model_changes.counts(db_session, change)["applied"] == 1

    def test_only_a_change_the_account_is_shown_can_be_marked(self, db_session):
        local = _person(db_session, "local")
        change = _change(db_session, renames="", renames_azure="azure-old -> azure-gpt-4")
        assert model_changes.set_applied(db_session, change.id, local) is False
        assert model_changes.set_applied(db_session, 9999, local) is False
        assert _states(db_session, change) == {}

    def test_several_changes_make_one_set_of_renames(self, db_session):
        user = _person(db_session, "local")
        _change(db_session, renames="old-llm -> test-llm\nold-vlm -> test-vlm")
        _change(db_session, renames="old-llm -> test-vlm")
        assert model_changes.renames_for(db_session, user) == {"old-llm": "test-vlm", "old-vlm": "test-vlm"}   # the later change wins


class TestForm:
    def test_what_a_change_needs(self):
        for bad in (
            _form(title=""), _form(title="x" * 201), _form(message="x" * 4001),
            _form(renames=""), _form(renames="not a rename"),
            _form(show_until=""), _form(show_until="soon"), _form(show_until=_at(-1)), _form(show_until=_at(400)),
            _form(show_from="soon"), _form(show_from=_at(3), show_until=_at(2)), _form(show_from=_at(2), show_until=_at(2)),
        ):
            with pytest.raises(ValueError):
                model_changes.clean_fields(bad)

    def test_the_new_model_must_be_one_its_accounts_can_use(self):
        for bad in (
            _form(renames="old -> no-such-model"),
            _form(renames="old -> azure-gpt-4"),                    # local-only accounts cannot use an Azure model
            _form(renames="old -> bedrock-claude"),
            _form(renames="", renames_azure="old -> bedrock-claude"),
            _form(renames="", renames_aws="old -> azure-gpt-4"),
        ):
            with pytest.raises(ValueError):
                model_changes.clean_fields(bad)
        ok = model_changes.clean_fields(_form(
            renames="gone-model -> test-llm", renames_azure="gone-model -> azure-gpt-4", renames_aws="x -> test-vlm",
        ))
        assert json.loads(ok["renames_azure"]) == {"gone-model": "azure-gpt-4"}

    def test_times_are_taipei_and_an_empty_start_is_now(self, db_session):
        values = model_changes.clean_fields(_form(show_from="", show_until="2099-01-02T08:30"), now=datetime(2098, 12, 1))
        assert values["show_from"] is None
        assert values["show_until"] == datetime(2099, 1, 2, 0, 30)       # 08:30 in Taipei, stored as naive UTC
        change = _change(db_session)
        assert change.show_from <= _now() and model_changes.is_showing(change)
        started = change.show_from
        change = model_changes.save(db_session, model_changes.clean_fields(_form(title="Reworded")), by="admin", change=change)
        assert change.show_from == started                               # an edit without a start keeps the one it had
        change = model_changes.save(
            db_session, model_changes.clean_fields(_form(show_from="2020-01-01T00:00")), by="admin", change=change,
        )
        assert change.show_from == datetime(2019, 12, 31, 16, 0) and model_changes.is_showing(change)

    def test_the_forms_rows_are_the_three_tiers(self):
        boxes = model_changes.rows_to_boxes([
            ("old-llm", "test-llm", "everyone"), ("", "", "azure"), (" old-llm ", "azure-gpt-4", "Azure"),
            ("bedrock-old", "bedrock-nova", "aws"), ("old-vlm", "test-vlm", ""),
        ])
        assert boxes == {
            "renames": "old-llm -> test-llm\nold-vlm -> test-vlm", "renames_azure": "old-llm -> azure-gpt-4",
            "renames_aws": "bedrock-old -> bedrock-nova",
        }
        values = model_changes.clean_fields(_form(**boxes))
        assert model_changes.rows(SimpleNamespace(**values)) == [
            {"old": "old-llm", "new": "test-llm", "tier": "everyone"}, {"old": "old-vlm", "new": "test-vlm", "tier": "everyone"},
            {"old": "old-llm", "new": "azure-gpt-4", "tier": "azure"}, {"old": "bedrock-old", "new": "bedrock-nova", "tier": "aws"},
        ]
        for bad in ([("old-llm", "", "everyone")], [("", "test-llm", "everyone")], [("old-llm", "test-llm", "vip")]):
            with pytest.raises(ValueError):
                model_changes.rows_to_boxes(bad)


class TestDashboard:
    def test_the_change_is_on_the_dashboard_with_its_renames(self, client, db_session, test_user):
        change = _change(db_session, title="Retiring <old>", renames="old-llm -> test-llm")
        body = client.get("/dashboard", headers=web_auth_header(sub=test_user.username)).text
        assert 'id="modelChanges"' in body and f"MC{change.id}" in body
        assert "Retiring &lt;old&gt;" in body and "<old>" not in body          # what the admin wrote is escaped
        assert "old-llm" in body and f"until {model_changes.local_time(change.show_until)}" in body
        assert "data-model-change-scope" in body                   # it says it is for Claude Code users
        assert "/dashboard/model-changes/patch.sh" in body

    def test_an_account_it_is_not_for_sees_nothing(self, client, db_session):
        local = _person(db_session, "local")
        _change(db_session, renames="", renames_azure="azure-old -> azure-gpt-4")
        body = client.get("/dashboard", headers=web_auth_header(sub=local.username)).text
        assert 'id="modelChanges"' not in body
        assert client.get("/dashboard/model-changes", headers=web_auth_header(sub=local.username)).text.strip() == ""

    def test_its_script_carries_the_renames_of_the_accounts_tier(self, client, db_session, test_user):
        local = _person(db_session, "local")
        change = _change(db_session, renames="old-llm -> test-llm", renames_azure="azure-old -> azure-gpt-4")
        for fmt in ("bat", "sh"):
            resp = client.get(f"/dashboard/model-changes/patch.{fmt}", headers=web_auth_header(sub=test_user.username))
            assert resp.status_code == 200 and f"claude-code-model-change.{fmt}" in resp.headers["content-disposition"]
            assert _payload(resp.text) == {"env": {}, "models": {"old-llm": "test-llm", "azure-old": "azure-gpt-4"}}
            assert f"MC{change.id}" in resp.text
        resp = client.get("/dashboard/model-changes/patch.sh", headers=web_auth_header(sub=local.username))
        assert _payload(resp.text)["models"] == {"old-llm": "test-llm"}          # a local-only account's own
        assert client.get("/dashboard/model-changes/patch.exe", headers=web_auth_header(sub=local.username)).status_code == 404

    def test_it_is_not_a_notice_and_its_script_is_not_the_notices_patch(self, client, db_session, test_user):
        """A model change is its own flow: nothing was detected, so it is not
        among the violations, and neither script carries the other's work."""
        from app.services import client_rules

        change = _change(db_session)
        headers = web_auth_header(sub=test_user.username)
        assert client_rules.notices_for(db_session, test_user) == []
        assert client.get("/dashboard/client-notices", headers=headers).text.strip() == ""
        assert client.get("/dashboard/client-rules/all/patch.sh", headers=headers).status_code == 404
        db_session.add(ClientViolation(rule_id="CC001", username=test_user.username, first_seen_at=_now(), last_seen_at=_now()))
        db_session.commit()
        notices = _payload(client.get("/dashboard/client-rules/all/patch.sh", headers=headers).text)
        assert notices["models"] == {} and notices["env"]["CLAUDE_CODE_AUTO_MODE_SERVER"] == "0"
        assert _payload(client.get("/dashboard/model-changes/patch.sh", headers=headers).text) == {
            "env": {}, "models": {"old-llm": "test-llm"},
        }
        # "I've applied everything" on the notices says nothing about the model change.
        assert client_rules.resolve_all(db_session, test_user.username) == 1
        assert model_changes.for_account(db_session, test_user)[0]["applied"] is False
        assert _states(db_session, change) == {}

    def test_there_is_a_script_only_while_a_change_is_shown(self, client, db_session, test_user):
        headers = web_auth_header(sub=test_user.username)
        assert client.get("/dashboard/model-changes/patch.sh", headers=headers).status_code == 404
        change = _change(db_session)
        assert client.get("/dashboard/model-changes/patch.sh", headers=headers).status_code == 200
        model_changes.end(db_session, change)
        assert client.get("/dashboard/model-changes/patch.sh", headers=headers).status_code == 404
        late = client.get("/dashboard/model-changes/patch.sh", headers={**headers, "Accept": "text/html"}, follow_redirects=False)
        assert late.status_code == 303 and late.headers["location"] == "/dashboard"   # a mail link opened too late

    def test_i_have_switched(self, client, db_session, test_user):
        change = _change(db_session)
        headers = {**web_auth_header(sub=test_user.username), "Accept": "application/json"}
        resp = client.post(f"/dashboard/model-changes/{change.id}/applied", headers=headers)
        assert resp.json() == {"ok": True, "id": change.id}
        panel = client.get("/dashboard/model-changes", headers=web_auth_header(sub=test_user.username)).text
        assert "data-model-change-done" in panel and "data-model-change-applied" not in panel
        assert "/dashboard/model-changes/patch.bat" in panel       # folded, with the script still there
        assert client.get("/dashboard/model-changes/patch.bat", headers=headers).status_code == 200

    def test_a_change_that_is_not_the_accounts_is_a_404(self, client, db_session):
        local = _person(db_session, "local")
        change = _change(db_session, renames="", renames_azure="azure-old -> azure-gpt-4")
        headers = {**web_auth_header(sub=local.username), "Accept": "application/json"}
        assert client.post(f"/dashboard/model-changes/{change.id}/applied", headers=headers).status_code == 404
        assert client.post("/dashboard/model-changes/999/applied", headers=headers).status_code == 404


class TestAdmin:
    def test_announcing_a_change(self, client, db_session, admin_user):
        until = _at(14)
        form = _rows(("old-llm", "test-llm", "everyone"), ("azure-old", "azure-gpt-4", "azure"), ("", "", "everyone"), show_until=until)
        resp = client.post("/admin/model-changes", data=form, headers=_admin(admin_user))
        assert resp.status_code == 200 and resp.json()["ref"].startswith("MC")
        [change] = db_session.exec(select(ModelChange)).all()
        assert change.created_by == admin_user.username
        assert json.loads(change.renames) == {"old-llm": "test-llm"} and json.loads(change.renames_azure) == {"azure-old": "azure-gpt-4"}
        assert change.renames_aws == ""
        assert model_changes.local_input(change.show_until) == until and model_changes.is_showing(change)   # open from now

    def test_scheduling_one_for_later(self, client, db_session, admin_user, test_user):
        form = _rows(show_from=_at(1), show_until=_at(8))
        assert client.post("/admin/model-changes", data=form, headers=_admin(admin_user)).status_code == 200
        [change] = db_session.exec(select(ModelChange)).all()
        assert model_changes.state_of(change) == "scheduled"
        assert 'id="modelChanges"' not in client.get("/dashboard", headers=web_auth_header(sub=test_user.username)).text
        body = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        card = body[body.index('id="model-changes"'):body.index('id="client-setup"')]
        assert "Scheduled" in card and "Cancel it" in card and "Mail can be sent once it opens" in card

    @pytest.mark.parametrize("form", [
        _rows(("old", "no-such-model", "everyone")),
        _rows(("old", "azure-gpt-4", "everyone")),          # an Azure model for accounts without Azure access
        _rows(("old-llm", "", "everyone")),
        _rows(("", "", "everyone")),                        # no model at all
        _rows(("old-llm", "test-llm", "vip")),
        _rows(show_until=""),
        _rows(show_from=_at(9), show_until=_at(8)),
        {**_rows(), "tier": []},                            # rows that do not line up
    ])
    def test_a_refused_form_creates_nothing(self, client, db_session, admin_user, form):
        resp = client.post("/admin/model-changes", data=form, headers=_admin(admin_user))
        assert resp.status_code == 400 and resp.json()["detail"]
        assert db_session.exec(select(ModelChange)).all() == []

    def test_only_admins(self, client, db_session, test_user):
        change = _change(db_session)
        headers = {**web_auth_header(sub=test_user.username), "Accept": "application/json"}
        for path in ("/admin/model-changes", f"/admin/model-changes/{change.id}", f"/admin/model-changes/{change.id}/end",
                     f"/admin/model-changes/{change.id}/mail"):
            assert client.post(path, data=_rows(), headers=headers).status_code == 403, path
        assert client.get(f"/admin/model-changes/{change.id}/mail-preview", headers=headers).status_code == 403
        db_session.expire_all()
        assert model_changes.is_showing(db_session.get(ModelChange, change.id))

    def test_editing_and_ending(self, client, db_session, admin_user):
        change = _change(db_session)
        until = _at(60)
        resp = client.post(
            f"/admin/model-changes/{change.id}",
            data=_rows(("old-llm", "test-vlm", "everyone"), title="New wording", show_until=until), headers=_admin(admin_user),
        )
        assert resp.status_code == 200
        db_session.expire_all()
        change = db_session.get(ModelChange, change.id)
        assert change.title == "New wording" and model_changes.local_input(change.show_until) == until
        assert json.loads(change.renames) == {"old-llm": "test-vlm"} and change.updated_by == admin_user.username
        assert client.post(f"/admin/model-changes/{change.id}/end", headers=_admin(admin_user)).status_code == 200
        db_session.expire_all()
        assert model_changes.state_of(db_session.get(ModelChange, change.id)) == "ended"
        resp = client.post(f"/admin/model-changes/{change.id}", data=_rows(), headers=_admin(admin_user))
        assert resp.status_code == 400                               # an ended announcement is not brought back
        assert client.post("/admin/model-changes/999/end", headers=_admin(admin_user)).status_code == 404

    def test_the_card_lists_it_with_how_far_it_got(self, client, db_session, admin_user, test_user):
        change = _change(db_session, title="Retire <b>old</b>", renames="gone-model -> test-llm")
        model_changes.set_applied(db_session, change.id, test_user)
        body = client.get("/admin", headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).text
        card = body[body.index('id="model-changes"'):body.index('id="client-setup"')]
        assert f'id="model-change-{change.id}"' in card and "Retire &lt;b&gt;old&lt;/b&gt;" in card
        assert "for everyone" in card and "1 switched" in card
        assert "gone-model not configured" in card                 # an old name the gateway no longer has
        assert 'id="model-change-new"' in card and 'id="mc-row-template"' in card
        # One row per model: old name, new name, who it is for.
        assert 'name="old_model" value="gone-model"' in card and 'name="new_model" value="test-llm"' in card
        assert '<option value="everyone" selected>Everyone</option>' in card
        assert 'name="show_from"' in card and 'name="show_until"' in card
        assert f"/admin/model-changes/{change.id}/mail-preview?audience=local" in card

    def test_the_admin_can_download_what_each_tier_gets(self, client, db_session, admin_user):
        change = _change(db_session, renames="", renames_azure="azure-old -> azure-gpt-4")
        headers = web_auth_header(sub=admin_user.username, scopes=["admin"])
        resp = client.get(f"/admin/model-changes/{change.id}/patch.sh?audience=azure", headers=headers)
        assert resp.status_code == 200 and _payload(resp.text) == {"env": {}, "models": {"azure-old": "azure-gpt-4"}}
        assert client.get(f"/admin/model-changes/{change.id}/patch.sh?audience=local", headers=headers).status_code == 404
        assert client.get(f"/admin/model-changes/{change.id}/patch.exe?audience=azure", headers=headers).status_code == 404
        assert client.get(f"/admin/model-changes/{change.id}/patch.sh?audience=nobody", headers=headers).status_code == 400


class TestMail:
    def _people(self, db_session, count: int) -> list[str]:
        names = [f"user{i:02d}" for i in range(count)]
        for name in names:
            db_session.add(User(username=name, password_hash="x", api_key=f"sk-{name}", daily_limit_usd=1.0))
        db_session.commit()
        return names

    def test_nobody_is_mailed_until_an_admin_asks(self, db_session, mail_on):
        self._people(db_session, 3)
        _change(db_session)
        assert client_mail.run_auto_mail(db_session) == "sent=0 failed=0 waiting=0"
        assert mail_on == []

    def test_who_is_queued(self, db_session, mail_on):
        _person(db_session, "local")
        _person(db_session, "azure", can_use_azure=True)
        _person(db_session, "switched", can_use_azure=True)
        _person(db_session, "gone", can_use_azure=True, is_disabled=True)
        _person(db_session, "batch-job", can_use_azure=True, is_service_account=True)
        change = _change(db_session, renames="", renames_azure="azure-old -> azure-gpt-4")
        model_changes.set_applied(db_session, change.id, db_session.exec(select(User).where(User.username == "switched")).one())
        assert model_changes.request_mail(db_session, change) == 1
        assert model_changes.due_mail(db_session) == ["azure"]
        assert client_mail.run_auto_mail(db_session) == "sent=1 failed=0 waiting=0"
        [(to, subject, body)] = mail_on
        assert to == "azure@example.test" and change.title in subject

    def test_the_mail_says_what_to_replace_and_links_to_the_one_script(self, db_session, mail_on):
        _person(db_session, "alice", can_use_azure=True)
        change = _change(
            db_session, title="Retire <b>old</b>", message="Line one\nline <two>",
            renames="old-llm -> test-llm", renames_azure="azure-old -> azure-gpt-4",
        )
        model_changes.request_mail(db_session, change)
        client_mail.run_auto_mail(db_session)
        [(to, subject, body)] = mail_on
        assert subject.endswith("Retire <b>old</b>")                # a subject is plain text
        assert "Retire &lt;b&gt;old&lt;/b&gt;" in body and "<b>old</b>" not in body
        assert "Line one<br>line &lt;two&gt;" in body
        assert "old-llm" in body and "azure-gpt-4" in body          # this account's own renames
        assert 'href="https://gw.example.test/dashboard/model-changes/patch.bat"' in body
        assert f"Reference: MC{change.id}" in body and model_changes.local_time(change.show_until) in body
        assert "for Claude Code users only" in body and body.index("Claude Code users only") < body.index("Retire &lt;b&gt;")
        [log] = db_session.exec(select(ClientMailLog)).all()
        assert (log.username, log.address, log.rule_ids, log.kind, log.ok) == ("alice", "alice@example.test", f"MC{change.id}", "model_change", True)
        assert _states(db_session, change)["alice"].mailed_at is not None

    def test_a_click_queues_everyone_and_sends_only_the_first_batch(self, client, db_session, admin_user, mail_on, monkeypatch):
        monkeypatch.setenv("CLIENT_RULE_MAIL_BATCH", "3")
        self._people(db_session, 8 - len(db_session.exec(select(User)).all()))  # 8 people, the fixtures' included
        change = _change(db_session)
        names = model_changes.audience(db_session, change)
        assert len(names) == 8 and admin_user.username in names      # the admin is a person too
        resp = client.post(f"/admin/model-changes/{change.id}/mail", data={"only_unmailed": "on"}, headers=_admin(admin_user))
        assert resp.json() == {"ok": True, "queued": 8, "skipped": [], "batch": 3, "interval_minutes": 10.0}
        assert len(mail_on) == 3
        assert client_mail.run_auto_mail(db_session) == "sent=3 failed=0 waiting=2"
        assert client_mail.run_auto_mail(db_session) == "sent=2 failed=0 waiting=0"
        assert client_mail.run_auto_mail(db_session) == "sent=0 failed=0 waiting=0"
        assert sorted(to for to, _, _ in mail_on) == sorted(f"{n}@example.test" for n in names)   # each exactly once

    def test_it_shares_the_batch_with_the_notices_mail(self, db_session, mail_on, monkeypatch):
        monkeypatch.setenv("CLIENT_RULE_MAIL_BATCH", "3")
        names = self._people(db_session, 5)
        for name in names[:2]:
            db_session.add(ClientViolation(rule_id="CC001", username=name, first_seen_at=_now(), last_seen_at=_now()))
        db_session.commit()
        assert client_mail.request_mail(db_session, "CC001") == 2
        model_changes.request_mail(db_session, _change(db_session))
        assert client_mail.run_auto_mail(db_session) == "sent=3 failed=0 waiting=4"      # 2 notices first, then 1 change
        assert [("things to change" in s or "auto mode" in s) for _, s, _ in mail_on] == [True, True, False]
        assert client_mail.run_auto_mail(db_session) == "sent=3 failed=0 waiting=1"
        assert client_mail.run_auto_mail(db_session) == "sent=1 failed=0 waiting=0"

    def test_one_mail_for_two_changes_queued_together(self, db_session, mail_on):
        _person(db_session, "alice")
        first = _change(db_session, title="First", renames="old-llm -> test-llm")
        second = _change(db_session, title="Second", renames="old-vlm -> test-vlm")
        model_changes.request_mail(db_session, first)
        model_changes.request_mail(db_session, second)
        assert client_mail.run_auto_mail(db_session) == "sent=1 failed=0 waiting=0"
        [(_, subject, body)] = mail_on
        assert "2 model changes" in subject and "1. First" in body and "2. Second" in body
        assert "old-llm" in body and "old-vlm" in body
        assert db_session.exec(select(ClientMailLog)).one().rule_ids == f"MC{first.id},MC{second.id}"

    def test_not_mailed_yet_and_everyone_again(self, db_session, mail_on):
        self._people(db_session, 2)
        change = _change(db_session)
        assert model_changes.request_mail(db_session, change) == 2
        client_mail.run_auto_mail(db_session)
        _person(db_session, "newcomer")
        assert model_changes.request_mail(db_session, change, only_unmailed=True) == 1
        client_mail.run_auto_mail(db_session)
        assert [to for to, _, _ in mail_on][-1] == "newcomer@example.test" and len(mail_on) == 3
        assert model_changes.request_mail(db_session, change, only_unmailed=False) == 3
        client_mail.run_auto_mail(db_session)
        assert len(mail_on) == 6

    def test_whoever_switched_or_whose_change_ended_is_not_mailed(self, db_session, mail_on):
        alice = _person(db_session, "alice")
        _person(db_session, "bob")
        change = _change(db_session)
        model_changes.request_mail(db_session, change)
        model_changes.set_applied(db_session, change.id, alice)      # after being queued
        assert model_changes.due_mail(db_session) == ["bob"]
        model_changes.end(db_session, change)
        assert model_changes.due_mail(db_session) == []
        assert client_mail.run_auto_mail(db_session) == "sent=0 failed=0 waiting=0"
        assert model_changes.request_mail(db_session, change) == 0   # an ended change queues nobody

    def test_a_failed_mail_is_logged_and_tried_again_later(self, db_session, mail_on):
        _person(db_session, "alice")
        change = _change(db_session)
        model_changes.request_mail(db_session, change)
        with patch.object(mailer, "_send_sync", side_effect=RuntimeError("mailbox down")):
            assert client_mail.run_auto_mail(db_session) == "sent=0 failed=1 waiting=0"
        state = _states(db_session, change)["alice"]
        assert state.mail_error and state.mailed_at is None and model_changes.is_queued(state)
        assert db_session.exec(select(ClientMailLog)).one().ok is False
        assert client_mail.run_auto_mail(db_session) == "sent=0 failed=0 waiting=0"      # not hammered
        later = _now() + timedelta(seconds=client_mail.MAIL_RETRY_S + 60)
        assert model_changes.due_mail(db_session, later, client_mail.MAIL_RETRY_S) == ["alice"]
        assert client_mail.send_due(db_session, later)["sent"] == 1
        assert mail_on[-1][0] == "alice@example.test"

    def test_two_senders_never_mail_the_same_account(self, db_session, mail_on):
        _person(db_session, "alice")
        change = _change(db_session)
        model_changes.request_mail(db_session, change)
        state = _states(db_session, change)["alice"]
        seen = (state.id, state.mail_attempted_at)                   # what both senders read
        assert model_changes.claim_mail(db_session, *seen, _now()) is True
        assert model_changes.claim_mail(db_session, *seen, _now()) is False    # the second one lost the claim

    def test_mail_for_a_change_needs_mail_and_a_change_being_shown(self, client, db_session, admin_user, mail_on, monkeypatch):
        change = _change(db_session)
        model_changes.end(db_session, change)
        assert client.post(f"/admin/model-changes/{change.id}/mail", headers=_admin(admin_user)).status_code == 400
        live = _change(db_session)
        monkeypatch.delenv("EXCHANGE_SERVER")
        resp = client.post(f"/admin/model-changes/{live.id}/mail", headers=_admin(admin_user))
        assert resp.status_code == 400 and "not configured" in resp.json()["detail"]

    def test_send_to_named_accounts_only(self, client, db_session, admin_user, mail_on):
        for name in ("alice", "bob", "carol"):
            _person(db_session, name)
        change = _change(db_session)
        resp = client.post(f"/admin/model-changes/{change.id}/mail", data={"to": "alice, bob@example.test"}, headers=_admin(admin_user))
        assert resp.json() == {"ok": True, "queued": 2, "skipped": [], "batch": 20, "interval_minutes": 10.0}
        assert sorted(to for to, _, _ in mail_on) == ["alice@example.test", "bob@example.test"]
        assert "carol" not in _states(db_session, change)
        # Asked for by name: mailed again, though already mailed.
        resp = client.post(f"/admin/model-changes/{change.id}/mail", data={"to": "alice"}, headers=_admin(admin_user))
        assert resp.json()["queued"] == 1 and len(mail_on) == 3

    def test_send_to_refuses_names_it_is_not_for_and_skips_who_switched(self, client, db_session, admin_user, mail_on):
        alice = _person(db_session, "alice", can_use_azure=True)
        _person(db_session, "local")
        _person(db_session, "robot", can_use_azure=True, is_service_account=True)
        change = _change(db_session, renames="", renames_azure="azure-old -> azure-gpt-4")
        for names in ("alice, nobody", "local", "robot"):
            resp = client.post(f"/admin/model-changes/{change.id}/mail", data={"to": names}, headers=_admin(admin_user))
            assert resp.status_code == 400 and "Not accounts this announcement is for" in resp.json()["detail"], names
        assert mail_on == [] and _states(db_session, change) == {}          # one bad name: nobody is queued
        assert client.post(f"/admin/model-changes/{change.id}/mail", data={"to": " , "}, headers=_admin(admin_user)).status_code == 400
        model_changes.set_applied(db_session, change.id, alice)
        resp = client.post(f"/admin/model-changes/{change.id}/mail", data={"to": "alice"}, headers=_admin(admin_user))
        assert resp.json()["queued"] == 0 and resp.json()["skipped"] == ["alice"] and mail_on == []

    def test_the_mail_can_be_previewed_without_sending_it(self, client, db_session, admin_user, mail_on):
        change = _change(db_session, title="Retire <b>old</b>", renames="", renames_azure="azure-old -> azure-gpt-4")
        headers = web_auth_header(sub=admin_user.username, scopes=["admin"])
        resp = client.get(f"/admin/model-changes/{change.id}/mail-preview?audience=azure", headers=headers)
        assert resp.status_code == 200 and "text/html" in resp.headers["content-type"]
        assert "Nothing was sent" in resp.text and "azure-gpt-4" in resp.text and "for Claude Code users only" in resp.text
        assert "Retire &lt;b&gt;old&lt;/b&gt;" in resp.text and "<b>old</b>" not in resp.text
        assert client.get(f"/admin/model-changes/{change.id}/mail-preview?audience=local", headers=headers).status_code == 404
        assert client.get(f"/admin/model-changes/{change.id}/mail-preview?audience=vip", headers=headers).status_code == 400
        assert client.get("/admin/model-changes/999/mail-preview", headers=headers).status_code == 404
        assert mail_on == [] and db_session.exec(select(ClientMailLog)).all() == []
        assert _states(db_session, change) == {}

    def test_a_scheduled_announcement_mails_nobody_yet(self, client, db_session, admin_user, mail_on):
        _person(db_session, "alice")
        change = _change(db_session, show_from=_at(1), show_until=_at(2))
        resp = client.post(f"/admin/model-changes/{change.id}/mail", headers=_admin(admin_user))
        assert resp.status_code == 400 and "not opened yet" in resp.json()["detail"]
        assert model_changes.request_mail_to(db_session, change, ["alice"]) == {"queued": 0, "skipped": []}


class TestEndpoint:
    """``GET /v1/model-changes``: the dashboard's model-change panel for a client
    that is not a browser. Which models are being replaced, and the renames
    for the caller's tier."""

    def _get(self, client, key: str = "sk-testkey123") -> dict:
        resp = client.get("/v1/model-changes", headers=auth_header(key))
        assert resp.status_code == 200, resp.text
        return resp.json()

    EMPTY = {"object": "model_changes", "changes": [], "retiring": [], "patch": {"models": {}}}

    def test_nothing_is_returned_outside_a_change(self, client, db_session, test_user):
        assert self._get(client) == self.EMPTY                       # before any change
        change = _change(db_session, show_until=_at(2))
        assert self._get(client)["retiring"] == ["old-llm"]          # while it is shown
        model_changes.end(db_session, change)
        assert self._get(client) == self.EMPTY                       # after it

    def test_only_between_the_opening_and_the_ending_time(self, client, db_session, test_user):
        change = _change(db_session, show_from=_at(1), show_until=_at(2))
        assert self._get(client) == self.EMPTY                       # scheduled: not open yet
        with patch("app.services.model_changes.utcnow", return_value=change.show_from):
            [entry] = self._get(client)["changes"]                   # from the opening time on
        assert entry["from"] == model_changes.local_iso(change.show_from) and entry["from"].endswith("+08:00")
        assert entry["until"] == model_changes.local_iso(change.show_until)
        with patch("app.services.model_changes.utcnow", return_value=change.show_until - timedelta(seconds=1)):
            assert self._get(client)["retiring"] == ["old-llm"]
        with patch("app.services.model_changes.utcnow", return_value=change.show_until):
            assert self._get(client) == self.EMPTY                   # and not from the ending time on

    def test_what_is_retiring_and_the_patch_for_the_callers_tier(self, client, db_session, test_user):
        _person(db_session, "local")
        _person(db_session, "azure", can_use_azure=True)
        change = _change(
            db_session, renames="old-llm -> test-llm", renames_azure="old-llm -> azure-gpt-4",
            renames_aws="bedrock-old -> bedrock-nova",
        )
        local = self._get(client, "sk-local")
        assert local == {
            "object": "model_changes",
            "changes": [{
                "id": f"MC{change.id}", "title": change.title, "message": change.message,
                "models": {"old-llm": "test-llm"}, "from": model_changes.local_iso(change.show_from),
                "until": model_changes.local_iso(change.show_until), "applied": False,
            }],
            "retiring": ["old-llm"],
            "patch": {"models": {"old-llm": "test-llm"}},
        }
        assert self._get(client, "sk-azure")["patch"] == {"models": {"old-llm": "azure-gpt-4"}}
        both = self._get(client)                                     # the fixture's account has Azure and AWS
        assert both["retiring"] == ["old-llm", "bedrock-old"]
        assert both["patch"] == {"models": {"old-llm": "azure-gpt-4", "bedrock-old": "bedrock-nova"}}

    def test_a_change_for_another_tier_is_not_returned(self, client, db_session, test_user):
        _person(db_session, "local")
        _change(db_session, renames="", renames_azure="azure-old -> azure-gpt-4")
        assert self._get(client, "sk-local") == self.EMPTY
        assert self._get(client)["retiring"] == ["azure-old"]

    def test_several_changes_make_one_patch(self, client, db_session, test_user):
        first = _change(db_session, renames="old-llm -> test-llm\nold-vlm -> test-vlm")
        second = _change(db_session, renames="old-llm -> test-vlm")
        body = self._get(client)
        assert [c["id"] for c in body["changes"]] == [f"MC{first.id}", f"MC{second.id}"]
        assert body["retiring"] == ["old-llm", "old-vlm"]
        assert body["patch"] == {"models": {"old-llm": "test-vlm", "old-vlm": "test-vlm"}}    # the later change wins

    def test_switched_is_reported_and_nothing_is_null(self, client, db_session, test_user):
        change = _change(db_session, message="")
        model_changes.set_applied(db_session, change.id, test_user)
        body = self._get(client)
        [entry] = body["changes"]
        assert entry["applied"] is True and "message" not in entry
        assert body["retiring"] == ["old-llm"]                       # still returned: the patch is for other machines too
        assert "null" not in json.dumps(body)

    def test_it_reads_and_never_shows_a_key(self, client, db_session, test_user):
        change = _change(db_session)
        assert test_user.api_key not in client.get("/v1/model-changes", headers=auth_header()).text
        for method in ("post", "put", "patch", "delete"):
            assert getattr(client, method)("/v1/model-changes", headers=auth_header()).status_code == 405
        assert client.get("/model-changes", headers=auth_header()).status_code == 404     # only under /v1
        assert client.get("/v1/model-changes").status_code == 401
        assert _states(db_session, change) == {}

    def test_it_answers_once_the_budget_is_spent(self, client, db_session, test_user):
        _change(db_session)
        test_user.daily_limit_usd = 0.0
        db_session.add(test_user)
        db_session.commit()
        assert self._get(client)["retiring"] == ["old-llm"]

    def test_the_notices_endpoint_knows_nothing_of_it(self, client, db_session, test_user):
        _change(db_session)
        assert client.get("/v1/notices", headers=auth_header()).json() == {"object": "notices", "window_days": 15, "notices": []}

    def test_a_broken_store_reads_as_no_change(self, client, db_session, test_user):
        _change(db_session)
        with patch("app.services.model_changes.for_account", side_effect=RuntimeError("no such table")):
            assert self._get(client) == self.EMPTY
