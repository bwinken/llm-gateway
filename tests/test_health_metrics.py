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

    def test_waiting_by_reason_not_double_counted(self):
        """vLLM >= 0.20 exports vllm:num_requests_waiting_by_reason alongside
        the total (vllm-project/vllm#38435); its reasons sum to the total.
        Prefix matching added both, so the dashboard showed 2x the queue."""
        text = (
            "# HELP vllm:num_requests_waiting Number of requests waiting to be processed.\n"
            "# TYPE vllm:num_requests_waiting gauge\n"
            'vllm:num_requests_waiting{engine="0",model_name="m"} 6.0\n'
            "# HELP vllm:num_requests_waiting_by_reason Number of waiting requests by reason.\n"
            "# TYPE vllm:num_requests_waiting_by_reason gauge\n"
            'vllm:num_requests_waiting_by_reason{engine="0",model_name="m",reason="capacity"} 4.0\n'
            'vllm:num_requests_waiting_by_reason{engine="0",model_name="m",reason="deferred"} 2.0\n'
            'vllm:num_requests_running{engine="0",model_name="m"} 3.0\n'
        )
        assert _parse_vllm_metrics(text) == {"running": 3, "waiting": 6}

    def test_sums_data_parallel_engines(self):
        """One server, several DP engines: each engine is its own label set
        and the server-wide queue is their sum."""
        text = (
            'vllm:num_requests_waiting{engine="0",model_name="m"} 2.0\n'
            'vllm:num_requests_waiting{engine="1",model_name="m"} 5.0\n'
            'vllm:num_requests_waiting_by_reason{engine="0",model_name="m",reason="capacity"} 2.0\n'
            'vllm:num_requests_waiting_by_reason{engine="1",model_name="m",reason="capacity"} 5.0\n'
        )
        assert _parse_vllm_metrics(text) == {"running": 0, "waiting": 7}

    def test_unlabeled_sample(self):
        text = "vllm:num_requests_running 2\nvllm:num_requests_waiting 1\n"
        assert _parse_vllm_metrics(text) == {"running": 2, "waiting": 1}
