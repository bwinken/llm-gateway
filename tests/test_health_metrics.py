"""Tests for vLLM /metrics scraping helpers in health.py."""

from __future__ import annotations

from app.services.health import _metrics_url, _parse_vllm_metrics


class TestMetricsUrl:
    def test_strips_v1_suffix(self):
        assert _metrics_url("http://host:8000/v1") == "http://host:8000/metrics"

    def test_trailing_slash(self):
        assert _metrics_url("http://host:8000/v1/") == "http://host:8000/metrics"

    def test_no_v1_suffix(self):
        assert _metrics_url("http://host:8000") == "http://host:8000/metrics"


class TestParseVllmMetrics:
    def test_basic(self):
        text = (
            "# HELP vllm:num_requests_running ...\n"
            "# TYPE vllm:num_requests_running gauge\n"
            'vllm:num_requests_running{model_name="m"} 8.0\n'
            'vllm:num_requests_waiting{model_name="m"} 15.0\n'
        )
        out = _parse_vllm_metrics(text)
        assert out == {"running": 8, "waiting": 15}

    def test_sums_across_labels(self):
        text = (
            'vllm:num_requests_running{model_name="a"} 3.0\n'
            'vllm:num_requests_running{model_name="b"} 2.0\n'
            'vllm:num_requests_waiting{model_name="a"} 1.0\n'
        )
        out = _parse_vllm_metrics(text)
        assert out == {"running": 5, "waiting": 1}

    def test_missing_waiting_defaults_zero(self):
        text = 'vllm:num_requests_running{model_name="m"} 4.0\n'
        out = _parse_vllm_metrics(text)
        assert out == {"running": 4, "waiting": 0}

    def test_non_vllm_text_returns_none(self):
        # A non-vLLM server's /metrics (or an HTML 404 page) → None
        text = "# some other prometheus exporter\nprocess_cpu_seconds_total 12.3\n"
        assert _parse_vllm_metrics(text) is None

    def test_empty_returns_none(self):
        assert _parse_vllm_metrics("") is None

    def test_malformed_line_skipped(self):
        text = (
            "vllm:num_requests_running garbage-no-number\n"
            'vllm:num_requests_waiting{model_name="m"} 7.0\n'
        )
        out = _parse_vllm_metrics(text)
        # running line unparseable → 0; waiting parsed
        assert out == {"running": 0, "waiting": 7}
