"""The SSE / event-stream pumps must release the downstream connection when
the *client* disconnects mid-stream.

Starlette serves a ``StreamingResponse`` inside an anyio task group and
cancels that group the moment the client goes away. anyio keeps
re-delivering the cancellation at every ``await`` until the task leaves the
scope, so the pump's ``finally`` used to run under a hail of ``cancel()``
calls: its ``await task`` on the reader forwarded the second cancellation
into the reader task (asyncio cancels the future a cancelled task is
awaiting), and that second ``CancelledError`` landed inside httpcore's
*shielded* connection-close sequence. httpcore then dropped the pool
request but never closed the HTTP/1.1 socket, so every abandoned stream
permanently held one pool connection — 200 of them on the Azure client in
production, surfacing as ``PoolTimeout`` on every new Azure request.

The fake responses below model the essential httpcore behaviour: on
cancellation they run a close sequence that contains ONE suspension point
(``await asyncio.sleep(0)``) and only mark themselves closed if that
sequence completes. A second cancellation interrupts it, exactly like the
real thing.
"""

import asyncio

import anyio
import pytest

from app.services.bedrock_proxy import _pump_bedrock_events
from app.services.vllm_proxy import _pump_sse_lines


class _HttpcoreLikeResponse:
    """Streams forever; closes itself (with a suspension) on cancellation."""

    def __init__(self):
        self.closed = False
        self.close_interrupted = False

    async def _close_sequence(self):
        # httpcore: mark the pool request done, then close the socket —
        # with an ``await`` in between (anyio's ``SocketStream.aclose``).
        await asyncio.sleep(0)
        self.closed = True

    async def _body(self):
        try:
            while True:
                await asyncio.sleep(3600)
                yield b""  # pragma: no cover
        except asyncio.CancelledError:
            try:
                await self._close_sequence()
            except asyncio.CancelledError:
                self.close_interrupted = True
                raise
            raise

    async def aiter_lines(self):
        async for chunk in self._body():
            yield chunk.decode()

    async def aiter_bytes(self):
        async for chunk in self._body():
            yield chunk

    async def aclose(self):
        # httpx's Response.aclose() is a no-op once httpcore has already
        # run its own close path (the pool stream is marked closed first).
        pass


async def _consume_until_cancelled(pump):
    async for _ in pump:
        pass


async def _starlette_style_disconnect(pump_factory, resp):
    """Consume the pump inside an anyio task group, then cancel the group
    while the consumer is parked on the pump — what Starlette does when
    ``listen_for_disconnect`` sees ``http.disconnect``."""

    async def send():
        return resp

    async with anyio.create_task_group() as tg:
        tg.start_soon(_consume_until_cancelled, pump_factory(send()))
        await asyncio.sleep(0.05)
        tg.cancel_scope.cancel()
    # Give a detached cleanup task (if any) a few loop turns to finish.
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.parametrize(
    "pump_factory",
    [
        lambda coro: _pump_sse_lines(coro, ping_interval=0.01, max_idle=60),
        lambda coro: _pump_bedrock_events(coro, ping_interval=0.01, max_idle=60),
    ],
    ids=["sse", "bedrock"],
)
def test_client_disconnect_releases_downstream(pump_factory):
    resp = _HttpcoreLikeResponse()
    asyncio.run(_starlette_style_disconnect(pump_factory, resp))
    assert not resp.close_interrupted, (
        "reader's close sequence was interrupted by a second cancellation"
    )
    assert resp.closed, "downstream response was never closed"
