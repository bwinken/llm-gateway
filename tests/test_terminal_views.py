"""The terminal side of the account endpoints: the plain-text views of
``GET /v1/me``, ``/v1/status`` and ``/v1/usage``, and ``gw`` (``setup/gw``,
served by ``GET /v1/cli``), the script that asks for them."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlmodel import Session

from app.models.schema import UsageLog, User
from app.services import terminal
from app.services.terminal import Style
from tests.conftest import (
    TEST_AZURE_MODELS,
    TEST_MODEL_ROUTING,
    auth_header,
    earlier_today,
)

GW = Path(__file__).resolve().parent.parent / "setup" / "gw"


def _spend(db_session: Session, user: User, amount: str, backend: str = "vllm",
           model: str = "test-llm", model_type: str = "llm", **kwargs) -> None:
    kwargs.setdefault("created_at", earlier_today())
    db_session.add(UsageLog(
        user_id=user.id, model=model, model_type=model_type, input_tokens=1000, output_tokens=500,
        cost_usd=Decimal(amount), endpoint="/v1/chat/completions", backend=backend, **kwargs,
    ))
    db_session.commit()


def _update(db_session: Session, user: User, **fields) -> None:
    for key, value in fields.items():
        setattr(user, key, value)
    db_session.add(user)
    db_session.commit()


def _no_nulls(value) -> bool:
    if isinstance(value, dict):
        return all(_no_nulls(v) for v in value.values())
    if isinstance(value, list):
        return all(_no_nulls(v) for v in value)
    return value is not None


def _text(client, path: str, **params) -> str:
    resp = client.get(path, params={"format": "text", **params}, headers=auth_header())
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain")
    return resp.text


class TestChoosingText:
    def test_json_stays_the_default(self, client, test_user):
        # curl sends `Accept: */*`: nothing that reads /v1/me today changes.
        resp = client.get("/v1/me", headers={**auth_header(), "Accept": "*/*"})
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.json()["object"] == "account"

    def test_format_text(self, client, test_user):
        text = _text(client, "/v1/me")
        assert "testuser" in text
        assert "$0.00 of $100.00" in text
        assert "$100.00 left" in text

    def test_accept_header(self, client, test_user):
        resp = client.get("/v1/me", headers={**auth_header(), "Accept": "text/plain"})
        assert resp.headers["content-type"].startswith("text/plain")
        assert "Budget today" in resp.text

    def test_format_wins_over_accept(self, client, test_user):
        resp = client.get("/v1/me?format=json", headers={**auth_header(), "Accept": "text/plain"})
        assert resp.json()["username"] == "testuser"

    def test_unknown_format_is_refused(self, client, test_user):
        assert client.get("/v1/me?format=xml", headers=auth_header()).status_code == 422

    def test_plain_unless_asked(self, client, test_user):
        # The server cannot see a terminal: no escape codes unless asked for.
        assert "\x1b" not in _text(client, "/v1/me")
        assert "\x1b[" in _text(client, "/v1/me", color=1)

    def test_ascii(self, client, db_session, test_user):
        _spend(db_session, test_user, "40")
        for path in ("/v1/me", "/v1/status", "/v1/usage"):
            assert _text(client, path, ascii=1).isascii(), path
        assert "[########------------]" in _text(client, "/v1/me", ascii=1)

    def test_text_is_never_cached(self, client, test_user):
        resp = client.get("/v1/me?format=text", headers=auth_header())
        assert resp.headers["cache-control"] == "no-store"

    @pytest.mark.parametrize("path", ["/v1/me", "/v1/status", "/v1/usage", "/v1/cli"])
    def test_needs_a_key(self, client, test_user, path):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=auth_header("sk-nope")).status_code == 401

    @pytest.mark.parametrize("path", ["/v1/me", "/v1/status", "/v1/usage", "/v1/cli"])
    def test_answers_when_the_budget_is_spent(self, client, db_session, test_user, path):
        # That is when people look.
        _spend(db_session, test_user, "120")
        assert client.get("/v1/models", headers=auth_header()).status_code == 429
        assert client.get(path, headers=auth_header()).status_code == 200


class TestBudgetText:
    def test_spent_budget_says_what_happens(self, client, db_session, test_user):
        _spend(db_session, test_user, "120")
        text = _text(client, "/v1/me")
        assert "100%" in text
        assert "Budget spent: requests are refused (429) until the reset." in text

    def test_soft_mode(self, client, db_session, test_user, monkeypatch):
        monkeypatch.setenv("ENFORCE_DAILY_LIMIT", "false")
        _spend(db_session, test_user, "120")
        text = _text(client, "/v1/me")
        assert "not enforced" in text
        assert "refused" not in text

    def test_cloud_cap(self, client, db_session, test_user):
        _spend(db_session, test_user, "5", backend="azure", model="azure-gpt-4")
        with patch.dict("app.core.config.APP_CONFIG", {"cloud_daily_limit_usd": 5.0}):
            text = _text(client, "/v1/me")
            assert "$5.00 of $5.00" in text
            assert "cloud models are refused (429)" in text
            with patch("app.routers.v1_api.get_cloud_budget_fallback", return_value=True):
                assert "cloud models are answered by on-prem models" in _text(client, "/v1/me")

    def test_no_access_is_said(self, client, db_session, test_user):
        _update(db_session, test_user, can_use_azure=False, can_use_bedrock=False)
        lines = _text(client, "/v1/me").splitlines()
        assert any(line.split() == ["Azure", "no", "access"] for line in lines)
        assert not any(line.strip().startswith("cloud") for line in lines)

    def test_concurrency(self, client, test_user):
        assert "at a time" not in _text(client, "/v1/me")
        with patch("app.routers.v1_api.get_concurrency_settings", return_value=("enforce", 4)):
            assert "Up to 4 requests at a time." in _text(client, "/v1/me")

    def test_control_characters_never_reach_the_terminal(self, client, db_session, test_user):
        # A display name comes from SSO; an escape sequence in it would run
        # in the reader's terminal.
        _update(db_session, test_user, display_name="Al\x1b[2Jice\x07\x9b")
        text = _text(client, "/v1/me")
        assert "Al[2Jice" in text
        assert not any(ch in text for ch in "\x1b\x07\x9b")

    def test_wide_names_keep_the_columns(self):
        assert terminal._width("王小明") == 6
        assert terminal._width(Style(color=True).bold("ab")) == 2
        rows = terminal._table([["王小明", "1"], ["bob", "22"]], "lr")
        assert [terminal._width(row) for row in rows] == [12, 12]


class TestFormatting:
    def test_amounts(self):
        assert terminal.usd(0) == "$0.00"
        assert terminal.usd(1234.5) == "$1,234.50"
        assert terminal.usd(0.0042) == "$0.0042"
        assert terminal.tokens(950) == "950"
        assert terminal.tokens(120_000) == "120.0K"
        assert terminal.tokens(3_200_000) == "3.2M"
        assert terminal.context_window(131072) == "128K"
        assert terminal.context_window(200_000) == "200K"
        assert terminal.context_window(1_000_000) == "1M"
        assert terminal.duration(34_080) == "9h 28m"
        assert terminal.duration(600) == "10m"

    def test_a_zero_limit_is_fully_spent(self):
        # $0 is a $0 budget, never "unlimited".
        assert terminal._share(0, 0) == 1.0
        assert terminal._level(terminal._share(0, 0)) == "over"

    def test_anything_spent_shows_in_the_bar(self):
        assert terminal._bar(0.001, Style(ascii=True)) == "[#-------------------]"
        assert terminal._bar(0, Style(ascii=True)) == "[--------------------]"


class TestStatus:
    def _status(self, client, headers=None):
        resp = client.get("/v1/status", headers=headers or auth_header())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert _no_nulls(body), body
        return body

    def test_lists_what_the_account_can_use(self, client, test_user):
        body = self._status(client)
        assert body["object"] == "status"
        by_id = {m["id"]: m for m in body["models"]}
        assert by_id["test-llm"]["backend"] == "onprem"
        assert by_id["azure-gpt-4"]["backend"] == "azure"
        assert by_id["bedrock-claude"]["backend"] == "aws"
        # Every type, not only the chat models /v1/models lists.
        assert "test-embedding" in by_id
        assert by_id["test-vlm"]["display_name"] == "Test VLM"
        assert by_id["test-vlm"]["context_window"] == 32768
        # The price a request is billed at ([pricing.llm] here).
        assert by_id["test-llm"]["input_price_per_1m"] == 0.5
        assert by_id["test-llm"]["output_price_per_1m"] == 1.5

    def test_up_down_and_load(self, client, test_user):
        down = TEST_MODEL_ROUTING["test-vlm"]["base_url"]
        busy = TEST_MODEL_ROUTING["test-llm"]["base_url"]
        with (
            patch("app.services.model_catalog.is_alive", side_effect=lambda url: url != down),
            patch("app.services.model_catalog.get_metrics",
                  side_effect=lambda url: {"running": 3, "waiting": 1} if url == busy else None),
        ):
            body = self._status(client)
            text = _text(client, "/v1/status")
        by_id = {m["id"]: m for m in body["models"]}
        assert by_id["test-llm"] == {
            "id": "test-llm", "backend": "onprem", "type": "llm", "state": "up",
            "running": 3, "waiting": 1, "input_price_per_1m": 0.5, "output_price_per_1m": 1.5,
        }
        assert by_id["test-vlm"]["state"] == "down"
        assert "running" not in by_id["test-vlm"]
        # The gateway does not probe cloud models.
        assert by_id["azure-gpt-4"]["state"] == "unchecked"
        total = len(TEST_MODEL_ROUTING)
        assert body["onprem"] == {"total": total, "up": total - 1, "down": ["test-vlm"]}

        assert f"{total - 1} of {total} on-prem models up" in text
        assert "3 running, 1 waiting" in text
        vlm_line = next(line for line in text.splitlines() if "test-vlm" in line)
        assert "DOWN" in vlm_line and "32K ctx" in vlm_line and "$5.00 / $15.00" in vlm_line
        assert "Cloud models are not health-checked." in text

    def test_cloud_models_need_access(self, client, db_session, test_user):
        _update(db_session, test_user, can_use_azure=False, can_use_bedrock=False)
        backends = {m["backend"] for m in self._status(client)["models"]}
        assert backends == {"onprem"}
        text = _text(client, "/v1/status")
        assert "azure-gpt-4" not in text and "Azure" not in text

    def test_admins_see_every_backend(self, client, admin_user):
        body = self._status(client, headers=auth_header(admin_user.api_key))
        assert {m["backend"] for m in body["models"]} == {"onprem", "azure", "aws"}

    def test_hidden_models_are_left_out(self, client, test_user):
        routing = {**TEST_MODEL_ROUTING, "staged": {**TEST_MODEL_ROUTING["test-llm"], "hidden": True}}
        azure = {**TEST_AZURE_MODELS, "azure-staged": {**TEST_AZURE_MODELS["azure-gpt-4"], "hidden": True}}
        with (
            patch("app.core.config.get_model_routing_snapshot", return_value=routing),
            patch("app.core.config.get_azure_models_snapshot", return_value=azure),
        ):
            ids = [m["id"] for m in self._status(client)["models"]]
        assert "staged" not in ids and "azure-staged" not in ids
        assert "test-llm" in ids

    def test_the_one_display_order(self, client, test_user):
        # Backends in turn, and within each the order every page uses
        # (model_order): by type first.
        models = self._status(client)["models"]
        backends = [m["backend"] for m in models]
        assert backends == sorted(backends, key=["onprem", "azure", "aws"].index)
        onprem_types = [m["type"] for m in models if m["backend"] == "onprem"]
        from app.services.model_order import type_rank
        assert onprem_types == sorted(onprem_types, key=type_rank)
        with patch("app.core.config.get_display_order_snapshot",
                   return_value={"vllm": [], "azure": ["azure-embed", "azure-gpt-4"], "bedrock": []}):
            azure = [m["id"] for m in self._status(client)["models"] if m["backend"] == "azure"]
        assert azure == ["azure-gpt-4", "azure-embed"]  # type still comes first


class TestUsage:
    def _usage(self, client, **params):
        resp = client.get("/v1/usage", params=params, headers=auth_header())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert _no_nulls(body), body
        return body

    def test_one_row_per_day_today_last(self, client, db_session, test_user):
        from app.core.timeutil import local_today

        _spend(db_session, test_user, "2.5")
        _spend(db_session, test_user, "4", created_at=datetime.now(timezone.utc) - timedelta(days=2))
        body = self._usage(client)
        assert body["object"] == "usage"
        assert body["timezone"] == "Asia/Taipei"
        days = body["days"]
        today = local_today()
        assert [d["date"] for d in days] == [(today - timedelta(days=6 - i)).isoformat() for i in range(7)]
        assert days[-1] == {
            "date": today.isoformat(), "requests": 1, "input_tokens": 1000, "output_tokens": 500, "cost_usd": 2.5,
        }
        # Quiet days are there as zeros, and the older spend is on its own day.
        assert sum(d["cost_usd"] for d in days) == 6.5
        assert sum(1 for d in days if d["requests"] == 0) == 5

    def test_days_parameter(self, client, test_user):
        assert len(self._usage(client, days=1)["days"]) == 1
        assert len(self._usage(client, days=30)["days"]) == 30
        for bad in (0, 91, "x"):
            assert client.get(f"/v1/usage?days={bad}", headers=auth_header()).status_code == 422

    def test_this_month_by_model(self, client, db_session, test_user):
        _spend(db_session, test_user, "3")
        _spend(db_session, test_user, "1")
        _spend(db_session, test_user, "2", backend="azure", model="azure-gpt-4")
        _spend(db_session, test_user, "0.5", backend="bedrock", model="bedrock-claude")
        month = self._usage(client)["month"]
        assert month["month"] == datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m")
        assert month["cost_usd"] == 6.5
        assert month["requests"] == 4
        assert [(m["model"], m["backend"], m["requests"], m["cost_usd"]) for m in month["models"]] == [
            ("test-llm", "onprem", 2, 4.0),
            ("azure-gpt-4", "azure", 1, 2.0),
            ("bedrock-claude", "aws", 1, 0.5),
        ]

    def test_a_revoked_backend_is_left_out_of_the_month(self, client, db_session, test_user):
        # Same as the dashboard: nothing of a cloud backend the account may
        # no longer use, rows and total alike.
        _spend(db_session, test_user, "3")
        _spend(db_session, test_user, "2", backend="azure", model="azure-gpt-4")
        _update(db_session, test_user, can_use_azure=False)
        month = self._usage(client)["month"]
        assert [m["model"] for m in month["models"]] == ["test-llm"]
        assert month["cost_usd"] == 3.0

    def test_same_numbers_once_rolled_up(self, client, db_session, test_user):
        from app.services import usage_rollup

        _spend(db_session, test_user, "4", created_at=datetime.now(timezone.utc) - timedelta(days=2))
        _spend(db_session, test_user, "1")
        before = self._usage(client)
        usage_rollup.advance(db_session, now=datetime.now(timezone.utc))
        assert self._usage(client) == before

    def test_text(self, client, db_session, test_user):
        _spend(db_session, test_user, "2.5")
        _spend(db_session, test_user, "1", backend="azure", model="azure-gpt-4")
        text = _text(client, "/v1/usage", days=3)
        lines = text.splitlines()
        assert lines[0].startswith("Usage") and "last 3 days" in lines[0]
        today_line = next(line for line in lines if line.rstrip().endswith("█" * 12))
        assert " * " in today_line and "$3.50" in today_line
        assert any(line.split()[:1] == ["total"] and "$3.50" in line for line in lines)
        assert "This month" in text and "$3.50, 2 requests" in text
        llm = next(line for line in lines if line.strip().startswith("test-llm"))
        assert llm.split() == ["test-llm", "on-prem", "1", "$2.50", "71%"]

    def test_runs_off_the_event_loop(self):
        # It reads usage and awaits nothing: a plain def, so FastAPI runs it
        # in the threadpool and a slow query cannot stall the streams.
        import inspect

        from app.routers import v1_api

        assert not inspect.iscoroutinefunction(v1_api.usage_history)


class TestScriptDownload:
    def test_serves_the_script_with_this_gateways_address(self, client, test_user):
        resp = client.get("/v1/cli", headers=auth_header())
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        script = resp.text
        assert script.startswith("#!/bin/sh\n")
        assert "GATEWAY_URL='http://testserver'" in script
        assert "__GATEWAY_URL__" not in script
        # The same file for everyone: it never carries the caller's key.
        assert "sk-testkey123" not in script
        assert script.replace("http://testserver", "__GATEWAY_URL__") == GW.read_text()

    @pytest.mark.parametrize("host", ["gw.example.com'; rm -rf ~; '", "a b", "$(id)", "x/../y", ""])
    def test_only_a_plain_host_is_written_into_it(self, client, test_user, host):
        resp = client.get("/v1/cli", headers={**auth_header(), "Host": host})
        if resp.status_code == 200:  # some of these never get past the HTTP layer
            assert "GATEWAY_URL=''" in resp.text
            assert "rm -rf" not in resp.text and "$(id)" not in resp.text

    def test_host_with_a_port(self, client, test_user):
        resp = client.get("/v1/cli", headers={**auth_header(), "Host": "gw.example.com:8443"})
        assert "GATEWAY_URL='http://gw.example.com:8443'" in resp.text

    def test_the_repository_copy_has_no_address_and_no_key(self):
        text = GW.read_text()
        assert text.count("__GATEWAY_URL__") == 1
        assert "sk-" not in text
        assert os.access(GW, os.X_OK)


# ── The script itself, run with a stand-in for curl ──

_FAKE_CURL = r"""#!/bin/sh
# Records how it was called; answers like the gateway would.
printf '%s\n' "$@" > "$GW_TEST_DIR/argv"
cat > "$GW_TEST_DIR/stdin"
for arg in "$@"; do url=$arg; done
case $url in
    */v1/cli) printf '#!/bin/sh\necho new-script\n' ;;
    *) printf 'BODY %s\n' "$url" ;;
