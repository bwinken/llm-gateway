"""
Parse config.toml, inject type tags, build MODEL_ROUTING, PRICING_MAP,
FALLBACK_MAP, and APP_CONFIG.

Each model entry in MODEL_ROUTING contains:
  - base_url:        downstream server URL
  - real_model:      actual model name to send downstream
  - api_key:         downstream API key (from config.toml)
  - type:            injected from parent section (llm, vlm, embedding, ...)
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import toml
from dotenv import load_dotenv

load_dotenv()

_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.toml"

APP_TITLE: str = os.getenv("APP_TITLE", "LLM Gateway")
DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://llm_gateway:password@localhost:5432/llm_gateway")

# AuthCenter JWT validation (oauth2-proxy handles login flow)
AUTH_CENTER_APP_ID: str = os.getenv("AUTH_CENTER_APP_ID", "llm_gateway")
AUTH_CENTER_PUBLIC_KEY_PATH: str = os.getenv("AUTH_CENTER_PUBLIC_KEY_PATH", "./keys/public.pem")
AUTH_BASE_URL: str = os.getenv("AUTH_BASE_URL", "auth-center")


# Optional per-model metadata that can be declared in config.toml and is
# surfaced to clients via GET /v1/models. Unknown/unspecified keys are
# simply omitted from the response — the endpoint stays backward
# compatible for any model entry that doesn't declare them.
_MODEL_METADATA_KEYS: tuple[str, ...] = (
    "display_name",
    "context_window",
    "max_output_tokens",
    "supports_tools",
    "supports_vision",
    "supports_prompt_caching",
)

# Internal config keys stored in config.toml and loaded into MODEL_ROUTING
# but NOT surfaced to API clients via GET /v1/models.
_MODEL_INTERNAL_KEYS: tuple[str, ...] = (
    "hidden",
)


def _load_toml() -> dict[str, Any]:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return toml.load(f)


def _build_config(raw: dict[str, Any]) -> tuple[
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, dict[str, float]],
    dict[str, str],
]:
    """Return (APP_CONFIG, MODEL_ROUTING, PRICING_MAP, FALLBACK_MAP)."""

    # --- app ---
    app_config: dict[str, Any] = raw.get("app", {})

    # --- pricing ---
    pricing_section = raw.get("pricing", {})
    default_input = float(pricing_section.get("default_input_price_per_1m", 0.10))
    default_output = float(pricing_section.get("default_output_price_per_1m", 0.10))

    pricing_map: dict[str, dict[str, float]] = {
        "_default": {
            "input_price_per_1m": default_input,
            "output_price_per_1m": default_output,
        }
    }
    for type_key, prices in pricing_section.items():
        if not isinstance(prices, dict):
            continue
        pricing_map[type_key] = {
            "input_price_per_1m": float(prices.get("input_price_per_1m", default_input)),
            "output_price_per_1m": float(prices.get("output_price_per_1m", default_output)),
        }

    # --- models ---
    model_routing: dict[str, dict[str, Any]] = {}
    for type_key, models in raw.get("models", {}).items():
        for model_name, model_cfg in models.items():
            entry: dict[str, Any] = {
                "base_url": model_cfg.get("base_url", ""),
                "real_model": model_cfg.get("real_model", model_name),
                "api_key": model_cfg.get("api_key", ""),
                "type": type_key,
            }
            # Optional metadata surfaced via GET /v1/models. Missing keys
            # are simply omitted from the response so existing configs
            # keep working unchanged.
            for meta_key in _MODEL_METADATA_KEYS:
                if meta_key in model_cfg:
                    entry[meta_key] = model_cfg[meta_key]
            for internal_key in _MODEL_INTERNAL_KEYS:
                if internal_key in model_cfg:
                    entry[internal_key] = model_cfg[internal_key]
            model_routing[model_name] = entry

    # --- fallback (type -> preferred fallback model alias) ---
    fallback_map: dict[str, str] = {}
    for type_key, alias in raw.get("fallback", {}).items():
        if isinstance(alias, str):
            fallback_map[type_key] = alias

    return app_config, model_routing, pricing_map, fallback_map


_raw = _load_toml()
APP_CONFIG, MODEL_ROUTING, PRICING_MAP, FALLBACK_MAP = _build_config(_raw)

_config_lock = threading.Lock()
_config_mtime: float = _CONFIG_PATH.stat().st_mtime


def _check_auto_reload() -> None:
    """Reload config if the file has been modified (handles multi-worker sync)."""
    global _config_mtime
    try:
        mtime = _CONFIG_PATH.stat().st_mtime
    except OSError:
        return
    if mtime != _config_mtime:
        _config_mtime = mtime
        reload_config()


def get_model_routing_snapshot() -> dict[str, dict[str, Any]]:
    """Return a shallow copy of MODEL_ROUTING safe for iteration."""
    _check_auto_reload()
    return dict(MODEL_ROUTING)


def reload_config() -> None:
    """Re-read config.toml and update all globals in-place.

    Updates before removing stale keys to avoid a transient empty state.
    """
    global _config_mtime
    raw = _load_toml()
    try:
        _config_mtime = _CONFIG_PATH.stat().st_mtime
    except OSError:
        pass
    _, new_routing, new_pricing, new_fallback = _build_config(raw)

    # Pre-compute stale keys outside the lock
    stale_routing = set(MODEL_ROUTING) - set(new_routing)
    stale_pricing = set(PRICING_MAP) - set(new_pricing)
    stale_fallback = set(FALLBACK_MAP) - set(new_fallback)

    with _config_lock:
        # Swap all three dicts as close together as possible
        MODEL_ROUTING.update(new_routing)
        PRICING_MAP.update(new_pricing)
        FALLBACK_MAP.update(new_fallback)
        for k in stale_routing:
            MODEL_ROUTING.pop(k, None)
        for k in stale_pricing:
            PRICING_MAP.pop(k, None)
        for k in stale_fallback:
            FALLBACK_MAP.pop(k, None)


def save_config(
    models: dict[str, dict[str, Any]],
    pricing: dict[str, dict[str, float]],
    fallback: dict[str, str],
) -> None:
    """Write config back to config.toml and reload globals."""
    raw = _load_toml()

    # Rebuild [models.*] sections grouped by type
    models_section: dict[str, dict[str, Any]] = {}
    for alias, info in models.items():
        type_key = info["type"]
        if type_key not in models_section:
            models_section[type_key] = {}
        entry: dict[str, Any] = {
            "real_model": info["real_model"],
            "base_url": info["base_url"],
        }
        if info.get("api_key"):
            entry["api_key"] = info["api_key"]
        # Preserve any metadata fields the caller passed through so a save
        # from the admin UI doesn't silently wipe context_window etc.
        for meta_key in _MODEL_METADATA_KEYS:
            if meta_key in info:
                entry[meta_key] = info[meta_key]
        for internal_key in _MODEL_INTERNAL_KEYS:
            if internal_key in info:
                entry[internal_key] = info[internal_key]
        models_section[type_key][alias] = entry
    raw["models"] = models_section

    # Rebuild [pricing] section
    pricing_section: dict[str, Any] = {}
    for type_key, prices in pricing.items():
        if type_key == "_default":
            pricing_section["default_input_price_per_1m"] = prices["input_price_per_1m"]
            pricing_section["default_output_price_per_1m"] = prices["output_price_per_1m"]
        else:
            pricing_section[type_key] = {
                "input_price_per_1m": prices["input_price_per_1m"],
                "output_price_per_1m": prices["output_price_per_1m"],
            }
    raw["pricing"] = pricing_section

    # Rebuild [fallback] section
    raw["fallback"] = {k: v for k, v in fallback.items() if v}

    # Atomic write: write to temp file then rename to prevent corruption
    dir_path = _CONFIG_PATH.parent
    fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".toml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            toml.dump(raw, f)
        os.replace(tmp_path, _CONFIG_PATH)
    except BaseException:
        os.unlink(tmp_path)
        raise

    reload_config()


def get_config_data() -> dict[str, Any]:
    """Return current config as a JSON-serializable dict.

    Auto-reloads from config.toml if file has changed (multi-worker safe).
    """
    _check_auto_reload()
    return {
        "models": dict(MODEL_ROUTING),
        "pricing": dict(PRICING_MAP),
        "fallback": dict(FALLBACK_MAP),
    }
