"""
What the three backends share when they call a downstream.

  - the timeouts every non-stream and stream call uses
    (``_NON_STREAM_TIMEOUT``, ``_STREAM_TIMEOUT``);
  - the SSE pump (``_pump_sse_lines``): idle ticks, the dead-downstream guard,
    and ``_release_stream``, the cleanup that survives a client disconnect
    (Bedrock's binary event-stream pump reuses ``_next_queued`` and
    ``_release_stream``);
  - ``_relay_native_anthropic_sse``, the relay of a native Anthropic Messages
    stream, used by the vLLM-native and the Bedrock-native paths;
  - ``_error_response``, a downstream's non-200 body passed on as it is.

Nothing here knows which backend it serves, and nothing here bills: the
relay hands its usage to the caller's ``on_finish``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import httpx
from fastapi.responses import JSONResponse

from app.core.logger import logger
from app.services.anthropic_adapter import empty_turn_warning
from app.services.observability import (
    first_output_seen,
    is_output_sse_line,
    mark_first_output,
)


# Kept just below the nginx `proxy_read_timeout` (300s) so the gateway's own
# httpx timeout fires first and returns a clean 502, rather than nginx
# severing the connection mid-flight and the client seeing a raw 504.
_NON_STREAM_TIMEOUT = 280.0
# Streaming requests, all three backends. Every phase is bounded:
#
#   connect / pool  — the pre-flight must fail fast when the pool is starved
#                     or the downstream is unreachable;
#   write           — per socket write while uploading the request body
#                     (agentic clients send multi-MB histories);
#   read            — the wait for RESPONSE HEADERS, and afterwards each gap
#                     between body reads.
#
# `read` used to be None: the wait for headers had no ceiling at all. The SSE
# pump's `_SSE_MAX_IDLE` guard and its client pings only start once headers
# have arrived, so a downstream that accepted the upload and then never
# answered left the coroutine parked forever — no ping, no log, no 502, the
# pool connection held, and SIGTERM unable to finish (uvicorn waits for
# in-flight requests, so the service had to be SIGKILLed). Bounding `read` at
# _NON_STREAM_TIMEOUT caps time-to-headers at the same figure the non-stream
# paths use, still under nginx's 300s so the gateway answers first. After
# headers the pump's idle guard (300s) is the documented ceiling; this read
# timeout is 20s tighter and fires first, which is fine — both surface as an
# error event on the stream after ~5 minutes of downstream silence.
_STREAM_TIMEOUT = httpx.Timeout(
    connect=30.0, read=_NON_STREAM_TIMEOUT, write=30.0, pool=30.0,
)

# Hard ceiling on total downstream silence for a streaming request. The
# per-chunk read timeout is unbounded (reasoning models stall legitimately),
# but a truly dead downstream would otherwise hang the request forever — each
# hung stream permanently holds one shared-pool connection, and enough of
# them accumulated over weeks exhausts the pool (slow requests + health
# probes failing). After this many seconds with no real chunk, the pump gives
# up so the connection is recycled and the caller can surface an error.
# Applies to ALL SSE stream paths (chat completions, responses, Anthropic).
_SSE_MAX_IDLE = 300.0

# How often the pump emits an idle tick while a stream is silent. The
# Anthropic path forwards each tick as an SSE `event: ping` — Claude Code
# treats long gaps without any event as a dead connection, and 10s keeps
# ~33% headroom under the smallest client idle timeout observed (~15s). The
# OpenAI-shape paths (chat/responses) consume the tick only for idle
# accounting and emit nothing on the wire.
_SSE_PING_INTERVAL = 10.0
# Real Anthropic's ping carries `{"type": "ping"}` as the data payload.
# Claude Code parses every SSE event's data as JSON and dispatches on the
# `type` key — an empty `{}` payload looks malformed to that parser and may
# cause the client to drop or stall the stream, defeating the ping's purpose.
_ANTHROPIC_PING_EVENT = 'event: ping\ndata: {"type": "ping"}\n\n'


async def _next_queued(queue: asyncio.Queue, timeout: float):
    """``queue.get()`` bounded by ``timeout`` (raises ``asyncio.TimeoutError``).

    An item already queued is taken directly: ``asyncio.wait_for`` wraps every
    call in a new Task (~15 µs on Python 3.11), which the pumps would otherwise
    pay for every SSE line of every stream. Only an empty queue waits, so the
    idle-tick timing is unchanged."""
    try:
        return queue.get_nowait()
    except asyncio.QueueEmpty:
        return await asyncio.wait_for(queue.get(), timeout=timeout)


async def _pump_sse_lines(
    send_coro,
    ping_interval: float = _SSE_PING_INTERVAL,
    max_idle: float = _SSE_MAX_IDLE,
):
    """Run a streaming HTTP request as a background task and yield events.

    Yields tuples:
      ('ping', None)        — `ping_interval` seconds passed without new data
      ('line', str)         — SSE data line from downstream
      ('err', Exception)    — downstream raised, or `max_idle` seconds passed
                              with no real data (treated as a dead downstream)
      ('done', None)        — stream finished cleanly

    Caller does NOT need to close the response — this helper handles aclose.
    """
    queue: asyncio.Queue = asyncio.Queue()
    resp_holder: list = []

    async def reader():
        try:
            resp = await send_coro
            resp_holder.append(resp)
            watch = not first_output_seen()  # TTFT, see request_timing
            async for line in resp.aiter_lines():
                if watch and is_output_sse_line(line):
                    mark_first_output()
                    watch = False
                await queue.put(("line", line))
        except BaseException as e:
            await queue.put(("err", e))
        finally:
            await queue.put(("done", None))

    task = asyncio.create_task(reader())
    idle = 0.0
    try:
        while True:
            try:
                kind, data = await _next_queued(queue, ping_interval)
            except asyncio.TimeoutError:
                idle += ping_interval
                if idle >= max_idle:
                    yield ("err", TimeoutError(
                        f"downstream produced no data for {max_idle:.0f}s"
                    ))
                    return
                yield ("ping", None)
                continue
            idle = 0.0  # real data arrived — reset the idle clock
            yield (kind, data)
            if kind in ("done", "err"):
                return
    finally:
        await _release_stream(task, resp_holder)


# Strong references to in-flight cleanup tasks: asyncio only holds tasks
# weakly, and a cleanup that outlives its pump (see _release_stream) must not
# be garbage-collected mid-close.
_pending_releases: set[asyncio.Task] = set()


async def _release_stream(task: asyncio.Task, resp_holder: list) -> None:
    """Stop the pump's reader task and close the downstream response.

    Runs the actual cleanup in a DETACHED task and only *waits* for it here,
    so the caller's own cancellation can never reach the close sequence.

    That matters because the pump is consumed from a Starlette
    ``StreamingResponse``, which runs inside an anyio task group that is
    cancelled the moment the client disconnects — and anyio keeps
    re-delivering that cancellation at every ``await`` until the task leaves
    the scope. With the cleanup inline, ``await task`` forwarded the *second*
    cancellation into the reader (asyncio cancels the future a cancelled task
    is awaiting), and it landed inside httpcore's shielded connection-close:
    httpcore had already marked the pool request done, so the HTTP/1.1
    socket was never closed and the pool slot stayed "in use" forever. Every
    stream a client abandoned mid-flight leaked one downstream connection;
    on a 200-slot cloud client that was ~a day of Claude Code usage before
    every new request failed with ``PoolTimeout``.

    Reproduced by ``tests/test_stream_disconnect_cleanup.py``.
    """

    async def _cleanup() -> None:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for resp in resp_holder:
            try:
                await resp.aclose()
            except Exception:
                pass

    cleanup = asyncio.create_task(_cleanup())
    _pending_releases.add(cleanup)
    cleanup.add_done_callback(_pending_releases.discard)
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        # The consumer is being cancelled (client gone). The cleanup task
        # carries on by itself; re-raise so the generator still unwinds.
        raise


def _error_response(resp) -> JSONResponse:
    """Build a JSONResponse from a non-200 downstream response, handling non-JSON bodies."""
    try:
        content = resp.json()
    except Exception:
        content = {"error": resp.text[:500]}
    return JSONResponse(content=content, status_code=resp.status_code)


# ---------------------------------------------------------------------------
# Native Anthropic Messages stream
# ---------------------------------------------------------------------------

class _NativeAnthropicCapture:
    """Rebuild the Anthropic assistant turn from native /v1/messages SSE
    events (Phase 2 I/O capture only). Best-effort: any malformed event is
    skipped rather than breaking the stream.

    Deltas are collected as lists of parts and joined once in ``as_message``:
    re-concatenating a string held in a dict copies it on every delta, which
    is quadratic over a long thinking block or a large tool input."""

    # delta type -> (field it streams into, key carrying the fragment)
    _STREAMED = {
        "text_delta": ("text", "text"),
        "thinking_delta": ("thinking", "thinking"),
        "signature_delta": ("signature", "signature"),
        "input_json_delta": ("input", "partial_json"),
    }

    def __init__(self):
        self._blocks: dict[int, dict[str, Any]] = {}
        # (block index, field) -> fragments
        self._parts: dict[tuple[int, str], list[str]] = {}

    def handle(self, chunk: dict[str, Any]) -> None:
        try:
            ctype = chunk.get("type")
            if ctype == "content_block_start":
                idx = int(chunk.get("index", 0))
                self._blocks[idx] = dict(chunk.get("content_block") or {})
            elif ctype == "content_block_delta":
                idx = int(chunk.get("index", 0))
                delta = chunk.get("delta") or {}
                self._blocks.setdefault(idx, {"type": "text", "text": ""})
                streamed = self._STREAMED.get(delta.get("type"))
                if streamed is not None:
                    field, key = streamed
                    self._parts.setdefault((idx, field), []).append(delta.get(key, ""))
        except Exception:
            pass

    def as_message(self) -> dict[str, Any]:
        blocks = []
        for idx in sorted(self._blocks):
            block = dict(self._blocks[idx])
            for field in ("text", "thinking", "signature"):
                parts = self._parts.get((idx, field))
                if parts:
                    block[field] = block.get(field, "") + "".join(parts)
            json_parts = self._parts.get((idx, "input"))
            if json_parts is not None:
                try:
                    block["input"] = json.loads("".join(json_parts))
                except Exception:
                    block["input"] = {}
            blocks.append(block)
        return {"role": "assistant", "content": blocks}


def _merge_native_usage(merged: dict[str, int], usage: Any) -> None:
    """Fold one Anthropic ``usage`` object into ``merged``. A later non-zero
    counter wins: ``message_delta`` usage is cumulative, and some servers
    repeat a counter there as 0."""
    if not isinstance(usage, dict):
        return
    for key, value in usage.items():
        if isinstance(value, int) and not isinstance(value, bool) and value:
            merged[key] = value


async def _relay_native_anthropic_sse(
    send_coro,
    model_alias: str,
    req_shape: str,
    capture_io: bool,
    on_finish: Callable[[dict[str, int], Any], None],
):
    """Relay a native Anthropic SSE stream to the client, line by line.

    Shared by the vLLM-native and Bedrock-native Messages paths. Events pass
    through unchanged except ``message_start``'s ``model`` (real model →
    alias). Pings go out only at event boundaries. A stream that ends
    without ``message_stop`` and without an error event of its own gets a
    trailing ``overloaded_error``, so Claude Code retries instead of taking
    the turn as complete.

    ``on_finish(usage, output)`` runs once at the end, with the usage
    counters merged from ``message_start`` and ``message_delta`` (see
    ``_merge_native_usage``) and the rebuilt assistant message when I/O
    capture is on. It does the billing.
    """
    usage: dict[str, int] = {}
    stop_reason = None
    saw_message_stop = False
    saw_error_event = False
    failed = False
    text_chars = 0
    thinking_chars = 0
    capture = _NativeAnthropicCapture() if capture_io else None
    # True whenever the previous yielded line completed an SSE event
    # (blank separator) — the only place a ping can be injected without
    # splitting an `event:`/`data:` pair.
    at_boundary = True

    async for kind, data in _pump_sse_lines(send_coro):
        if kind == "ping":
            if at_boundary:
                yield _ANTHROPIC_PING_EVENT
            continue
        if kind == "err":
            logger.error("Stream error: {}: {}", type(data).__name__, data)
            failed = True
            break
        if kind == "done":
            break

        line = data
        if not line:
            at_boundary = True
            yield "\n"
            continue

        if line.startswith("data: "):
            try:
                chunk = json.loads(line[6:])
            except json.JSONDecodeError:
                chunk = None
            if isinstance(chunk, dict):
                ctype = chunk.get("type")
                if ctype == "message_start":
                    msg = chunk.get("message") or {}
                    _merge_native_usage(usage, msg.get("usage"))
                    if msg.get("model"):
                        # Hide the downstream real model behind the alias.
                        msg["model"] = model_alias
                        line = "data: " + json.dumps(chunk, ensure_ascii=False)
                elif ctype == "message_delta":
                    _merge_native_usage(usage, chunk.get("usage"))
                    stop_reason = (chunk.get("delta") or {}).get("stop_reason", stop_reason)
                elif ctype == "message_stop":
                    saw_message_stop = True
                elif ctype == "error":
                    saw_error_event = True
                elif ctype == "content_block_delta":
                    delta = chunk.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text_chars += len(delta.get("text", ""))
                    elif delta.get("type") == "thinking_delta":
                        thinking_chars += len(delta.get("thinking", ""))
                if capture is not None:
                    capture.handle(chunk)

        at_boundary = False
        yield f"{line}\n"

    if failed or not (saw_message_stop or saw_error_event):
        # Same contract as AnthropicStreamTranslator.fail(): a stream that
        # dies without message_stop must NOT look complete — emit an
        # overloaded_error so Claude Code retries with backoff.
        if not failed:
            logger.warning(
                "Native Anthropic stream ended without message_stop | model={} — truncated",
                model_alias,
            )
        if not at_boundary:
            yield "\n"
        err_payload = json.dumps({
            "type": "error",
            "error": {
                "type": "overloaded_error",
                "message": "Downstream stream ended prematurely; the response may be incomplete.",
            },
        })
        yield f"event: error\ndata: {err_payload}\n\n"

    diag = empty_turn_warning(
        model_alias, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
        stop_reason, text_chars, thinking_chars, req_shape,
    )
    if diag:
        logger.warning(diag)
    on_finish(usage, capture.as_message() if capture is not None else None)
