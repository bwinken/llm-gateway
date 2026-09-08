"""``Slow request`` WARNING — a file-visible trace for requests that succeed
but take too long.

The file sink only records WARNING and above by default, and ``usage_logs``
stores no duration, so a request that took four minutes to finish used to
leave no trace anywhere an operator could grep. ``_log_usage`` now emits one
WARNING line when the request's wall-clock time (measured from the ASGI
middleware's entry marker) reaches ``SLOW_REQUEST_WARN_S`` (default 120s;
``0`` disables).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.core.logger import logger
from app.models.schema import User
from app.services import vllm_proxy
from tests.conftest import auth_header, make_httpx_response


@pytest.fixture()
def warnings():
    """Collect WARNING-and-above messages emitted during the test."""
    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(m.record["message"]), level="WARNING")
    yield lines
    logger.remove(sink_id)


def _user() -> User:
    return User(id=1, username="slowuser", password_hash="", api_key="sk-slow", daily_limit_usd=10.0)


def _log(latency_ms):
    with patch("app.services.vllm_proxy.request_latency_ms", return_value=latency_ms):
        vllm_proxy._log_usage(_user(), "test-llm", "llm", 1200, 34, "/v1/chat/completions", backend="azure")


def _slow_lines(lines):
    return [line for line in lines if line.startswith("Slow request")]


class TestThreshold:
    def test_default_threshold_is_120s(self, monkeypatch):
        monkeypatch.delenv("SLOW_REQUEST_WARN_S", raising=False)
        assert vllm_proxy._slow_request_threshold_s() == 120.0

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("SLOW_REQUEST_WARN_S", "45")
        assert vllm_proxy._slow_request_threshold_s() == 45.0

    def test_garbage_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("SLOW_REQUEST_WARN_S", "soon")
        assert vllm_proxy._slow_request_threshold_s() == 120.0


class TestWarningLine:
    def test_over_threshold_logs_one_line_with_the_diagnostic_fields(self, warnings, monkeypatch):
        monkeypatch.delenv("SLOW_REQUEST_WARN_S", raising=False)
        _log(latency_ms=130_500.0)

        slow = _slow_lines(warnings)
        assert len(slow) == 1
        line = slow[0]
        for field in (
            "user=slowuser", "model=test-llm", "type=llm",
            "endpoint=/v1/chat/completions", "backend=azure",
            "duration=130.5s", "input_tokens=1200", "output_tokens=34",
            "threshold=120s",
        ):
            assert field in line, line

    def test_under_threshold_is_silent(self, warnings, monkeypatch):
        monkeypatch.delenv("SLOW_REQUEST_WARN_S", raising=False)
        _log(latency_ms=119_999.0)
        assert _slow_lines(warnings) == []

    def test_exactly_at_threshold_logs(self, warnings, monkeypatch):
        monkeypatch.setenv("SLOW_REQUEST_WARN_S", "30")
        _log(latency_ms=30_000.0)
        assert len(_slow_lines(warnings)) == 1

    def test_zero_disables(self, warnings, monkeypatch):
        monkeypatch.setenv("SLOW_REQUEST_WARN_S", "0")
        _log(latency_ms=999_000.0)
        assert _slow_lines(warnings) == []

    def test_missing_start_marker_is_silent(self, warnings, monkeypatch):
        """A code path that bypassed the middleware has no start time; never
        guess a duration."""
        monkeypatch.delenv("SLOW_REQUEST_WARN_S", raising=False)
        _log(latency_ms=None)
        assert _slow_lines(warnings) == []

    def test_hook_failure_never_breaks_billing(self, warnings, monkeypatch):
        monkeypatch.delenv("SLOW_REQUEST_WARN_S", raising=False)
        with patch("app.services.vllm_proxy._warn_if_slow", side_effect=RuntimeError("boom")):
            _log(latency_ms=130_000.0)  # must not raise
        assert any("slow-request hook failed" in line for line in warnings)


class TestEndToEnd:
    def test_slow_chat_completion_is_logged_through_the_real_seam(self, client, test_user, warnings, monkeypatch):
        """Through the app: the middleware stamps the start time, the proxy
        reaches _log_usage, and the line carries the resolved alias."""
        monkeypatch.setenv("SLOW_REQUEST_WARN_S", "60")
        downstream = {
            "id": "c1",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8},
        }

        async def _post(*args, **kwargs):
            return make_httpx_response(200, downstream)

        client.__httpx_mock__.post = _post
        with patch("app.services.vllm_proxy.request_latency_ms", return_value=61_000.0):
            resp = client.post(
                "/v1/chat/completions",
                json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header(),
            )

        assert resp.status_code == 200
        slow = _slow_lines(warnings)
        assert len(slow) == 1
        assert "user=testuser" in slow[0]
        assert "model=test-llm" in slow[0]
        assert "backend=vllm" in slow[0]
        assert "duration=61.0s" in slow[0]

    def test_fast_chat_completion_is_not_logged(self, client, test_user, warnings, monkeypatch):
        monkeypatch.delenv("SLOW_REQUEST_WARN_S", raising=False)
        downstream = {
            "id": "c1",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8},
        }

        async def _post(*args, **kwargs):
            return make_httpx_response(200, downstream)

        client.__httpx_mock__.post = _post
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(),
        )
        assert resp.status_code == 200
        assert _slow_lines(warnings) == []
