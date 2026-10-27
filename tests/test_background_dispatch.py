"""`_submit_background` — the fire-and-forget seam behind usage logging and
the Langfuse generation.

The rest of the suite patches this to run inline (see `_inline_background`
in conftest) so assertions on `usage_logs` don't race the executor thread.
That patch would hide a broken dispatch, so the real thing is exercised here.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading

from app.services.vllm_proxy import _submit_background


class TestSubmitBackground:
    def test_runs_inline_without_a_running_loop(self):
        """Scripts and sync contexts have no loop — the write must still happen."""
        calls = []
        _submit_background(lambda *a: calls.append(a), 1, "two")
        assert calls == [(1, "two")]

    def test_offloads_to_a_worker_thread_and_returns_before_it_finishes(self):
        """Under a running loop the write goes to a thread and is not awaited.

        Billing must not add latency to the response: dispatch has to return
        while the write is still in flight, and the write must not occupy the
        event loop thread while it blocks on the DB.
        """
        gate = threading.Event()
        finished = threading.Event()
        seen = {}

        def write(*args):
            seen["args"] = args
            seen["thread"] = threading.current_thread().name
            gate.wait(5.0)
            finished.set()

        async def main():
            _submit_background(write, "a", "b")
            # The write is parked on the gate; if dispatch had awaited it,
            # this line would not run until after `finished` was set.
            returned_early = not finished.is_set()
            gate.set()
            return returned_early, threading.current_thread().name

        returned_early, loop_thread = asyncio.run(main())

        assert returned_early, "dispatch blocked until the write completed"
        assert finished.wait(5.0), "the write was never dispatched"
        assert seen["args"] == ("a", "b")
        assert seen["thread"] != loop_thread, "the write ran on the event loop thread"

    def test_runs_in_a_copy_of_the_callers_context(self):
        """Request-scoped contextvars stay readable from the worker thread."""
        var: contextvars.ContextVar[str] = contextvars.ContextVar("req", default="unset")
        done = threading.Event()
        seen = {}

        def job():
            seen["value"] = var.get()
            done.set()

        async def main():
            var.set("this-request")
            _submit_background(job)

        asyncio.run(main())
        assert done.wait(5.0)
        assert seen["value"] == "this-request"
