"""One display order for models (app/services/model_order.py), used by the
welcome page, the dashboard's model tables and its Cost by Model card."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.models.schema import UsageLog
from app.services.model_order import (
    catalog_key,
    natural_key,
    positions,
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

    def test_arranged_first_then_price_then_name(self):
        pos = positions(["second", "first"][::-1])  # first, second
        rows = [("cheap", 1.0), ("second", 99.0), ("first", 50.0), ("b-same", 5.0), ("a-same", 5.0)]
        ordered = sorted(rows, key=lambda r: catalog_key(r[0], pos.get(r[0]), r[1]))
        assert [r[0] for r in ordered] == ["first", "second", "cheap", "a-same", "b-same"]

    def test_unknown_models_after_configured(self):
        assert catalog_key("zzz", None, 1e9) < unknown_key("aaa")

    def test_type_rank(self):
        assert type_rank("llm") < type_rank("vlm") < type_rank("systemone") < type_rank("audio") < type_rank("zzz")


def _order_patch(vllm=(), azure=(), bedrock=()):
    return patch("app.routers.web_ui.get_display_order_snapshot",
                 return_value={"vllm": list(vllm), "azure": list(azure), "bedrock": list(bedrock)})


class TestDashboardTables:
    def _dashboard(self, client, user, routing, alive=lambda url: True, order=()):
        with (
            patch("app.routers.web_ui.get_model_routing_snapshot", return_value=routing),
            patch("app.routers.web_ui.is_alive", side_effect=alive),
            _order_patch(vllm=order),
        ):
            return client.get("/dashboard", headers=web_auth_header(sub=user.username)).text

    def test_same_price_models_sort_naturally(self, client, test_user):
        routing = {n: _route(host=n) for n in ("qwen3-235b", "Qwen3-8b", "qwen3-32b")}
        body = self._dashboard(client, test_user, routing)
        assert _order(body, list(routing)) == ["Qwen3-8b", "qwen3-32b", "qwen3-235b"]

    def test_arranged_first_and_label_says_so(self, client, test_user):
        routing = {
            "cheap": _route(host="c", input_price_per_1m=0.1, output_price_per_1m=0.1),
            "flagship": _route(host="f", input_price_per_1m=9, output_price_per_1m=9),
            "added-later": _route(host="n", input_price_per_1m=0.01, output_price_per_1m=0.01),
        }
        body = self._dashboard(client, test_user, routing, order=["flagship", "cheap", "deleted-alias"])
        # Arranged ones in their order, then the unplaced one even though cheapest.
        assert _order(body, list(routing)) == ["flagship", "cheap", "added-later"]
        assert "arranged by an admin" in body

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

    def test_arranged_order_applies_to_the_card(self, client, db_session, test_user):
        routing = {n: _route(host=n) for n in ("qwen3-8b", "qwen3-32b")}
        self._spend(db_session, test_user, "qwen3-8b", 1.0)
        self._spend(db_session, test_user, "qwen3-32b", 0.5)
        with (
            patch("app.routers.web_ui.get_model_routing_snapshot", return_value=routing),
            _order_patch(vllm=["qwen3-32b", "qwen3-8b"]),
        ):
            resp = client.get("/dashboard/api/model-breakdown", headers=web_auth_header(sub=test_user.username))
        assert [r["model"] for r in resp.json()["groups"][0]["rows"]] == ["qwen3-32b", "qwen3-8b"]

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


class TestDisplayOrderConfig:
    BASE = {"real_model": "rm", "base_url": "http://mock:8000/v1", "api_key": "", "type": "llm"}

    def _put(self, client, admin_user, display_order, models=("m1", "m2")):
        body = {
            "models": {m: dict(self.BASE) for m in models},
            "pricing": {"_default": {"input_price_per_1m": 0.1, "output_price_per_1m": 0.1}},
            "fallback": {},
            "display_order": display_order,
        }
        with patch("app.routers.admin.save_config") as saved:
            resp = client.put("/admin/api/config", json=body,
                              headers=web_auth_header(sub=admin_user.username, scopes=["admin"]))
        return resp, saved

    def test_saved_pruned_and_deduplicated(self, client, admin_user):
        resp, saved = self._put(client, admin_user, {"vllm": ["m2", "gone", "m1", "m2"]})
        assert resp.status_code == 200, resp.text
        order = saved.call_args.args[7]
        assert order["vllm"] == ["m2", "m1"]
        assert order["azure"] == [] and order["bedrock"] == []

    def test_omitted_leaves_the_section_alone(self, client, admin_user):
        resp, saved = self._put(client, admin_user, None)
        assert resp.status_code == 200
        assert saved.call_args.args[7] is None

    def test_cloud_backend_omitted_from_payload_checks_current_models(self, client, admin_user):
        # No azure_models in the body: aliases are checked against the live config.
        resp, saved = self._put(client, admin_user, {"azure": ["azure-gpt-4", "nope"]})
        assert resp.status_code == 200, resp.text
        assert saved.call_args.args[7]["azure"] == ["azure-gpt-4"]

    @pytest.mark.parametrize("bad", [["m1"], {"vllm": "m1"}, {"vllm": [1]}, {"openai": ["m1"]}])
    def test_rejects_bad_shapes(self, client, admin_user, bad):
        resp, saved = self._put(client, admin_user, bad)
        assert resp.status_code == 400
        assert "display_order" in resp.json()["detail"]
        saved.assert_not_called()

    def test_loaded_from_config(self):
        from app.core.config import _build_display_order

        raw = {"display_order": {"vllm": ["b", "a", "b", 3], "azure": "not-a-list", "other": ["x"]}}
        assert _build_display_order(raw) == {"vllm": ["b", "a"], "azure": [], "bedrock": []}
        assert _build_display_order({}) == {"vllm": [], "azure": [], "bedrock": []}

    def test_save_config_writes_and_drops_the_section(self, tmp_path):
        import tomllib

        from app.core import config as cfg

        path = tmp_path / "config.toml"
        path.write_text('[models.llm.m1]\nbase_url = "http://a/v1"\n')
        with (
            patch.object(cfg, "_CONFIG_PATH", path),
            patch.object(cfg, "reload_config"),
            patch.object(cfg, "_load_toml", side_effect=lambda: tomllib.loads(path.read_text())),
        ):
            models = {"m1": {"type": "llm", "base_url": "http://a/v1", "real_model": "m1"}}
            pricing = {"_default": {"input_price_per_1m": 0.1, "output_price_per_1m": 0.1}}
            cfg.save_config(models, pricing, {}, display_order={"vllm": ["m1"], "azure": [], "bedrock": []})
            written = tomllib.loads(path.read_text())
            assert written["display_order"] == {"vllm": ["m1"]}
            cfg.save_config(models, pricing, {}, display_order={"vllm": [], "azure": [], "bedrock": []})
            assert "display_order" not in tomllib.loads(path.read_text())

    def test_config_api_returns_it(self, client, admin_user):
        with patch.dict("app.core.config.DISPLAY_ORDER", {"vllm": ["test-llm"], "azure": [], "bedrock": []}):
            data = client.get("/admin/api/config",
                              headers=web_auth_header(sub=admin_user.username, scopes=["admin"])).json()
        assert data["display_order"]["vllm"] == ["test-llm"]

    def test_not_a_per_model_key(self):
        from app.core.config import _MODEL_INTERNAL_KEYS, _MODEL_METADATA_KEYS

        assert "sort_order" not in _MODEL_INTERNAL_KEYS + _MODEL_METADATA_KEYS
