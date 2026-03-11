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
from pathlib import Path
from typing import Any

import toml
from dotenv import load_dotenv

load_dotenv()

_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.toml"

APP_TITLE: str = os.getenv("APP_TITLE", "LLM Gateway")
SECRET_KEY: str = os.getenv("SECRET_KEY", "change-me")
DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://llm_gateway:password@localhost:5432/llm_gateway")
DEFAULT_ADMIN_KEY: str = os.getenv("DEFAULT_ADMIN_KEY", "sk-admin-key-change-me")
DEFAULT_ADMIN_USER: str = os.getenv("DEFAULT_ADMIN_USER", "admin")
DEFAULT_ADMIN_DAILY_LIMIT: float = float(os.getenv("DEFAULT_ADMIN_DAILY_LIMIT", "100.0"))

# AuthCenter OAuth2 SSO
AUTH_CENTER_BASE_URL: str = os.getenv("AUTH_CENTER_BASE_URL", "http://localhost:8000")
AUTH_CENTER_APP_ID: str = os.getenv("AUTH_CENTER_APP_ID", "llm_gateway")
AUTH_CENTER_CLIENT_SECRET: str = os.getenv("AUTH_CENTER_CLIENT_SECRET", "change-me")
AUTH_CENTER_REDIRECT_URI: str = os.getenv("AUTH_CENTER_REDIRECT_URI", "http://localhost:8050/auth/callback")
AUTH_CENTER_PUBLIC_KEY_PATH: str = os.getenv("AUTH_CENTER_PUBLIC_KEY_PATH", "./keys/public.pem")


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
            model_routing[model_name] = {
                "base_url": model_cfg.get("base_url", ""),
                "real_model": model_cfg.get("real_model", model_name),
                "api_key": model_cfg.get("api_key", ""),
                "type": type_key,
            }

    # --- fallback (type -> preferred fallback model alias) ---
    fallback_map: dict[str, str] = {}
    for type_key, alias in raw.get("fallback", {}).items():
        if isinstance(alias, str):
            fallback_map[type_key] = alias

    return app_config, model_routing, pricing_map, fallback_map


_raw = _load_toml()
APP_CONFIG, MODEL_ROUTING, PRICING_MAP, FALLBACK_MAP = _build_config(_raw)


def reload_config() -> None:
    """Re-read config.toml and update all globals in-place.

    Updates before removing stale keys to avoid a transient empty state.
    """
    raw = _load_toml()
    _, new_routing, new_pricing, new_fallback = _build_config(raw)

    # Update first, then remove stale keys (never leaves dicts empty mid-swap)
    stale = set(MODEL_ROUTING) - set(new_routing)
    MODEL_ROUTING.update(new_routing)
    for k in stale:
        MODEL_ROUTING.pop(k, None)

    stale = set(PRICING_MAP) - set(new_pricing)
    PRICING_MAP.update(new_pricing)
    for k in stale:
        PRICING_MAP.pop(k, None)

    stale = set(FALLBACK_MAP) - set(new_fallback)
    FALLBACK_MAP.update(new_fallback)
    for k in stale:
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

    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        toml.dump(raw, f)

    reload_config()


def get_config_data() -> dict[str, Any]:
    """Return current config as a JSON-serializable dict."""
    return {
        "models": dict(MODEL_ROUTING),
        "pricing": dict(PRICING_MAP),
        "fallback": dict(FALLBACK_MAP),
    }
