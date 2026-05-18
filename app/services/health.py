"""
Background task: periodically ping each backend server's /models endpoint
and scrape its vLLM /metrics endpoint for a load snapshot.
"""

from __future__ import annotations

import asyncio

from app.core.config import MODEL_ROUTING, _check_auto_reload
from app.core.logger import logger
from app.core.server_state import get_client, prune_cache, set_alive, set_metrics


def _metrics_url(base_url: str) -> str:
    """Derive the vLLM /metrics URL from an OpenAI-style base_url.

    vLLM serves /metrics at the server root, not under /v1, so a base_url
    of ``http://host:8000/v1`` maps to ``http://host:8000/metrics``.
    """
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return f"{root.rstrip('/')}/metrics"


def _parse_vllm_metrics(text: str) -> dict[str, int] | None:
    """Parse `vllm:num_requests_running` / `_waiting` from Prometheus text.

    Returns None when neither metric is present (endpoint disabled, wrong
    format, or not a vLLM server) so the caller can fall back gracefully.
    A vLLM server may export the metric once per model_name label; we sum
    across labels to get the server-wide total.
    """
    running: float | None = None
    waiting: float | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Format: `vllm:num_requests_running{labels...} 8.0`
        if line.startswith("vllm:num_requests_running"):
            val = _last_number(line)
            if val is not None:
                running = (running or 0.0) + val
        elif line.startswith("vllm:num_requests_waiting"):
            val = _last_number(line)
            if val is not None:
                waiting = (waiting or 0.0) + val
    if running is None and waiting is None:
        return None
    return {"running": int(running or 0), "waiting": int(waiting or 0)}


def _last_number(line: str) -> float | None:
    """Return the trailing numeric value of a Prometheus sample line."""
    try:
        return float(line.rsplit(None, 1)[-1])
    except (ValueError, IndexError):
        return None


async def check_all_servers() -> None:
    """Ping every unique base_url in MODEL_ROUTING concurrently."""
    seen: dict[str, str] = {}  # base_url -> api_key
    client = get_client()

    # Collect unique base_urls with their first api_key (snapshot to avoid mutation during iter)
    _check_auto_reload()
    for _model_name, route in list(MODEL_ROUTING.items()):
        base_url = route["base_url"]
        if base_url not in seen:
            seen[base_url] = route.get("api_key", "")

    async def _ping(base_url: str, api_key: str) -> None:
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            resp = await client.get(f"{base_url}/models", headers=headers, timeout=5.0)
            alive = resp.status_code == 200
        except Exception:
            alive = False
        set_alive(base_url, alive)
        if not alive:
            logger.warning("Server DOWN: {}", base_url)
            set_metrics(base_url, None)
            return

        # Scrape the load snapshot. Best-effort: any failure (endpoint
        # disabled, non-vLLM server, parse miss) just clears the entry so
        # the dashboard shows a plain ONLINE indicator with no load numbers.
        try:
            mresp = await client.get(
                _metrics_url(base_url), headers=headers, timeout=5.0,
            )
            metrics = (
                _parse_vllm_metrics(mresp.text)
                if mresp.status_code == 200 else None
            )
        except Exception:
            metrics = None
        set_metrics(base_url, metrics)

    await asyncio.gather(*[_ping(url, key) for url, key in seen.items()])
    prune_cache(set(seen.keys()))


async def health_check_loop(interval: int = 30) -> None:
    """Run health checks in an infinite loop."""
    while True:
        try:
            await check_all_servers()
        except Exception as exc:
            logger.error("Health check error: {}", exc)
        await asyncio.sleep(interval)
