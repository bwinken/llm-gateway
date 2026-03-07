"""
Global httpx.AsyncClient manager and per-server health cache.
"""

from __future__ import annotations

import httpx

_client: httpx.AsyncClient | None = None
_health_cache: dict[str, bool] = {}


async def init_client() -> None:
    global _client
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=40),
        follow_redirects=True,
    )


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def get_client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("httpx client not initialised – call init_client() first")
    return _client


def set_alive(base_url: str, alive: bool) -> None:
    _health_cache[base_url] = alive


def is_alive(base_url: str) -> bool:
    return _health_cache.get(base_url, False)


def all_health() -> dict[str, bool]:
    return dict(_health_cache)
