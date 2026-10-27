"""
Prometheus metrics — opt-in: set ``METRICS_ENABLED=true`` in ``.env``.

Off (the default) every function here is a no-op that never imports
``prometheus_client`` and ``GET /metrics`` answers 404.

On, ``GET /metrics`` serves the gateway's own counters and histograms:

  llm_gateway_requests_total{endpoint,backend,model,status}
  llm_gateway_tokens_total{backend,model,kind}          kind: input|output|cache_read
  llm_gateway_cost_usd_total{backend,model}
  llm_gateway_request_duration_seconds{endpoint,backend,model}
  llm_gateway_ttft_seconds{backend,model}               streams only
  llm_gateway_upstream_ttfb_seconds{backend,model}
  llm_gateway_gateway_overhead_seconds{backend}
  llm_gateway_output_tokens_per_second{backend,model}
  llm_gateway_rejections_total{reason}                  daily_limit|cloud_limit|concurrency_limit
  llm_gateway_in_flight_requests                        open HTTP requests, streams included
  llm_gateway_server_up{server}                         health loop: 1 UP, 0 DOWN
  llm_gateway_vllm_requests_running{server} / _waiting{server}

Labels never carry a user identity or any other unbounded value: ``model`` is
a configured alias (anything else is reported as ``other``) and ``endpoint``
is one of the gateway's own route names.

Multiple workers: ``--workers 2`` means two processes, and a scrape lands on
just one of them. ``prometheus_client``'s multiprocess mode makes every
worker write its values to mmap files in a shared directory, and the
endpoint sums them. Each server run gets its own directory,
``<METRICS_DIR>/<supervisor pid>`` (``METRICS_DIR`` defaults to
``<tmp>/llm-gateway-metrics``): every worker of one run shares a parent, so
a restart starts from zero instead of mixing in the previous run's files,
and directories of runs whose supervisor is gone are deleted. A worker that
died without a clean shutdown has its live gauges dropped by the next worker
that starts; its counters are kept, so totals never go backwards.

Recording never raises into a request: every entry point swallows and logs.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from app.core import config
from app.core.logger import logger

_PREFIX = "llm_gateway"

_LATENCY_BUCKETS = (0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 300, 600)
_FIRST_BYTE_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120)
_OVERHEAD_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5)
_TPS_BUCKETS = (5, 10, 20, 30, 40, 60, 80, 100, 150, 200, 300)

_metrics: dict[str, Any] | None = None
_run_dir: Path | None = None
_failed = False  # init failed once (e.g. METRICS_DIR unusable): stay off, log once


def enabled() -> bool:
    return os.getenv("METRICS_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _base_dir() -> Path:
    return Path(os.getenv("METRICS_DIR", "").strip() or Path(tempfile.gettempdir()) / "llm-gateway-metrics")


def _pid_alive(pid: int) -> bool:
    if os.name != "posix":  # os.kill(pid, 0) terminates the process on Windows
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _clean_stale(base: Path, run_dir: Path) -> None:
    """Delete directories of server runs whose supervisor is gone, and drop
    the live gauges of workers of this run that died uncleanly."""
    from prometheus_client import multiprocess

    for child in base.iterdir():
        if child != run_dir and child.is_dir() and child.name.isdigit() and not _pid_alive(int(child.name)):
            shutil.rmtree(child, ignore_errors=True)
    pids = {int(m.group(1)) for f in run_dir.glob("*.db") if (m := re.search(r"_(\d+)\.db$", f.name))}
    for pid in pids:
        if pid != os.getpid() and not _pid_alive(pid):
            multiprocess.mark_process_dead(pid, str(run_dir))


def _init() -> dict[str, Any] | None:
    """Create the metric objects on first use (per process). None when off,
    or when setting up failed: that is logged once, and metrics stay off for
    this worker rather than retrying (and warning) on every request."""
    global _failed
    if _metrics is not None or _failed or not enabled():
        return _metrics
    try:
        return _create()
    except Exception as exc:
        _failed = True
        logger.error("Prometheus metrics disabled for this worker: setup failed | error={}: {}",
                     type(exc).__name__, exc)
        return None


def _create() -> dict[str, Any]:
    global _metrics, _run_dir
    base = _base_dir()
    run_dir = base / str(os.getppid())
    run_dir.mkdir(parents=True, exist_ok=True)
    # Must be set before prometheus_client creates any value: multiprocess
    # values open their mmap files under this directory.
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = str(run_dir)
    from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, values

    # The value class is chosen when prometheus_client is first imported, and
    # caches its open files: install a fresh multiprocess one bound to this
    # directory, whatever imported the library first.
    values.ValueClass = values.MultiProcessValue()
    try:
        _clean_stale(base, run_dir)
    except Exception as exc:
        logger.warning("Metrics: stale-file cleanup failed: {}: {}", type(exc).__name__, exc)

    # A private registry: the metric objects only write their files; the
    # endpoint collects from the files, so nothing is exposed twice.
    reg = CollectorRegistry()
    model_labels = ("backend", "model")
    _metrics = {
        "requests": Counter(f"{_PREFIX}_requests_total", "Completed requests.",
                            ("endpoint", "backend", "model", "status"), registry=reg),
        "tokens": Counter(f"{_PREFIX}_tokens_total", "Tokens billed.",
                          (*model_labels, "kind"), registry=reg),
        "cost": Counter(f"{_PREFIX}_cost_usd_total", "Spend in USD.", model_labels, registry=reg),
        "duration": Histogram(f"{_PREFIX}_request_duration_seconds", "Request duration, entry to completion.",
                              ("endpoint", "backend", "model"), buckets=_LATENCY_BUCKETS, registry=reg),
        "ttft": Histogram(f"{_PREFIX}_ttft_seconds", "Request entry to first output (streams).",
                          model_labels, buckets=_FIRST_BYTE_BUCKETS, registry=reg),
        "ttfb": Histogram(f"{_PREFIX}_upstream_ttfb_seconds", "Downstream send to response headers.",
                          model_labels, buckets=_FIRST_BYTE_BUCKETS, registry=reg),
        "overhead": Histogram(f"{_PREFIX}_gateway_overhead_seconds",
                              "Request entry to first downstream send (auth, DB, translation).",
                              ("backend",), buckets=_OVERHEAD_BUCKETS, registry=reg),
        "tps": Histogram(f"{_PREFIX}_output_tokens_per_second", "Output tokens / generation time.",
                         model_labels, buckets=_TPS_BUCKETS, registry=reg),
        "rejections": Counter(f"{_PREFIX}_rejections_total", "Requests refused with 429 before reaching a downstream.",
                              ("reason",), registry=reg),
        "in_flight": Gauge(f"{_PREFIX}_in_flight_requests", "Open HTTP requests, streams included.",
                           multiprocess_mode="livesum", registry=reg),
        "server_up": Gauge(f"{_PREFIX}_server_up", "On-prem server health (1 UP, 0 DOWN).",
                           ("server",), multiprocess_mode="livemostrecent", registry=reg),
        "vllm_running": Gauge(f"{_PREFIX}_vllm_requests_running", "vLLM requests running (scraped).",
                              ("server",), multiprocess_mode="livemostrecent", registry=reg),
        "vllm_waiting": Gauge(f"{_PREFIX}_vllm_requests_waiting", "vLLM requests waiting (scraped).",
                              ("server",), multiprocess_mode="livemostrecent", registry=reg),
    }
    _run_dir = run_dir
    logger.info("Prometheus metrics enabled | dir={}", run_dir)
    return _metrics


def startup() -> None:
    """Lifespan hook: create this worker's metrics (and clean stale files)."""
    _init()


