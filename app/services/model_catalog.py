"""The models an account can see, with what each costs and whether it is up.

Shared by the dashboard (``web_ui``) and the account endpoints
(``GET /v1/status``, ``GET /v1/usage``) so both show the same models, at the
same prices, in the same order (``model_order``).
"""

from __future__ import annotations

from typing import Any

from app.core import config
from app.core.server_state import get_metrics, is_alive
from app.models.schema import User
from app.services.model_order import catalog_key, positions, type_rank, unknown_key

# ``usage_logs.backend`` → the name the account endpoints use (``/v1/me``).
BACKEND_NAMES: dict[str, str] = {"vllm": "onprem", "azure": "azure", "bedrock": "aws"}
BACKEND_ORDER: tuple[str, ...] = ("vllm", "azure", "bedrock")



def resolve_prices(entry: dict, model_type: str, pricing_map: dict) -> tuple[float, float]:
    """(input, output) USD per 1M tokens for a model entry.

    Same priority as billing's ``_calc_cost``: per-model override (both
    ``input_price_per_1m`` and ``output_price_per_1m`` present) → per-type
    ``[pricing.<type>]`` → ``_default`` — so what a page shows is exactly
    what the request will be billed at.
    """
    if "input_price_per_1m" in entry and "output_price_per_1m" in entry:
        return float(entry["input_price_per_1m"]), float(entry["output_price_per_1m"])
    p = pricing_map.get(model_type) or pricing_map.get("_default") or {}
    return float(p.get("input_price_per_1m", 0.0)), float(p.get("output_price_per_1m", 0.0))


def hidden_cloud_backends(user: User) -> frozenset[str]:
    """Cloud backends this user may not use: nothing of them is shown (spend
    columns, Cost by Model rows, model tables), even when spend from before
    an admin revoked access remains. On the web UI an AuthCenter admin (the
    token's scope, carried in ``user.is_admin`` by ``get_web_user``) sees
    everything; an API-key caller is never one."""
    hidden = set()
    if not (user.can_use_azure or user.is_admin):
        hidden.add("azure")
    if not (user.can_use_bedrock or user.is_admin):
        hidden.add("bedrock")
    return frozenset(hidden)


def catalog_keys(
    snapshots: dict[str, dict[str, dict]], pricing_map: dict, order: dict[str, dict[str, int]],
) -> dict[tuple[str, str], tuple]:
    """``(backend, alias) -> catalog_key`` for every configured model, hidden
    ones included (their past usage still has to sort sensibly).
    ``snapshots`` is ``{backend: {alias: entry}}``, ``order`` the
    ``[display_order]`` positions per backend."""
    keys: dict[tuple[str, str], tuple] = {}
    for backend, snapshot in snapshots.items():
        for alias, entry in snapshot.items():
            in_price, out_price = resolve_prices(entry, entry.get("type", "llm"), pricing_map)
            keys[(backend, alias)] = catalog_key(alias, order[backend].get(alias), in_price + out_price)
    return keys


def usage_row_key(row: dict, catalog: dict[tuple[str, str], tuple]) -> tuple:
    """Where a usage row (``model``, ``type``, ``backend``) sits within its
    backend: by type, then the catalog order, models no longer configured
    last. The same place the model has in the model tables."""
    return (
        type_rank(row["type"]),
        catalog.get((row["backend"], row["model"])) or unknown_key(row["model"]),
    )


def _snapshots() -> dict[str, dict[str, dict]]:
    return {
        "vllm": config.get_model_routing_snapshot(),
        "azure": config.get_azure_models_snapshot(),
        "bedrock": config.get_bedrock_models_snapshot(),
    }


def _positions() -> dict[str, dict[str, int]]:
    return {
        backend: positions(aliases)
        for backend, aliases in config.get_display_order_snapshot().items()
    }


def current_catalog_keys() -> dict[tuple[str, str], tuple]:
    """``catalog_keys`` for the configuration as it is now."""
    return catalog_keys(_snapshots(), config.get_pricing_snapshot(), _positions())


def usable_models(user: User) -> list[dict[str, Any]]:
    """Every configured model ``user`` may ask for, hidden entries included
    (a hidden model is left out of the lists but answers by name): ``id``,
    ``backend``, ``type`` and ``display_name`` when declared. Cloud backends
    without access are left out, as a request for them is served elsewhere.
    No order; for what to show, use ``visible_models``."""
    hidden = hidden_cloud_backends(user)
    rows: list[dict[str, Any]] = []
    for backend, snapshot in _snapshots().items():
        if backend in hidden:
            continue
        for alias, entry in snapshot.items():
            row: dict[str, Any] = {"id": alias, "backend": BACKEND_NAMES[backend], "type": entry.get("type", "llm")}
            if entry.get("display_name"):
                row["display_name"] = entry["display_name"]
            rows.append(row)
    return rows


def visible_models(user: User) -> list[dict[str, Any]]:
    """Every model ``user`` can see, in display order: on-prem first, then
    the cloud backends the account may use; hidden entries left out.

    Each row has ``id``, ``backend`` (``onprem`` / ``azure`` / ``aws``),
    ``type``, ``state`` and the billed prices, plus the metadata the config
    declares (``config._MODEL_METADATA_KEYS``, as ``/v1/models`` passes it:
    display name, context window, capability flags).
    ``state`` is ``up`` / ``down`` for on-prem servers (the health loop's
    view, per worker), with ``running`` / ``waiting`` when the server's
    ``/metrics`` answered, and ``unchecked`` for cloud models, which the
    gateway does not probe. No value is ever None: absent keys are omitted.
    """
    pricing_map = config.get_pricing_snapshot()
    order = _positions()
    hidden = hidden_cloud_backends(user)
    rows: list[dict[str, Any]] = []
    for backend, snapshot in _snapshots().items():
        if backend in hidden:
            continue
        found: list[tuple[tuple, dict[str, Any]]] = []
        for alias, entry in snapshot.items():
            if entry.get("hidden"):
                continue
            model_type = entry.get("type", "llm")
            in_price, out_price = resolve_prices(entry, model_type, pricing_map)
            row: dict[str, Any] = {
                "id": alias,
                "backend": BACKEND_NAMES[backend],
                "type": model_type,
            }
            if backend == "vllm":
                base_url = entry["base_url"]
                row["state"] = "up" if is_alive(base_url) else "down"
                load = get_metrics(base_url) or {}
                for key in ("running", "waiting"):
                    if load.get(key) is not None:
                        row[key] = int(load[key])
            else:
                row["state"] = "unchecked"
            for key in config._MODEL_METADATA_KEYS:
                if entry.get(key) not in (None, ""):
                    row[key] = entry[key]
            row["input_price_per_1m"] = in_price
            row["output_price_per_1m"] = out_price
            sort_key = (
                type_rank(model_type),
                catalog_key(alias, order[backend].get(alias), in_price + out_price),
            )
            found.append((sort_key, row))
        found.sort(key=lambda item: item[0])
        rows.extend(row for _, row in found)
    return rows
