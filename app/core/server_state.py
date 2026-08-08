"""
Global httpx.AsyncClient manager and per-server health cache.

Four clients:
  - `_client`         — used for vLLM downstreams (internal LAN, no proxy)
  - `_health_client`  — used ONLY by the background health checker. A health
                        probe sharing `_client` competes for the same
                        connection pool as user traffic, so pool exhaustion
                        (many long-lived streams) makes every probe fail with
                        PoolTimeout and flips all servers to DOWN even though
                        the containers are healthy.
  - `_azure_client`   — used for Azure OpenAI; routed through a corporate
                        HTTP proxy when AZURE_HTTP_PROXY is set, otherwise
                        falls back to the shared `_client`.
  - `_bedrock_client` — used for AWS Bedrock; same convention with
                        BEDROCK_HTTP_PROXY / BEDROCK_INSECURE.

Keeping them separate means internal vLLM traffic never goes through the
corporate proxy, while external cloud calls can when the deployment needs it.
"""

from __future__ import annotations

import os

import httpx

_client: httpx.AsyncClient | None = None
_health_client: httpx.AsyncClient | None = None
_azure_client: httpx.AsyncClient | None = None
_bedrock_client: httpx.AsyncClient | None = None
# Shared-client pool size, kept for utilization reporting (pool_snapshot).
_client_max_connections: int = 0
_health_cache: dict[str, bool] = {}
# Per-vLLM-server load snapshot scraped from /metrics, keyed by base_url.
# Each value is {"running": int, "waiting": int}. Absent when the server's
# /metrics endpoint is unreachable or disabled.
_metrics_cache: dict[str, dict[str, int]] = {}


def _make_cloud_client(proxy: str, insecure: bool) -> httpx.AsyncClient:
    # retries=1 covers connection ESTABLISHMENT only (httpx never re-sends a
    # request that already went out), so a transient TCP blip costs one silent
    # reconnect instead of a user-visible 502.
    transport_kwargs: dict = dict(
        retries=1,
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=40),
        verify=not insecure,
    )
    if proxy:
        transport_kwargs["proxy"] = proxy
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
        transport=httpx.AsyncHTTPTransport(**transport_kwargs),
    )


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


async def init_client() -> None:
    global _client, _health_client, _azure_client, _bedrock_client, _client_max_connections
    # Each in-flight streaming request holds one pool connection for its whole
    # duration (minutes, for agentic clients), so the pool must be sized for
    # peak concurrent streams — not average request rate. Downstreams are
    # internal LAN servers, so idle capacity is cheap.
    _client_max_connections = _env_int("DOWNSTREAM_MAX_CONNECTIONS", 1000)
    # retries=1 on the transport retries CONNECTION ESTABLISHMENT only — a
    # request that was already sent is never replayed — so a transient TCP
    # blip on the LAN costs one silent reconnect instead of a 502.
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
        transport=httpx.AsyncHTTPTransport(
            retries=1,
            limits=httpx.Limits(
                max_connections=_client_max_connections,
                max_keepalive_connections=_env_int("DOWNSTREAM_MAX_KEEPALIVE_CONNECTIONS", 100),
            ),
        ),
    )
    # Dedicated health-probe client: its pool must never be starved by user
    # traffic, so a probe failure always means the downstream itself is bad.
    _health_client = httpx.AsyncClient(
        timeout=httpx.Timeout(5.0),
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        follow_redirects=True,
    )
    # Dedicated Azure client. Created when either a corporate HTTP proxy is
    # configured (AZURE_HTTP_PROXY) or SSL verification needs to be disabled
    # (AZURE_INSECURE=true, e.g. when traversing a corporate TLS-inspecting
    # proxy that re-signs certificates with an untrusted CA).
    # AZURE_HTTP_PROXY accepts inline credentials: http://user:pass@host:port
    azure_proxy = os.getenv("AZURE_HTTP_PROXY", "").strip()
    azure_insecure = _env_flag("AZURE_INSECURE")
    if azure_proxy or azure_insecure:
        _azure_client = _make_cloud_client(azure_proxy, azure_insecure)

    # Dedicated Bedrock client — same convention as Azure above.
    bedrock_proxy = os.getenv("BEDROCK_HTTP_PROXY", "").strip()
    bedrock_insecure = _env_flag("BEDROCK_INSECURE")
    if bedrock_proxy or bedrock_insecure:
        _bedrock_client = _make_cloud_client(bedrock_proxy, bedrock_insecure)


async def close_client() -> None:
    global _client, _health_client, _azure_client, _bedrock_client
    if _client is not None:
        await _client.aclose()
        _client = None
    if _health_client is not None:
        await _health_client.aclose()
        _health_client = None
    if _azure_client is not None:
        await _azure_client.aclose()
        _azure_client = None
    if _bedrock_client is not None:
        await _bedrock_client.aclose()
        _bedrock_client = None


def get_client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("httpx client not initialised – call init_client() first")
    return _client


def get_health_client() -> httpx.AsyncClient:
    """Return the dedicated health-probe client.

    Falls back to the shared client when uninitialised (tests patch
    `get_client` and never call `init_client`).
    """
    if _health_client is not None:
        return _health_client
    return get_client()


def pool_snapshot() -> dict[str, int] | None:
    """Best-effort view of the shared client's connection pool utilization.

    Reads httpcore internals, so any layout change just returns None instead
    of breaking the caller. `in_use` counts non-idle connections (each one is
    an in-flight downstream request, typically a long-lived stream).
    """
    if _client is None:
        return None
    try:
        pool = _client._transport._pool  # httpcore.AsyncConnectionPool
        conns = list(pool.connections)
        in_use = sum(1 for c in conns if not c.is_idle())
        return {
            "connections": len(conns),
            "in_use": in_use,
            "max": _client_max_connections,
        }
    except Exception:
        return None


def get_azure_client() -> httpx.AsyncClient:
    """Return the Azure-bound client.

    Uses the dedicated proxied client when AZURE_HTTP_PROXY is set, otherwise
    falls back to the shared client (Azure reachable directly).
    """
    if _azure_client is not None:
        return _azure_client
    return get_client()


def get_bedrock_client() -> httpx.AsyncClient:
    """Return the Bedrock-bound client.

    Uses the dedicated proxied client when BEDROCK_HTTP_PROXY /
    BEDROCK_INSECURE is set, otherwise falls back to the shared client.
    """
    if _bedrock_client is not None:
        return _bedrock_client
    return get_client()


def set_alive(base_url: str, alive: bool) -> None:
    _health_cache[base_url] = alive


def mark_down(base_url: str) -> None:
    """Request-layer circuit break: a connect attempt to this server just
    failed, so flip it DOWN immediately instead of letting requests 502 for
    up to a full health-check interval. The next successful probe (or a
    request-layer recovery) flips it back UP."""
    _health_cache[base_url] = False


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