def shutdown() -> None:
    """Lifespan hook: drop this worker's live gauges from the shared view."""
    if _metrics is None or _run_dir is None:
        return
    try:
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(os.getpid(), str(_run_dir))
    except Exception as exc:
        logger.warning("Metrics shutdown failed: {}: {}", type(exc).__name__, exc)


def render() -> tuple[bytes, str] | None:
    """The exposition for ``GET /metrics``, summed over every worker of this
    run; None when metrics are off. Blocking (reads the files)."""
    if _init() is None:
        return None
    from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest, multiprocess

    reg = CollectorRegistry()
    multiprocess.MultiProcessCollector(reg, path=str(_run_dir))
    return generate_latest(reg), CONTENT_TYPE_LATEST


def _model_label(model: str) -> str:
    """A configured alias, else ``other`` — a client-sent name must never
    become a label value."""
    if model in config.MODEL_ROUTING or model in config.AZURE_MODELS or model in config.BEDROCK_MODELS:
        return model
    return "other"


def observe_request(
    endpoint: str, backend: str, model: str,
    input_tokens: int, output_tokens: int, cached_tokens: int, cost: float,
    latency_ms: float | None, timing: dict[str, Any] | None,
) -> None:
    """One completed (billed) request, from the ``_log_usage`` seam."""
    try:
        m = _init()
        if m is None:
            return
        label = _model_label(model)
        m["requests"].labels(endpoint, backend, label, "200").inc()
        tokens = m["tokens"]
        tokens.labels(backend, label, "input").inc(max(input_tokens, 0))
        tokens.labels(backend, label, "output").inc(max(output_tokens, 0))
        if cached_tokens:
            tokens.labels(backend, label, "cache_read").inc(max(cached_tokens, 0))
        m["cost"].labels(backend, label).inc(max(float(cost), 0.0))
        if latency_ms is not None:
            m["duration"].labels(endpoint, backend, label).observe(latency_ms / 1000.0)
        timing = timing or {}
        if timing.get("ttft_ms") is not None:
            m["ttft"].labels(backend, label).observe(timing["ttft_ms"] / 1000.0)
        if timing.get("upstream_ttfb_ms") is not None:
            m["ttfb"].labels(backend, label).observe(timing["upstream_ttfb_ms"] / 1000.0)
        if timing.get("gateway_overhead_ms") is not None:
            m["overhead"].labels(backend).observe(timing["gateway_overhead_ms"] / 1000.0)
        if timing.get("output_tps") is not None:
            m["tps"].labels(backend, label).observe(timing["output_tps"])
    except Exception as exc:
        logger.warning("Metrics observe_request failed: {}: {}", type(exc).__name__, exc)


