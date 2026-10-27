"""Prometheus metrics (app/services/metrics.py): off unless METRICS_ENABLED,
multiprocess files per server run, bounded labels, and the hooks at the
billing / error / rejection / health seams."""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from pathlib import Path

from unittest.mock import patch

import pytest
from prometheus_client import values
from prometheus_client.parser import text_string_to_metric_families

from app.models.schema import UsageLog
from tests.conftest import auth_header, make_httpx_response
from tests.test_stream_idle_guard import _DeadClient, _run_check


@pytest.fixture()
def metrics_on(tmp_path, monkeypatch):
    from app.services import metrics

    monkeypatch.setenv("METRICS_ENABLED", "true")
    monkeypatch.setenv("METRICS_DIR", str(tmp_path))
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", "")  # restored after the test
    monkeypatch.setattr(metrics, "_metrics", None)
    monkeypatch.setattr(metrics, "_run_dir", None)
    monkeypatch.setattr(metrics, "_failed", False)
    monkeypatch.setattr(values, "ValueClass", values.ValueClass)
    # Model labels are checked against config's maps; conftest patches the
    # proxies' references, so point config's at the same test routing.
    from app.core import config
    from tests.conftest import TEST_MODEL_ROUTING

    monkeypatch.setattr(config, "MODEL_ROUTING", TEST_MODEL_ROUTING)
    return metrics


def _scrape(metrics) -> dict[tuple[str, tuple], float]:
    body, _ = metrics.render()
    out = {}
    for family in text_string_to_metric_families(body.decode()):
        for s in family.samples:
            out[(s.name, tuple(sorted(s.labels.items())))] = s.value
    return out


def _get(scraped, name: str, **labels) -> float | None:
    return scraped.get((name, tuple(sorted(labels.items()))))


def _chat(client) -> int:
    async def _post(*args, **kwargs):
        return make_httpx_response(200, {
            "id": "c1",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
        })

    client.__httpx_mock__.post = _post
    return client.post(
        "/v1/chat/completions",
        json={"model": "test-llm", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(),
    ).status_code


class TestDisabledByDefault:
    def test_endpoint_404_and_hooks_are_noops(self, client):
        from app.services import metrics

        assert client.get("/metrics").status_code == 404
        metrics.observe_request("/v1/chat/completions", "vllm", "test-llm", 1, 1, 0, 0.1, 5.0, {})
        metrics.observe_rejection("daily_limit")
        assert metrics._metrics is None


class TestEnabled:
    def test_completed_request_is_counted(self, client, test_user, metrics_on):
        assert _chat(client) == 200
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")

        m = _scrape(metrics_on)
        labels = dict(endpoint="/v1/chat/completions", backend="vllm", model="test-llm", status="200")
        assert _get(m, "llm_gateway_requests_total", **labels) == 1
        assert _get(m, "llm_gateway_tokens_total", backend="vllm", model="test-llm", kind="input") == 12
        assert _get(m, "llm_gateway_tokens_total", backend="vllm", model="test-llm", kind="output") == 3

    def test_unknown_model_is_reported_as_other(self, metrics_on):
        metrics_on.observe_request("/v1/messages", "vllm", "claude-made-up", 1, 1, 0, 0.0, None, None)
        metrics_on.observe_error("/v1/messages", "vllm", "another-made-up", 502)
        m = _scrape(metrics_on)
        assert _get(m, "llm_gateway_requests_total",
                    endpoint="/v1/messages", backend="vllm", model="other", status="200") == 1
        assert _get(m, "llm_gateway_requests_total",
                    endpoint="/v1/messages", backend="vllm", model="other", status="502") == 1

    def test_timing_phases_are_observed(self, metrics_on):
        metrics_on.observe_request(
            "/v1/messages", "vllm", "test-llm", 10, 50, 4, 0.02, 3000.0,
            {"ttft_ms": 800.0, "upstream_ttfb_ms": 120.0, "gateway_overhead_ms": 9.0, "output_tps": 42.0},
        )
        m = _scrape(metrics_on)
        assert _get(m, "llm_gateway_request_duration_seconds_sum",
                    endpoint="/v1/messages", backend="vllm", model="test-llm") == pytest.approx(3.0)
        assert _get(m, "llm_gateway_ttft_seconds_sum", backend="vllm", model="test-llm") == pytest.approx(0.8)
        assert _get(m, "llm_gateway_upstream_ttfb_seconds_sum", backend="vllm", model="test-llm") == pytest.approx(0.12)
        assert _get(m, "llm_gateway_gateway_overhead_seconds_sum", backend="vllm") == pytest.approx(0.009)
        assert _get(m, "llm_gateway_output_tokens_per_second_sum", backend="vllm", model="test-llm") == 42
        assert _get(m, "llm_gateway_tokens_total", backend="vllm", model="test-llm", kind="cache_read") == 4
        assert _get(m, "llm_gateway_cost_usd_total", backend="vllm", model="test-llm") == pytest.approx(0.02)

    def test_daily_limit_rejection_is_counted(self, client, db_session, test_user, metrics_on):
        db_session.add(UsageLog(user_id=test_user.id, cost_usd=Decimal("100")))
        db_session.commit()
        assert _chat(client) == 429
        assert _get(_scrape(metrics_on), "llm_gateway_rejections_total", reason="daily_limit") == 1

    def test_health_loop_sets_server_gauges(self, metrics_on):
        url = "http://metrics-health-test:8000/v1"
        _run_check(_DeadClient(), {"m": {"base_url": url, "api_key": ""}})
        assert _get(_scrape(metrics_on), "llm_gateway_server_up", server=url) == 0

    def test_in_flight_counts_open_requests_only(self, metrics_on):
        seen = {}

        async def app(scope, receive, send):
            seen["during"] = _get(_scrape(metrics_on), "llm_gateway_in_flight_requests")

        mw = metrics_on.InFlightMiddleware(app)
        asyncio.run(mw({"type": "http", "path": "/v1/messages"}, None, None))
        assert seen["during"] == 1
        assert _get(_scrape(metrics_on), "llm_gateway_in_flight_requests") == 0


class TestRunDirectory:
    def test_one_directory_per_server_run_and_stale_runs_removed(self, tmp_path, metrics_on):
        dead = tmp_path / "4194999"  # above Linux pid_max: never a live process
        dead.mkdir()
        (dead / "counter_1.db").write_bytes(b"")
        metrics_on.startup()
        run_dir = Path(os.environ["PROMETHEUS_MULTIPROC_DIR"])
        assert run_dir == tmp_path / str(os.getppid())
        assert not dead.exists()

    def test_bad_directory_never_breaks_a_request(self, client, test_user, metrics_on, tmp_path, monkeypatch):
        blocker = tmp_path / "file"
        blocker.write_text("not a directory")
        monkeypatch.setenv("METRICS_DIR", str(blocker))
        with patch.object(metrics_on, "_create", wraps=metrics_on._create) as create:
            assert _chat(client) == 200
            assert _chat(client) == 200
        assert create.call_count == 1   # failed once, then stays off quietly
        assert metrics_on.render() is None
