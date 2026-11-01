"""One display order for models (app/services/model_order.py), used by the
welcome page, the dashboard's model tables and its Cost by Model card."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.models.schema import UsageLog, User
from app.services.model_order import (
    catalog_key,
    natural_key,
    sort_order_of,
    type_rank,
    unknown_key,
)
from tests.conftest import web_auth_header


def _route(model_type: str = "llm", host: str = "a", **extra) -> dict:
    return {"base_url": f"http://{host}:8000/v1", "real_model": "x", "api_key": "", "type": model_type, **extra}


def _order(body: str, names: list[str]) -> list[str]:
    """``names`` in the order they first appear as model cells in ``body``."""
    found = [(body.index(f">{n}</code>"), n) for n in names if f">{n}</code>" in body]
    return [n for _, n in sorted(found)]


class TestKeys:
    def test_natural_order_compares_numbers(self):
        names = ["qwen3-235b", "qwen3-8b", "qwen3-32b", "qwen3-1.7b"]
        assert sorted(names, key=natural_key) == ["qwen3-1.7b", "qwen3-8b", "qwen3-32b", "qwen3-235b"]

    def test_natural_order_ignores_case(self):
        assert sorted(["gemma-3", "Llama-3", "deepseek-v3"], key=natural_key) == ["deepseek-v3", "gemma-3", "Llama-3"]

    def test_versions(self):
        assert sorted(["claude-sonnet-4.10", "claude-sonnet-4.5", "claude-sonnet-4"], key=natural_key) == [
            "claude-sonnet-4", "claude-sonnet-4.5", "claude-sonnet-4.10",
        ]

    def test_sort_order_first_then_price_then_name(self):
        rows = [
            ("cheap", None, 1.0), ("pinned-2", 2, 99.0), ("pinned-1", 1, 50.0),
            ("b-same", None, 5.0), ("a-same", None, 5.0),
        ]
        ordered = sorted(rows, key=lambda r: catalog_key(*r))
        assert [r[0] for r in ordered] == ["pinned-1", "pinned-2", "cheap", "a-same", "b-same"]

    def test_unknown_models_after_configured(self):
        assert catalog_key("zzz", None, 1e9) < unknown_key("aaa")

    @pytest.mark.parametrize("value, expected", [(3, 3), (0, 0), (True, None), ("1", None), (None, None)])
    def test_sort_order_of(self, value, expected):
        assert sort_order_of({"sort_order": value}) == expected

    def test_type_rank(self):
        assert type_rank("llm") < type_rank("vlm") < type_rank("systemone") < type_rank("audio") < type_rank("zzz")


class TestDashboardTables:
    def _dashboard(self, client, user, routing, alive=lambda url: True):
        with (
            patch("app.routers.web_ui.get_model_routing_snapshot", return_value=routing),
            patch("app.routers.web_ui.is_alive", side_effect=alive),
        ):
            return client.get("/dashboard", headers=web_auth_header(sub=user.username)).text

    def test_same_price_models_sort_naturally(self, client, test_user):
        routing = {n: _route(host=n) for n in ("qwen3-235b", "Qwen3-8b", "qwen3-32b")}
        body = self._dashboard(client, test_user, routing)
        assert _order(body, list(routing)) == ["Qwen3-8b", "qwen3-32b", "qwen3-235b"]

    def test_pinned_first_and_label_says_so(self, client, test_user):
        routing = {
            "cheap": _route(host="c", input_price_per_1m=0.1, output_price_per_1m=0.1),
            "flagship": _route(host="f", sort_order=1, input_price_per_1m=9, output_price_per_1m=9),
        }
        body = self._dashboard(client, test_user, routing)
        assert _order(body, list(routing)) == ["flagship", "cheap"]
        assert "admin order, then by price" in body

    def test_down_servers_go_last(self, client, test_user):
        routing = {"a-model": _route(host="down"), "b-model": _route(host="up")}
        body = self._dashboard(client, test_user, routing, alive=lambda url: "down" not in url)
        assert _order(body, list(routing)) == ["b-model", "a-model"]

    def test_types_in_fixed_order_even_if_config_differs(self, client, test_user):
        routing = {"emb": _route("embedding", host="e"), "chat": _route("llm", host="l")}
        body = self._dashboard(client, test_user, routing)
        assert body.index("LLM (Chat)") < body.index("Embedding")

    def test_welcome_page_uses_the_same_order(self, client, test_user):
        routing = {n: _route(host=n) for n in ("qwen3-32b", "qwen3-8b")}
        with patch("app.routers.web_ui.get_model_routing_snapshot", return_value=routing):
            body = client.get("/", headers=web_auth_header(sub=test_user.username)).text
        assert body.index("qwen3-8b") < body.index("qwen3-32b")


class TestCostByModelFollowsTheTables:
    """The Cost by Model rows sit in the model tables' order, not by spend."""

    def _spend(self, db, user, model, cost, backend="vllm", model_type="llm"):
        db.add(UsageLog(
            user_id=user.id, model=model, model_type=model_type, input_tokens=10, output_tokens=1,
            cost_usd=cost, endpoint="/v1/chat/completions", backend=backend,
            created_at=datetime.now(timezone.utc),
        ))
        db.commit()

    def test_rows_in_catalog_order(self, client, db_session, test_user):
        routing = {n: _route(host=n) for n in ("qwen3-8b", "qwen3-32b")}
        routing["emb"] = _route("embedding", host="e")
        self._spend(db_session, test_user, "emb", 9.0, model_type="embedding")
        self._spend(db_session, test_user, "qwen3-32b", 5.0)
        self._spend(db_session, test_user, "qwen3-8b", 1.0)
        self._spend(db_session, test_user, "retired-model", 7.0)  # no longer configured

        with patch("app.routers.web_ui.get_model_routing_snapshot", return_value=routing):
            resp = client.get("/dashboard/api/model-breakdown", headers=web_auth_header(sub=test_user.username))
            page = client.get("/dashboard", headers=web_auth_header(sub=test_user.username)).text
        rows = [r["model"] for r in resp.json()["groups"][0]["rows"]]
        # llm (configured, natural order), llm not in config, then embedding.
        assert rows == ["qwen3-8b", "qwen3-32b", "retired-model", "emb"]

        card = page[page.index('id="mbBody"'):]
        assert _order(card, rows) == rows

    def test_doughnut_still_ranks_by_cost(self, client, test_user):
        page = client.get("/dashboard", headers=web_auth_header(sub=test_user.username)).text
        assert "g.rows.slice().sort(function (a, b) { return b.cost_usd - a.cost_usd; })" in page


