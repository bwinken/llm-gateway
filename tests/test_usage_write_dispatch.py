"""`_submit_usage_write` — the fire-and-forget seam behind usage logging.

The rest of the suite patches this to run inline (see `_inline_usage_write`
in conftest) so assertions on `usage_logs` don't race the executor thread.
That patch would hide a broken dispatch, so the real thing is exercised here.
"""

from __future__ import annotations

import asyncio
import threading

from app.services.vllm_proxy import _submit_usage_write


class TestSubmitUsageWrite:
    def test_runs_inline_without_a_running_loop(self):
        """Scripts and sync contexts have no loop — the write must still happen."""
        calls = []
        _submit_usage_write(lambda *a: calls.append(a), 1, "two")
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
            _submit_usage_write(write, "a", "b")
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
