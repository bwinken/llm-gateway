"""Tests for the shared SSE pump's max-idle guard and the health checker's
pool-starvation handling.

Together these cover the "dashboard shows every model DOWN while the
containers are alive" failure mode: hung downstream streams used to hold
shared-pool connections forever, and once the pool was exhausted the health
probes (which shared that pool) all failed with PoolTimeout.
"""

import asyncio
from unittest.mock import patch

import httpx

from app.core.server_state import is_alive as real_is_alive, set_alive
from app.services import health
from app.services.vllm_proxy import _pump_sse_lines


class _StalledResponse:
    """Streaming response that never produces a line (dead downstream)."""

    def __init__(self):
        self.closed = False

    async def aiter_lines(self):
        await asyncio.sleep(3600)
        yield ""  # pragma: no cover

    async def aclose(self):
        self.closed = True


class _LinesResponse:
    def __init__(self, lines):
        self._lines = lines
        self.closed = False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aclose(self):
        self.closed = True


async def _collect(pump):
    events = []
    async for kind, data in pump:
        events.append((kind, data))
    return events


class TestPumpMaxIdle:
    def test_aborts_after_max_idle_and_closes_response(self):
        resp = _StalledResponse()

        async def scenario():
            async def send():
                return resp
            return await _collect(
                _pump_sse_lines(send(), ping_interval=0.02, max_idle=0.05)
            )

        events = asyncio.run(scenario())
        kinds = [k for k, _ in events]
        assert kinds[-1] == "err"
        assert isinstance(events[-1][1], TimeoutError)
        assert "ping" in kinds  # idle ticks emitted before giving up
        assert resp.closed  # the pool connection was recycled

    def test_passes_lines_through_and_closes(self):
        resp = _LinesResponse(['data: {"x":1}', "data: [DONE]"])

        async def scenario():
            async def send():
                return resp
            return await _collect(_pump_sse_lines(send()))

        events = asyncio.run(scenario())
        assert events == [
            ("line", 'data: {"x":1}'),
            ("line", "data: [DONE]"),
            ("done", None),
        ]
        assert resp.closed


class _PoolStarvedClient:
    async def get(self, *args, **kwargs):
        raise httpx.PoolTimeout("pool exhausted")


class _DeadClient:
    async def get(self, *args, **kwargs):
        raise httpx.ConnectError("connection refused")


def _run_check(client, routing):
    with patch.object(health, "get_health_client", return_value=client), \
         patch.object(health, "MODEL_ROUTING", routing), \
         patch.object(health, "_check_auto_reload", lambda: None):
        asyncio.run(health.check_all_servers())


class TestHealthPoolStarvation:
    def test_pool_timeout_keeps_previous_alive_state(self):
        url = "http://idle-guard-test-a:8000/v1"
        set_alive(url, True)
        _run_check(_PoolStarvedClient(), {"m": {"base_url": url, "api_key": ""}})
        assert real_is_alive(url) is True

    def test_connect_error_still_marks_down(self):
        url = "http://idle-guard-test-b:8000/v1"
        set_alive(url, True)
        _run_check(_DeadClient(), {"m": {"base_url": url, "api_key": ""}})
        assert real_is_alive(url) is False