class TestSortOrderConfig:
    BASE = {"real_model": "rm", "base_url": "http://mock:8000/v1", "api_key": "", "type": "llm"}

    def _put(self, client, admin_user, extra):
        body = {
            "models": {"m1": {**self.BASE, **extra}},
            "pricing": {"_default": {"input_price_per_1m": 0.1, "output_price_per_1m": 0.1}},
            "fallback": {},
        }
        with patch("app.routers.admin.save_config") as saved:
            resp = client.put("/admin/api/config", json=body,
                              headers=web_auth_header(sub=admin_user.username, scopes=["admin"]))
        return resp, saved

    def test_accepts_integer(self, client, admin_user):
        resp, saved = self._put(client, admin_user, {"sort_order": 3})
        assert resp.status_code == 200, resp.text
        assert saved.call_args.args[0]["m1"]["sort_order"] == 3

    def test_loaded_from_config_for_every_backend(self):
        from app.core.config import _build_config

        raw = {
            "models": {"llm": {"m1": {"base_url": "http://a/v1", "sort_order": 2}}},
            "azure_models": {"az": {"endpoint": "https://x", "deployment": "d", "sort_order": 1}},
            "bedrock_models": {"br": {"model_id": "anthropic.x", "region": "us-east-1", "sort_order": 4}},
        }
        _, routing, _, _, azure, _, bedrock, _ = _build_config(raw)
        assert routing["m1"]["sort_order"] == 2
        assert azure["az"]["sort_order"] == 1
        assert bedrock["br"]["sort_order"] == 4

    @pytest.mark.parametrize("bad", [True, "1", 1.5, -1])
    def test_rejects_non_integers_and_negatives(self, client, admin_user, bad):
        resp, saved = self._put(client, admin_user, {"sort_order": bad})
        assert resp.status_code == 400
        assert "sort_order" in resp.json()["detail"]
        saved.assert_not_called()

    def test_not_surfaced_in_v1_models(self, client, db_session):
        from app.core.config import _MODEL_INTERNAL_KEYS, _MODEL_METADATA_KEYS

        assert "sort_order" in _MODEL_INTERNAL_KEYS and "sort_order" not in _MODEL_METADATA_KEYS
        db_session.add(User(username="lister"))
        db_session.commit()
        routing = {"m1": _route(sort_order=1)}
        with patch("app.core.config.MODEL_ROUTING", routing):
            resp = client.get("/v1/models", headers={"Authorization": "Bearer sk-testkey123"})
        assert resp.status_code == 200
        assert "sort_order" not in resp.text
