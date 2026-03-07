"""
Background task: periodically ping each backend server's /models endpoint.
"""

from __future__ import annotations

import asyncio

from app.core.config import MODEL_ROUTING
from app.core.logger import logger
from app.core.server_state import get_client, set_alive


async def check_all_servers() -> None:
    """Ping every unique base_url in MODEL_ROUTING concurrently."""
    seen: dict[str, str] = {}  # base_url -> api_key
    client = get_client()

    # Collect unique base_urls with their first api_key (snapshot to avoid mutation during iter)
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

    await asyncio.gather(*[_ping(url, key) for url, key in seen.items()])


async def health_check_loop(interval: int = 30) -> None:
    """Run health checks in an infinite loop."""
    while True:
        try:
            await check_all_servers()
        except Exception as exc:
            logger.error("Health check error: {}", exc)
        await asyncio.sleep(interval)
