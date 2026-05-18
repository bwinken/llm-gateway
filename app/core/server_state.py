"""
Global httpx.AsyncClient manager and per-server health cache.

Two clients:
  - `_client`        — used for vLLM downstreams (internal LAN, no proxy)
  - `_azure_client`  — used for Azure OpenAI; routed through a corporate
                       HTTP proxy when AZURE_HTTP_PROXY is set, otherwise
                       falls back to the shared `_client`.

Keeping them separate means internal vLLM traffic never goes through the
corporate proxy, while external Azure calls can when the deployment needs it.
"""

from __future__ import annotations

import os

import httpx

_client: httpx.AsyncClient | None = None
_azure_client: httpx.AsyncClient | None = None
_health_cache: dict[str, bool] = {}
# Per-vLLM-server load snapshot scraped from /metrics, keyed by base_url.
# Each value is {"running": int, "waiting": int}. Absent when the server's
# /metrics endpoint is unreachable or disabled.
_metrics_cache: dict[str, dict[str, int]] = {}


async def init_client() -> None:
    global _client, _azure_client
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=40),
        follow_redirects=True,
    )
    # Dedicated Azure client through a corporate proxy, if configured.
    # AZURE_HTTP_PROXY accepts an inline-credentials URL too, e.g.
    # http://user:pass@proxy.company.local:8080
    azure_proxy = os.getenv("AZURE_HTTP_PROXY", "").strip()
    if azure_proxy:
        _azure_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=40),
            follow_redirects=True,
            proxy=azure_proxy,
        )


async def close_client() -> None:
    global _client, _azure_client
    if _client is not None:
        await _client.aclose()
        _client = None
    if _azure_client is not None:
        await _azure_client.aclose()
        _azure_client = None


def get_client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("httpx client not initialised – call init_client() first")
    return _client


def get_azure_client() -> httpx.AsyncClient:
    """Return the Azure-bound client.

    Uses the dedicated proxied client when AZURE_HTTP_PROXY is set, otherwise
    falls back to the shared client (Azure reachable directly).
    """
    if _azure_client is not None:
        return _azure_client
    return get_client()


def set_alive(base_url: str, alive: bool) -> None:
    _health_cache[base_url] = alive


def is_alive(base_url: str) -> bool:
    return _health_cache.get(base_url, False)


def set_metrics(base_url: str, metrics: dict[str, int] | None) -> None:
    """Store (or clear) the load snapshot for a vLLM server.

    Pass None to drop the entry — used when /metrics is unreachable so the
    dashboard falls back to a plain ONLINE/DOWN indicator.
    """
    if metrics is None:
        _metrics_cache.pop(base_url, None)
    else:
        _metrics_cache[base_url] = metrics


def get_metrics(base_url: str) -> dict[str, int] | None:
    """Return the latest load snapshot for a vLLM server, or None."""
    return _metrics_cache.get(base_url)


def prune_cache(active_urls: set[str]) -> None:
    """Remove cache entries for base_urls no longer in MODEL_ROUTING."""
    stale = [url for url in _health_cache if url not in active_urls]
    for url in stale:
        del _health_cache[url]
    stale_metrics = [url for url in _metrics_cache if url not in active_urls]
    for url in stale_metrics:
        del _metrics_cache[url]


def all_health() -> dict[str, bool]:
    return dict(_health_cache)
