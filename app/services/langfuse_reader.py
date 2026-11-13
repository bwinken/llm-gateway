"""Reading back from Langfuse: the Public API, for the weekly reports.

``observability.py`` writes one generation per request; this module is the
only place that reads. It is used by the report job and the admin's preview,
never on the request path: every call is blocking (run it in a thread) and
bounded by ``TIMEOUT_S``.

Two API generations exist and a self-hosted server may be on either:

* ``GET /api/public/v2/metrics`` (Langfuse v4)
* ``GET /api/public/metrics`` (Langfuse v3; removed from v4)

``metrics`` asks v2 first and falls back to v1 when the server answers 404,
remembering which one worked. The queries the reports send are valid on both:
no filters, and only dimensions both have (``providedModelName``, ``level``,
``name``, ``stringValue``). ``userId`` is not one of them (v2 refuses it as a
grouping dimension), so per-account figures come from the score list
(``scores``), which carries each score's trace user and tags.

A response row names a metric ``<aggregation>_<measure>`` (``count_count``,
``p95_latency``); ``value`` also accepts the reverse spelling, and numbers
that arrive as strings.

Failures raise ``LangfuseError`` with a short summary (status and the first
line of the server's message, never the credentials); the reports show that
summary in place of the section and still go out.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import httpx

TIMEOUT_S = 30.0
ROW_LIMIT = 1000  # the API's maximum
PAGE_SIZE = 100

_METRICS_PATHS = ("/api/public/v2/metrics", "/api/public/metrics")
_SCORES_PATH = "/api/public/v2/scores"

# Index into _METRICS_PATHS of the endpoint this server answered on.
_metrics_path: int | None = None


class LangfuseError(RuntimeError):
    """A Langfuse read failed. The message is safe to show an admin."""


def is_configured() -> bool:
    return bool(
        os.getenv("LANGFUSE_HOST") and os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")
    )


def monitoring() -> dict[str, Any]:
    """Whether the gateway is recording to Langfuse at all: ``{"on", "reason"}``.
    On means the three settings are present and ``LANGFUSE_SAMPLE_RATE`` is
    above 0 (at 0 nothing is recorded, so there is nothing to report). The
    weekly reports can only be turned on, and are only sent, while it is."""
    missing = [name for name in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY") if not os.getenv(name)]
    if missing:
        return {"on": False, "reason": f"Langfuse is not configured ({', '.join(missing)} in .env)."}
    if sample_rate() <= 0.0:
        return {"on": False, "reason": "LANGFUSE_SAMPLE_RATE is 0: nothing is being recorded in Langfuse."}
    return {"on": True, "reason": ""}


def host() -> str:
    return os.getenv("LANGFUSE_HOST", "").strip().rstrip("/")


def sample_rate() -> float:
    """The share of requests the gateway records (``LANGFUSE_SAMPLE_RATE``):
    below 1.0 every count read here is a sample."""
    from app.services.observability import get_sample_rate

    return get_sample_rate()


def iso(moment: datetime) -> str:
    """An aware datetime as the API wants it: UTC, millisecond precision."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def reset() -> None:
    """Test helper: forget which metrics endpoint answered."""
    global _metrics_path
    _metrics_path = None


def new_client() -> httpx.Client:
    """A client for one report: Basic auth with the project's key pair."""
    if not is_configured():
        raise LangfuseError("Langfuse is not configured (LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY).")
    return httpx.Client(
        base_url=host(),
        auth=(os.getenv("LANGFUSE_PUBLIC_KEY", ""), os.getenv("LANGFUSE_SECRET_KEY", "")),
        timeout=TIMEOUT_S,
    )


def _summary(response: httpx.Response) -> str:
    """Status and the server's own message, one line, bounded."""
    text = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            text = str(body.get("message") or body.get("error") or "")
    except Exception:
        text = response.text or ""
    lines = text.strip().splitlines()
    return f"HTTP {response.status_code}" + (f": {lines[0][:200]}" if lines else "")


def _get(client: httpx.Client, path: str, params: dict[str, Any]) -> httpx.Response:
    try:
        return client.get(path, params=params)
    except httpx.HTTPError as exc:
        # httpx timeouts stringify to an empty message: the type is the news.
        raise LangfuseError(f"{type(exc).__name__} reaching Langfuse") from exc


def _rows(response: httpx.Response) -> list[dict]:
    try:
        data = response.json().get("data")
    except Exception as exc:
        raise LangfuseError("Langfuse answered with something that is not JSON") from exc
    return [row for row in data or [] if isinstance(row, dict)]


def metrics(client: httpx.Client, query: dict[str, Any]) -> list[dict]:
    """Run one Metrics API query; the rows of ``data``."""
    global _metrics_path
    query = {**query, "config": {"row_limit": ROW_LIMIT, **(query.get("config") or {})}}
    params = {"query": json.dumps(query, separators=(",", ":"))}
    order = (0, 1) if _metrics_path is None else (_metrics_path,)
    response = None
    for index in order:
        response = _get(client, _METRICS_PATHS[index], params)
        if response.status_code == 404 and len(order) > 1:
            continue  # this server has the other generation of the API
        if response.status_code != 200:
            raise LangfuseError(_summary(response))
        _metrics_path = index
        return _rows(response)
    raise LangfuseError(_summary(response) if response is not None else "no metrics endpoint")


def value(row: dict, measure: str, aggregation: str) -> float | None:
    """One metric of a response row, whichever way round the server names it."""
    for key in (f"{aggregation}_{measure}", f"{measure}_{aggregation}"):
        raw = row.get(key)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    return None


def count(row: dict) -> int:
    return int(value(row, "count", "count") or 0)


def scores(
    client: httpx.Client, *, name: str, trace_tags: list[str], start: datetime, end: datetime, max_pages: int,
) -> tuple[list[dict], bool]:
    """Every score called ``name`` on a trace carrying all of ``trace_tags``
    in ``[start, end)``: ``(items, truncated)``. Each item has ``stringValue``
    and, under ``trace``, the trace's ``userId`` and ``tags``. Reads at most
    ``max_pages`` pages of ``PAGE_SIZE``; ``truncated`` says more were left."""
    items: list[dict] = []
    page = 1
    while page <= max_pages:
        response = _get(client, _SCORES_PATH, {
            "name": name, "traceTags": trace_tags, "fromTimestamp": iso(start), "toTimestamp": iso(end),
            "dataType": "CATEGORICAL", "limit": PAGE_SIZE, "page": page,
        })
        if response.status_code != 200:
            raise LangfuseError(_summary(response))
        batch = _rows(response)
        items.extend(batch)
        try:
            total_pages = int((response.json().get("meta") or {}).get("totalPages") or 0)
        except Exception:
            total_pages = 0
        if len(batch) < PAGE_SIZE or (total_pages and page >= total_pages):
            return items, False
        page += 1
    return items, True
