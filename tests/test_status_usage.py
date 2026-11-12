"""The dashboard's figures by API key, for clients such as a terminal tool:
``GET /v1/status`` (the models an account can use and whether they are up),
``GET /v1/usage`` (its usage per day, today and this month by model),
``GET /v1/apps`` (the app accounts it owns) and ``GET /v1/notices`` (its
client-rule notices)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlmodel import Session, select

from app.models.schema import AppOwner, ClientViolation, UsageLog, User
from tests.conftest import (
    TEST_AZURE_MODELS,
    TEST_MODEL_ROUTING,
    auth_header,
    earlier_today,
)


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


PATHS = ["/v1/status", "/v1/usage", "/v1/apps", "/v1/notices"]


class TestAuth:
    @pytest.mark.parametrize("path", PATHS)
    def test_needs_a_key(self, client, test_user, path):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=auth_header("sk-nope")).status_code == 401

    @pytest.mark.parametrize("path", PATHS)
    def test_answers_when_the_budget_is_spent(self, client, db_session, test_user, path):
        # That is when people look.
        _spend(db_session, test_user, "120")
        assert client.get("/v1/models", headers=auth_header()).status_code == 429
        assert client.get(path, headers=auth_header()).status_code == 200

    @pytest.mark.parametrize("path", PATHS)
    def test_disabled_account(self, client, db_session, test_user, path):
        _update(db_session, test_user, is_disabled=True)
        assert client.get(path, headers=auth_header()).status_code == 403

    @pytest.mark.parametrize("path", PATHS)
    def test_not_billed(self, client, db_session, test_user, path):
        assert client.get(path, headers=auth_header()).status_code == 200
        assert db_session.exec(select(UsageLog)).all() == []


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

    def test_declared_capabilities_are_passed_on(self, client, test_user):
        # The same metadata /v1/models carries (the dashboard's badges);
        # what the config does not declare is left out, never null.
        by_id = {m["id"]: m for m in self._status(client)["models"]}
        assert by_id["test-vlm"]["supports_tools"] is True
        assert by_id["test-vlm"]["supports_vision"] is True
        assert by_id["test-vlm"]["max_output_tokens"] == 4096
        assert "supports_tools" not in by_id["test-llm"]
        routing = {**TEST_MODEL_ROUTING, "test-llm": {**TEST_MODEL_ROUTING["test-llm"], "is_reasoning": False}}
        with patch("app.core.config.get_model_routing_snapshot", return_value=routing):
            by_id = {m["id"]: m for m in self._status(client)["models"]}
        assert by_id["test-llm"]["is_reasoning"] is False

    def test_up_down_and_load(self, client, test_user):
        down = TEST_MODEL_ROUTING["test-vlm"]["base_url"]
        busy = TEST_MODEL_ROUTING["test-llm"]["base_url"]
        with (
            patch("app.services.model_catalog.is_alive", side_effect=lambda url: url != down),
            patch("app.services.model_catalog.get_metrics",
                  side_effect=lambda url: {"running": 3, "waiting": 1} if url == busy else None),
        ):
            body = self._status(client)
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

    def test_cloud_models_need_access(self, client, db_session, test_user):
        _update(db_session, test_user, can_use_azure=False, can_use_bedrock=False)
        backends = {m["backend"] for m in self._status(client)["models"]}
        assert backends == {"onprem"}

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

    def test_today_by_model(self, client, db_session, test_user):
        # The dashboard's Cost by Model card, on "Today".
        from app.core.timeutil import local_today

        _spend(db_session, test_user, "3")
        _spend(db_session, test_user, "2", backend="azure", model="azure-gpt-4")
        _spend(db_session, test_user, "9", created_at=datetime.now(timezone.utc) - timedelta(days=2))
        body = self._usage(client)
        today = body["today"]
        assert today["date"] == local_today().isoformat() == body["days"][-1]["date"]
        assert today["cost_usd"] == 5.0 == body["days"][-1]["cost_usd"]
        assert today["requests"] == 2
        assert [(m["model"], m["backend"], m["cost_usd"]) for m in today["models"]] == [
            ("test-llm", "onprem", 3.0), ("azure-gpt-4", "azure", 2.0),
        ]
        assert set(today) - {"date"} == set(body["month"]) - {"month"}

    def test_a_revoked_backend_is_left_out_of_the_month(self, client, db_session, test_user):
        # Same as the dashboard: nothing of a cloud backend the account may
        # no longer use, rows and total alike.
        _spend(db_session, test_user, "3")
        _spend(db_session, test_user, "2", backend="azure", model="azure-gpt-4")
        _update(db_session, test_user, can_use_azure=False)
        body = self._usage(client)
        for block in (body["month"], body["today"]):
            assert [m["model"] for m in block["models"]] == ["test-llm"]
            assert block["cost_usd"] == 3.0

    def test_same_numbers_once_rolled_up(self, client, db_session, test_user):
        from app.services import usage_rollup

        _spend(db_session, test_user, "4", created_at=datetime.now(timezone.utc) - timedelta(days=2))
        _spend(db_session, test_user, "1")
        before = self._usage(client)
        usage_rollup.advance(db_session, now=datetime.now(timezone.utc))
        assert self._usage(client) == before

    def test_runs_off_the_event_loop(self):
        # It reads usage and awaits nothing: a plain def, so FastAPI runs it
        # in the threadpool and a slow query cannot stall the streams.
        import inspect

        from app.routers import v1_api

        assert not inspect.iscoroutinefunction(v1_api.usage_history)


class TestMe:
    def test_says_when_the_key_is_of_the_older_format(self, client, db_session, test_user):
        from app.models.schema import issue_api_key

        # The fixture's key is not one the gateway issues today.
        assert client.get("/v1/me", headers=auth_header()).json()["legacy_key"] is True
        issue_api_key(test_user)
        _update(db_session, test_user)
        body = client.get("/v1/me", headers=auth_header(test_user.api_key)).json()
        assert body["legacy_key"] is False
        assert test_user.api_key not in str(body)


class TestApps:
    def _app(self, db_session, owner: User, name: str, **fields) -> User:
        fields.setdefault("daily_limit_usd", 50.0)
        app = User(username=name, password_hash="x", api_key=f"sk-{name}-secret", **fields)
        db_session.add(app)
        db_session.commit()
        db_session.refresh(app)
        db_session.add(AppOwner(app_id=app.id, owner_id=owner.id))
        db_session.commit()
        return app

    def _apps(self, client, headers=None):
        resp = client.get("/v1/apps", headers=headers or auth_header())
        assert resp.status_code == 200, resp.text
        assert _no_nulls(resp.json()), resp.text
        return resp

    def test_none(self, client, test_user):
        body = self._apps(client).json()
        assert body["object"] == "apps"
        assert body["apps"] == []

    def test_owned_apps_with_this_months_usage(self, client, db_session, test_user):
        batch = self._app(db_session, test_user, "app_batch10")
        self._app(db_session, test_user, "app_batch2", daily_limit_usd=0.0)
        _spend(db_session, batch, "1.5")
        _spend(db_session, batch, "2")
        body = self._apps(client).json()
        assert body["month"] == datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m")
        assert body["apps"] == [
            {"username": "app_batch2", "daily_limit_usd": 0.0, "legacy_key": True,
             "month": {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}},
            {"username": "app_batch10", "daily_limit_usd": 50.0, "legacy_key": True,
             "month": {"requests": 2, "input_tokens": 2000, "output_tokens": 1000, "cost_usd": 3.5}},
        ]

    def test_an_apps_key_is_never_returned(self, client, db_session, test_user):
        # A leaked personal key must not open the app accounts behind it.
        self._app(db_session, test_user, "app_batch")
        text = self._apps(client).text
        assert "sk-app_batch-secret" not in text
        assert "api_key" not in text

    def test_only_the_callers_own(self, client, db_session, test_user, admin_user):
        self._app(db_session, admin_user, "app_theirs")
        assert self._apps(client).json()["apps"] == []
        theirs = self._apps(client, headers=auth_header(admin_user.api_key)).json()["apps"]
        assert [a["username"] for a in theirs] == ["app_theirs"]

    def test_runs_off_the_event_loop(self):
        import inspect

        from app.routers import v1_api

        assert not inspect.iscoroutinefunction(v1_api.owned_apps)
        assert not inspect.iscoroutinefunction(v1_api.client_notices)


AUTO = "CC001"      # on by default, fixed by a setting
OUTDATED = "CC003"  # fixed by updating Claude Code: no setting can do it


class TestNotices:
    def _now(self) -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def _violation(self, db_session, rule_id: str, username: str = "testuser", **kw) -> ClientViolation:
        kw.setdefault("last_seen_at", self._now())
        row = ClientViolation(rule_id=rule_id, username=username, first_seen_at=kw["last_seen_at"], **kw)
        db_session.add(row)
        db_session.commit()
        return row

    def _configure(self, db_session, rule_id: str, **kw):
        from app.services import client_rules

        rule = client_rules.get_rule(db_session, rule_id)
        values = dict(
            enabled=True, action=rule.action, title=rule.title, message=rule.message,
            patch=rule.patch, patch_azure=rule.patch_azure, patch_aws=rule.patch_aws,
            email_subject=rule.email_subject, auto_email=rule.auto_email, email_repeat_days=rule.email_repeat_days,
        )
        values.update(kw)
        client_rules.save_settings(db_session, rule.check, values, updated_by="test")
        client_rules.refresh()

    def _notices(self, client, headers=None) -> dict:
        resp = client.get("/v1/notices", headers=headers or auth_header())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert _no_nulls(body), body
        return body

    def test_nothing_open(self, client, test_user):
        assert self._notices(client) == {"object": "notices", "window_days": 15, "notices": []}

    def test_what_the_dashboard_panel_shows(self, client, db_session, test_user):
        self._violation(db_session, AUTO, client_version="2.1.280")
        body = self._notices(client)
        (notice,) = body["notices"]
        assert notice["id"] == AUTO
        assert notice["title"] and notice["message"]
        assert notice["client_version"] == "2.1.280"
        assert notice["last_seen_at"].endswith("+08:00")
        # The fix as data: what to set in the env block of settings.json.
        assert notice["patch"]["set"]["CLAUDE_CODE_AUTO_MODE_SERVER"] == "0"
        assert notice["patch"]["unset"] == []
        # The same text the dashboard shows comes from the same patch.
        page = client.get("/dashboard", headers={"Authorization": "Bearer " + _jwt()}).text
        assert notice["title"] in page
        assert body["patch"]["covers"] == [AUTO] and body["patch"]["manual"] == []
        assert body["patch"]["set"] == notice["patch"]["set"]

    def test_one_patch_for_everything_and_what_it_cannot_fix(self, client, db_session, test_user):
        self._configure(db_session, OUTDATED)
        self._violation(db_session, AUTO)
        self._violation(db_session, OUTDATED)
        body = self._notices(client)
        assert [n["id"] for n in body["notices"]] == [AUTO, OUTDATED]
        assert "patch" not in body["notices"][1]  # no setting updates Claude Code
        assert body["patch"]["covers"] == [AUTO]
        assert body["patch"]["manual"] == [OUTDATED]

    def test_only_manual_fixes_still_say_so(self, client, db_session, test_user):
        self._configure(db_session, OUTDATED)
        self._violation(db_session, OUTDATED)
        body = self._notices(client)
        assert body["patch"] == {"set": {}, "unset": [], "covers": [], "manual": [OUTDATED]}

    def test_a_variable_to_remove_is_listed_not_null(self, client, db_session, test_user):
        self._configure(db_session, AUTO, patch='{"CLAUDE_CODE_AUTO_MODE_SERVER": "0", "OLD_SETTING": null}')
        self._violation(db_session, AUTO)
        patch_ = self._notices(client)["notices"][0]["patch"]
        assert patch_["unset"] == ["OLD_SETTING"]
        assert "OLD_SETTING" not in patch_["set"]

    def test_the_patch_follows_the_accounts_access(self, client, db_session, test_user):
        self._configure(db_session, AUTO, patch='{"A": "base"}', patch_azure='{"A": "azure"}')
        self._violation(db_session, AUTO)
        assert self._notices(client)["patch"]["set"]["A"] == "azure"
        _update(db_session, test_user, can_use_azure=False)
        assert self._notices(client)["patch"]["set"]["A"] == "base"

    def test_resolved_old_and_other_peoples_are_left_out(self, client, db_session, test_user):
        from app.services import client_rules

        self._violation(db_session, AUTO, username="someone-else")
        assert self._notices(client)["notices"] == []
        old = self._now() - timedelta(days=client_rules.WINDOW_DAYS + 1)
        self._violation(db_session, AUTO, last_seen_at=old)
        assert self._notices(client)["notices"] == []
        for row in db_session.exec(select(ClientViolation)).all():
            db_session.delete(row)
        db_session.commit()
        self._violation(db_session, AUTO)
        assert len(self._notices(client)["notices"]) == 1
        assert client_rules.resolve(db_session, AUTO, "testuser")
        assert self._notices(client) == {"object": "notices", "window_days": 15, "notices": []}

    def test_a_key_is_never_in_a_patch(self, client, db_session, test_user):
        self._violation(db_session, AUTO)
        assert test_user.api_key not in client.get("/v1/notices", headers=auth_header()).text

    def test_read_only(self, client, db_session, test_user):
        # Reporting a notice as fixed stays on the dashboard, behind SSO.
        self._violation(db_session, AUTO)
        for method in ("post", "put", "delete"):
            assert getattr(client, method)("/v1/notices", headers=auth_header()).status_code == 405
        assert len(self._notices(client)["notices"]) == 1

    def test_a_broken_store_reads_as_nothing_open(self, client, test_user):
        with patch("app.services.client_rules.notices_for", side_effect=RuntimeError("no table")):
            assert self._notices(client)["notices"] == []


def _jwt() -> str:
    from tests.conftest import _make_test_jwt

    return _make_test_jwt(sub="testuser")