esac
printf '\n%s' "${GW_TEST_STATUS:-200}"
"""


@pytest.fixture()
def gw(tmp_path):
    """Run ``setup/gw`` with a fake ``curl`` first on PATH and an empty
    home. Returns ``run(*args, env=..., settings=...)`` → the finished
    process, plus what curl was given (``.argv``, ``.stdin``)."""
    if not shutil.which("sh"):
        pytest.skip("needs a POSIX shell")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(_FAKE_CURL)
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    script = tmp_path / "gw"
    shutil.copy(GW, script)

    def run(*args, env=None, settings=None, baked=None, status=None):
        text = GW.read_text()
        if baked:
            text = text.replace("__GATEWAY_URL__", baked)
        script.write_text(text)
        script.chmod(0o755)
        settings_file = home / ".claude" / "settings.json"
        if settings is None:
            settings_file.unlink(missing_ok=True)
        else:
            import json
            settings_file.write_text(json.dumps({"env": settings, "model": "x"}))
        for name in ("argv", "stdin"):
            (tmp_path / name).unlink(missing_ok=True)
        # A clean environment: the developer's own ANTHROPIC_* must not leak in.
        full_env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(home),
            "GW_TEST_DIR": str(tmp_path),
            "LANG": "C.UTF-8",
            **({"GW_TEST_STATUS": str(status)} if status else {}),
            **(env or {}),
        }
        done = subprocess.run(
            ["sh", str(script), *args], env=full_env, capture_output=True, text=True, timeout=30,
        )
        done.argv = (tmp_path / "argv").read_text().splitlines() if (tmp_path / "argv").exists() else None
        done.stdin_seen = (tmp_path / "stdin").read_text() if (tmp_path / "stdin").exists() else None
        return done

    run.script = script
    return run


GATEWAY = "https://gw.example.com"


class TestScript:
    def test_explicit_key_and_address(self, gw):
        done = gw("me", env={"LLM_GATEWAY_URL": GATEWAY + "/v1", "LLM_GATEWAY_API_KEY": "sk-mine"})
        assert done.returncode == 0, done.stderr
        assert done.stdout == f"BODY {GATEWAY}/v1/me?format=text\n"

    def test_the_key_is_never_on_the_command_line(self, gw):
        # `ps` shows every user's command lines to everyone on a shared host.
        done = gw("me", env={"LLM_GATEWAY_URL": GATEWAY, "LLM_GATEWAY_API_KEY": "sk-mine"})
        assert not any("sk-mine" in arg for arg in done.argv)
        assert done.stdin_seen == 'header = "Authorization: Bearer sk-mine"\n'
        assert "sk-mine" not in done.stdout + done.stderr

    def test_uses_claude_codes_key_when_it_points_here(self, gw):
        done = gw("me", baked=GATEWAY, settings={
            "ANTHROPIC_AUTH_TOKEN": "sk-from-settings", "ANTHROPIC_BASE_URL": GATEWAY + "/azure",
        })
        assert done.returncode == 0, done.stderr
        assert "Bearer sk-from-settings" in done.stdin_seen

    def test_the_address_can_come_from_claude_code_too(self, gw):
        # The repository copy has no address written into it.
        done = gw("me", settings={
            "ANTHROPIC_AUTH_TOKEN": "sk-from-settings", "ANTHROPIC_BASE_URL": "HTTPS://GW.example.com/aws/v1",
        })
        assert done.stdout == f"BODY {GATEWAY}/v1/me?format=text\n"

    def test_a_token_for_another_service_is_never_sent_here(self, gw):
        for settings in (
            {"ANTHROPIC_AUTH_TOKEN": "sk-other", "ANTHROPIC_BASE_URL": "https://elsewhere.example"},
            {"ANTHROPIC_AUTH_TOKEN": "sk-other"},  # no base URL: that one talks to Anthropic
        ):
            done = gw("me", baked=GATEWAY, settings=settings)
            assert done.returncode == 1
            assert "no API key" in done.stderr
            assert done.argv is None  # curl was never run

    def test_environment_before_settings(self, gw):
        done = gw(
            "me", baked=GATEWAY,
            env={"ANTHROPIC_AUTH_TOKEN": "sk-env", "ANTHROPIC_BASE_URL": GATEWAY},
            settings={"ANTHROPIC_AUTH_TOKEN": "sk-settings", "ANTHROPIC_BASE_URL": GATEWAY},
        )
        assert "Bearer sk-env" in done.stdin_seen

    def test_an_environment_pointing_elsewhere_does_not_hide_the_settings(self, gw):
        done = gw(
            "me",
            env={"ANTHROPIC_BASE_URL": "https://api.anthropic.com"},
            settings={"ANTHROPIC_AUTH_TOKEN": "sk-settings", "ANTHROPIC_BASE_URL": GATEWAY},
        )
        assert done.stdout == f"BODY {GATEWAY}/v1/me?format=text\n"

    def test_a_malformed_key_is_not_sent(self, gw):
        done = gw("me", env={"LLM_GATEWAY_URL": GATEWAY, "LLM_GATEWAY_API_KEY": 'sk-x"\nheader = "X: y'})
        assert done.returncode == 1 and done.argv is None

    def test_nothing_configured(self, gw):
        done = gw("me")
        assert done.returncode == 1
        assert "LLM_GATEWAY_URL" in done.stderr

    def test_views(self, gw):
        env = {"LLM_GATEWAY_URL": GATEWAY, "LLM_GATEWAY_API_KEY": "sk-mine"}
        assert gw("models", env=env).stdout == f"BODY {GATEWAY}/v1/status?format=text\n"
        assert gw("usage", "30", env=env).stdout == f"BODY {GATEWAY}/v1/usage?days=30&format=text\n"
        assert gw("usage", env=env).stdout == f"BODY {GATEWAY}/v1/usage?days=7&format=text\n"
        # No command: the budget, a blank line, the models.
        assert gw(env=env).stdout == (
            f"BODY {GATEWAY}/v1/me?format=text\n\nBODY {GATEWAY}/v1/status?format=text\n"
        )

    def test_json_colour_and_ascii(self, gw):
        env = {"LLM_GATEWAY_URL": GATEWAY, "LLM_GATEWAY_API_KEY": "sk-mine"}
        assert gw("models", "--json", env=env).stdout == f"BODY {GATEWAY}/v1/status\n"
        assert gw("usage", "3", "--json", env=env).stdout == f"BODY {GATEWAY}/v1/usage?days=3\n"
        # Not a terminal here, so no colour unless asked.
        assert gw("me", "--color", env=env).stdout == f"BODY {GATEWAY}/v1/me?format=text&color=1\n"
        assert gw("me", "--ascii", env=env).stdout == f"BODY {GATEWAY}/v1/me?format=text&ascii=1\n"
        assert gw("me", env={**env, "LANG": "C"}).stdout == f"BODY {GATEWAY}/v1/me?format=text&ascii=1\n"

    def test_bad_arguments(self, gw):
        env = {"LLM_GATEWAY_URL": GATEWAY, "LLM_GATEWAY_API_KEY": "sk-mine"}
        for args in (("usage", "7;id"), ("frobnicate",), ("--frob",), ("usage", "1", "2")):
            done = gw(*args, env=env)
            assert done.returncode == 1, args
            assert done.argv is None, args

    def test_a_refusal_is_reported(self, gw):
        env = {"LLM_GATEWAY_URL": GATEWAY, "LLM_GATEWAY_API_KEY": "sk-mine"}
        done = gw("me", env=env, status=401)
        assert done.returncode == 1 and done.stdout == ""
        assert "refused the key (401)" in done.stderr
        done = gw("me", env=env, status=403)
        assert done.returncode == 1 and "answered 403" in done.stderr and "BODY" in done.stderr

    def test_help_needs_nothing(self, gw):
        done = gw("help")
        assert done.returncode == 0 and done.argv is None
        assert done.stdout.startswith("gw - ")
        assert "gw usage [N]" in done.stdout and "set -eu" not in done.stdout

    def test_update_replaces_the_script(self, gw):
        done = gw("update", env={"LLM_GATEWAY_URL": GATEWAY, "LLM_GATEWAY_API_KEY": "sk-mine"})
        assert done.returncode == 0, done.stderr
        assert gw.script.read_text() == "#!/bin/sh\necho new-script\n"
        assert os.access(gw.script, os.X_OK)
        assert done.argv[-1] == f"{GATEWAY}/v1/cli"

    def test_update_keeps_the_script_when_the_reply_is_not_one(self, gw):
        done = gw("update", env={"LLM_GATEWAY_URL": GATEWAY + "/v1/elsewhere", "LLM_GATEWAY_API_KEY": "sk-mine"},
                  status=500)
        assert done.returncode == 1
        assert gw.script.read_text().startswith("#!/bin/sh\n# gw - ")
