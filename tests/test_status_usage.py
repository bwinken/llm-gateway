"""``GET /v1/status`` (the models an account can use and whether they are
up) and ``GET /v1/usage`` (its usage per day and this month by model): the
dashboard's figures by API key, for clients such as a terminal tool."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlmodel import Session

from app.models.schema import UsageLog, User
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


class TestAuth:
    @pytest.mark.parametrize("path", ["/v1/status", "/v1/usage"])
    def test_needs_a_key(self, client, test_user, path):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=auth_header("sk-nope")).status_code == 401

    @pytest.mark.parametrize("path", ["/v1/status", "/v1/usage"])
    def test_answers_when_the_budget_is_spent(self, client, db_session, test_user, path):
        # That is when people look.
        _spend(db_session, test_user, "120")
        assert client.get("/v1/models", headers=auth_header()).status_code == 429
        assert client.get(path, headers=auth_header()).status_code == 200

    @pytest.mark.parametrize("path", ["/v1/status", "/v1/usage"])
    def test_disabled_account(self, client, db_session, test_user, path):
        _update(db_session, test_user, is_disabled=True)
        assert client.get(path, headers=auth_header()).status_code == 403

    @pytest.mark.parametrize("path", ["/v1/status", "/v1/usage"])
    def test_not_billed(self, client, db_session, test_user, path):
        from sqlmodel import select

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

    def test_runs_off_the_event_loop(self):
        # It reads usage and awaits nothing: a plain def, so FastAPI runs it
        # in the threadpool and a slow query cannot stall the streams.
        import inspect

        from app.routers import v1_api

        assert not inspect.iscoroutinefunction(v1_api.usage_history)
