"""
Parse config.toml, inject type tags, build MODEL_ROUTING, PRICING_MAP, and APP_CONFIG.

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

SECRET_KEY: str = os.getenv("SECRET_KEY", "change-me")
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./llm_gateway.db")
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
]:
    """Return (APP_CONFIG, MODEL_ROUTING, PRICING_MAP) from raw TOML data."""

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

    return app_config, model_routing, pricing_map


_raw = _load_toml()
APP_CONFIG, MODEL_ROUTING, PRICING_MAP = _build_config(_raw)