def observe_error(endpoint: str, backend: str, model: str, status_code: int) -> None:
    """One failed downstream call, from the ``_log_error`` seam."""
    try:
        m = _init()
        if m is None:
            return
        m["requests"].labels(endpoint, backend, _model_label(model), str(status_code)).inc()
    except Exception as exc:
        logger.warning("Metrics observe_error failed: {}: {}", type(exc).__name__, exc)


def observe_rejection(reason: str) -> None:
    """A request refused with 429 at the gateway (budget or concurrency)."""
    try:
        m = _init()
        if m is None:
            return
        m["rejections"].labels(reason).inc()
    except Exception as exc:
        logger.warning("Metrics observe_rejection failed: {}: {}", type(exc).__name__, exc)


def set_server_state(server: str, alive: bool, load: dict[str, int] | None) -> None:
    """Health-loop result for one on-prem server (its ``base_url``)."""
    try:
        m = _init()
        if m is None:
            return
        m["server_up"].labels(server).set(1 if alive else 0)
        for key in ("running", "waiting"):
            gauge = m[f"vllm_{key}"].labels(server)
            gauge.set((load or {}).get(key, 0) if alive else 0)
    except Exception as exc:
        logger.warning("Metrics set_server_state failed: {}: {}", type(exc).__name__, exc)


class InFlightMiddleware:
    """Pure-ASGI gauge of open HTTP requests; a stream counts until its last
    byte is sent. Added only when metrics are enabled. Probes and the scrape
    itself are not counted."""

    _SKIP = frozenset(("/metrics", "/healthz", "/readyz"))

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in self._SKIP:
            await self.app(scope, receive, send)
            return
        gauge = None
        try:
            m = _init()
            if m is not None:
                gauge = m["in_flight"]
                gauge.inc()
        except Exception:
            gauge = None
        try:
            await self.app(scope, receive, send)
        finally:
            if gauge is not None:
                try:
                    gauge.dec()
                except Exception:
                    pass
