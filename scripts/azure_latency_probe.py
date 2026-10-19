#!/usr/bin/env python3
"""Probe Azure OpenAI latency, phase by phase, directly and via the gateway.

Sends the same small streaming Responses API request to an
``[azure_models.*]`` deployment and splits each run's wall time into the
phases that can be slow:

  setup     request start → request headers on the wire: pool wait, DNS,
            TCP, corporate-proxy CONNECT, TLS (≈0 on a reused connection)
  tcp/tls   the TCP connect and TLS handshake inside ``setup``
  upload    writing the request headers and body
  ttfb      body sent → response headers received: the downstream (and
            anything in between) deciding to answer
  ttft      request start → first output delta (text, reasoning or tool args)
  total     request start → stream end
  tok/s     output tokens / (total - ttft): generation speed

Targets:
  direct    straight to ``<endpoint>/openai/v1/responses`` with the entry's
            api_key, through ``AZURE_HTTP_PROXY`` / ``AZURE_INSECURE`` exactly
            like the gateway's Azure client (``--no-proxy`` bypasses it)
  gateway   ``<gateway-url>/azure/v1/responses`` with a gateway API key: the
            gateway's pass-through route, so both targets get the same body.
            These requests are billed to that key like any other.
  both      alternates direct / gateway per round, so both see the same
            Azure conditions; the summary prints the gateway's added time

Reading the result:
  direct already slow                   → Azure or the network path, not us
    big setup / tls on fresh connections → corporate proxy / TLS inspection
    big ttfb                             → Azure queueing (check region,
                                           quota, x-ratelimit-* headers)
    low tok/s                            → Azure generation / throttling
  gateway ≫ direct                       → gateway overhead (auth/DB, lease,
                                           translation) or its client pool

Run it on the gateway host, with the gateway's environment (``.env`` is
loaded automatically, like the service does):

    uv run python scripts/azure_latency_probe.py --list
    uv run python scripts/azure_latency_probe.py -m gpt-4o -n 10
    uv run python scripts/azure_latency_probe.py -m gpt-4o -n 10 --fresh-conn
    uv run python scripts/azure_latency_probe.py -m gpt-4o -n 10 --no-proxy
    uv run python scripts/azure_latency_probe.py -m gpt-4o -n 10 --target both \\
        --gateway-url http://127.0.0.1:8000 --gateway-key sk-...
    uv run python scripts/azure_latency_probe.py -m gpt-4o --input-chars 200000 --cache-bust

``--gateway-url`` / ``--gateway-key`` default to ``GATEWAY_URL`` /
``GATEWAY_API_KEY``. API keys are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from statistics import median
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.core.config import AZURE_MODELS  # noqa: E402  (also loads .env)

# Response headers worth seeing when chasing latency or filing a ticket.
_INTERESTING_HEADERS = (
    "apim-request-id",
    "x-request-id",
    "x-ms-client-request-id",
    "x-ms-region",
    "x-ms-deployment-name",
    "azureml-model-session",
    "openai-processing-ms",
    "x-envoy-upstream-service-time",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-remaining-tokens",
    "retry-after",
    "x-model-fallback",
)

_FILLER = "The quick brown fox jumps over the lazy dog. "

_METRICS = ("setup_ms", "tcp_ms", "tls_ms", "upload_ms", "ttfb_ms", "ttft_ms", "total_ms", "tok_s")


def _responses_url(endpoint: str) -> str:
    # Same normalization as azure_proxy._build_responses_url.
    endpoint = endpoint.rstrip("/")
    if endpoint.endswith("/openai"):
        endpoint = endpoint[: -len("/openai")]
    return f"{endpoint}/openai/v1/responses"


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


def _make_client(proxy: str | None, insecure: bool) -> httpx.Client:
    # trust_env=False: the gateway's clients use an explicit transport, so
    # they never pick up HTTP(S)_PROXY from the environment either.
    return httpx.Client(
        proxy=proxy or None,
        verify=not insecure,
        trust_env=False,
        timeout=httpx.Timeout(connect=30.0, read=280.0, write=30.0, pool=30.0),
    )


def _build_body(args: argparse.Namespace, model: str) -> dict[str, Any]:
    prompt = args.prompt
    if args.input_chars > len(prompt):
        pad = args.input_chars - len(prompt)
        prompt = (_FILLER * (pad // len(_FILLER) + 1))[:pad] + "\n\n" + prompt
    if args.cache_bust:
        # At the very start, so no prefix of the prompt can hit the cache.
        prompt = f"[run {uuid.uuid4().hex}]\n" + prompt
    body: dict[str, Any] = {
        "model": model,
        "input": [{"role": "user", "content": prompt}],
        "max_output_tokens": args.max_output_tokens,
        "stream": not args.no_stream,
    }
    if args.effort:
        body["reasoning"] = {"effort": args.effort}
    return body


class _Trace:
    """httpcore trace hook: first timestamp of each phase event."""

    def __init__(self) -> None:
        self.t: dict[str, float] = {}

    def __call__(self, name: str, info: dict) -> None:
        # "http11.send_request_headers.started" → "send_request_headers.started"
        key = name.split(".", 1)[1] if name.startswith(("http11.", "http2.")) else name
        self.t.setdefault(key, time.perf_counter())

    def span(self, start: str, end: str) -> float | None:
        if start in self.t and end in self.t:
            return (self.t[end] - self.t[start]) * 1000.0
        return None


def _usage_from(resp_obj: dict) -> dict[str, int]:
    usage = resp_obj.get("usage") or {}
    return {
        "input_tokens": usage.get("input_tokens") or 0,
        "output_tokens": usage.get("output_tokens") or 0,
        "cached_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0,
        "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0,
    }


def _run_once(
    client: httpx.Client, url: str, headers: dict, body: dict, target: str,
) -> dict[str, Any]:
    trace = _Trace()
    row: dict[str, Any] = {"target": target, "status": None, "error": None}
    t0 = time.perf_counter()
    first_event = first_delta = None
    usage: dict[str, int] = {}
    try:
        req = client.build_request(
            "POST", url, json=body, headers=headers, extensions={"trace": trace},
        )
        resp = client.send(req, stream=True)
        try:
            row["status"] = resp.status_code
            row["headers"] = {
                h: resp.headers[h] for h in _INTERESTING_HEADERS if h in resp.headers
            }
            if resp.status_code != 200:
                row["error"] = resp.read().decode("utf-8", "replace")[:300]
            elif not body["stream"]:
                data = json.loads(resp.read())
                first_delta = time.perf_counter()
                usage = _usage_from(data)
            else:
                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue  # blank separators, `event:` lines, `: ping`
                    now = time.perf_counter()
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    if first_event is None:
                        first_event = now
                    etype = event.get("type", "")
                    if first_delta is None and etype.endswith(".delta"):
                        first_delta = now
                    if etype == "response.completed":
                        usage = _usage_from(event.get("response") or {})
                    elif etype in ("error", "response.failed", "response.error"):
                        row["error"] = json.dumps(event)[:300]
        finally:
            resp.close()
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
    t_end = time.perf_counter()

    def since_t0(t: float | None) -> float | None:
        return (t - t0) * 1000.0 if t is not None else None

    row["setup_ms"] = since_t0(trace.t.get("send_request_headers.started"))
    row["tcp_ms"] = trace.span("connection.connect_tcp.started", "connection.connect_tcp.complete")
    row["tls_ms"] = trace.span("connection.start_tls.started", "connection.start_tls.complete")
    row["upload_ms"] = trace.span("send_request_headers.started", "send_request_body.complete")
    row["ttfb_ms"] = trace.span("send_request_body.complete", "receive_response_headers.complete")
    row["first_event_ms"] = since_t0(first_event)
    row["ttft_ms"] = since_t0(first_delta)
    row["total_ms"] = (t_end - t0) * 1000.0
    row.update(usage)
    gen_s = (t_end - first_delta) if first_delta is not None else 0.0
    out = usage.get("output_tokens") or 0
    row["tok_s"] = out / gen_s if gen_s > 0.05 and out else None
    return row


def _fmt(v: float | None, width: int = 7) -> str:
    return f"{v:>{width}.0f}" if v is not None else f"{'-':>{width}}"


_HEADER = (
    f"{'#':>3} {'target':<8} {'st':>3} {'setup':>7} {'tcp':>7} {'tls':>7} "
    f"{'upload':>7} {'ttfb':>7} {'ttft':>7} {'total':>7} {'in':>7} {'cached':>7} "
    f"{'out':>6} {'tok/s':>6}  request-id"
)


def _print_row(i: int, row: dict[str, Any], verbose: bool) -> None:
    hdrs = row.get("headers") or {}
    rid = hdrs.get("apim-request-id") or hdrs.get("x-request-id") or ""
    print(
        f"{i:>3} {row['target']:<8} {row['status'] or 'ERR':>3} "
        f"{_fmt(row['setup_ms'])} {_fmt(row['tcp_ms'])} {_fmt(row['tls_ms'])} "
        f"{_fmt(row['upload_ms'])} {_fmt(row['ttfb_ms'])} {_fmt(row['ttft_ms'])} "
        f"{_fmt(row['total_ms'])} {row.get('input_tokens', 0):>7} "
        f"{row.get('cached_tokens', 0):>7} {row.get('output_tokens', 0):>6} "
        f"{_fmt(row['tok_s'], 6)}  {rid}"
    )
    if row["error"]:
        print(f"      error: {row['error']}")
    if verbose and hdrs:
        for k, v in hdrs.items():
            print(f"      {k}: {v}")


def _pct(values: list[float], q: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def _summarize(rows: list[dict[str, Any]], target: str) -> dict[str, float]:
    ok = [r for r in rows if r["target"] == target and r["status"] == 200 and not r["error"]]
    failed = sum(1 for r in rows if r["target"] == target) - len(ok)
    print(f"\n{target}: {len(ok)} ok, {failed} failed")
    medians: dict[str, float] = {}
    if not ok:
        return medians
    print(f"  {'metric':<10} {'min':>8} {'median':>8} {'p90':>8} {'max':>8}")
    for m in _METRICS:
        vals = [r[m] for r in ok if r.get(m) is not None]
        if not vals:
            continue
        medians[m] = median(vals)
        unit = "" if m == "tok_s" else " ms"
        print(
            f"  {m:<10} {min(vals):>8.0f} {medians[m]:>8.0f} "
            f"{_pct(vals, 0.9):>8.0f} {max(vals):>8.0f}{unit}"
        )
    return medians


def _list_models() -> None:
    if not AZURE_MODELS:
        print("No [azure_models.*] entries in config.toml.")
        return
    for alias, e in AZURE_MODELS.items():
        host = httpx.URL(e.get("endpoint") or "http://-").host
        print(
            f"{alias:<30} type={e.get('type', 'llm'):<5} deployment={e.get('deployment', '')}"
            f"  host={host}  fallback={e.get('rate_limit_fallback', '-')}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("-m", "--model", help="[azure_models.*] alias to probe")
    ap.add_argument("--list", action="store_true", help="list Azure aliases and exit")
    ap.add_argument("-n", "--runs", type=int, default=5, help="rounds (default 5)")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--target", choices=("direct", "gateway", "both"), default="direct")
    ap.add_argument("--gateway-url", default=os.getenv("GATEWAY_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--gateway-key", default=os.getenv("GATEWAY_API_KEY", ""))
    ap.add_argument("--gateway-insecure", action="store_true", help="skip TLS verify to the gateway")
    ap.add_argument("--no-proxy", action="store_true", help="direct target: ignore AZURE_HTTP_PROXY")
    ap.add_argument("--fresh-conn", action="store_true",
                    help="new connection per request (measures DNS/TCP/proxy/TLS every time)")
    ap.add_argument("--prompt", default="Count from 1 to 50, separated by spaces.")
    ap.add_argument("--input-chars", type=int, default=0,
                    help="pad the prompt to this many characters (≈4 chars/token) to test prefill")
    ap.add_argument("--cache-bust", action="store_true", help="random prefix so prompt caching can't hit")
    ap.add_argument("--max-output-tokens", type=int, default=256)
    ap.add_argument("--effort", help="reasoning.effort to send (reasoning deployments only)")
    ap.add_argument("--no-stream", action="store_true", help="non-streaming request (no ttft)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print interesting response headers")
    ap.add_argument("--json", metavar="FILE", help="also write every run as JSON lines to FILE")
    args = ap.parse_args()

    if args.list:
        _list_models()
        return 0
    if not args.model:
        ap.error("--model is required (see --list)")
    entry = AZURE_MODELS.get(args.model)
    if entry is None:
        ap.error(f"'{args.model}' is not an [azure_models.*] alias (see --list)")
    if not entry.get("endpoint") or not entry.get("deployment"):
        ap.error(f"'{args.model}' is missing endpoint or deployment")

    targets = ["direct", "gateway"] if args.target == "both" else [args.target]
    if "gateway" in targets and not args.gateway_key:
        ap.error("gateway target needs --gateway-key (or GATEWAY_API_KEY)")

    proxy = None if args.no_proxy else (os.getenv("AZURE_HTTP_PROXY", "").strip() or None)
    insecure = _env_flag("AZURE_INSECURE")
    specs = {
        "direct": dict(
            url=_responses_url(entry["endpoint"]),
            headers={"Content-Type": "application/json", "api-key": entry.get("api_key", "")},
            model=entry["deployment"],
            client_args=(proxy, insecure),
        ),
        "gateway": dict(
            url=args.gateway_url.rstrip("/") + "/azure/v1/responses",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {args.gateway_key}"},
            model=args.model,
            client_args=(None, args.gateway_insecure),
        ),
    }

    print(f"model={args.model} deployment={entry['deployment']} stream={not args.no_stream}")
    for t in targets:
        extra = ""
        if t == "direct":
            proxy_host = httpx.URL(proxy).host if proxy else "none"
            extra = f"  proxy={proxy_host} insecure={insecure}"
        print(f"  {t:<8} {specs[t]['url']}{extra}")
    print(f"connections={'fresh per request' if args.fresh_conn else 'reused (first run pays setup)'}")
    print("all times in ms from request start, except tcp/tls/upload/ttfb (phase durations)\n")
    print(_HEADER)

    clients = {t: _make_client(*specs[t]["client_args"]) for t in targets}
    rows: list[dict[str, Any]] = []
    sink = open(args.json, "w") if args.json else None
    try:
        for i in range(1, args.runs + 1):
            # Alternate the order each round so neither target always goes first.
            order = targets if i % 2 else list(reversed(targets))
            for t in order:
                spec = specs[t]
                if args.fresh_conn:
                    clients[t].close()
                    clients[t] = _make_client(*spec["client_args"])
                body = _build_body(args, spec["model"])
                row = _run_once(clients[t], spec["url"], spec["headers"], body, t)
                row["run"] = i
                rows.append(row)
                _print_row(i, row, args.verbose)
                if sink:
                    sink.write(json.dumps(row) + "\n")
                    sink.flush()
                if args.interval > 0:
                    time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        for c in clients.values():
            c.close()
        if sink:
            sink.close()

    medians = {t: _summarize(rows, t) for t in targets}
    if len(targets) == 2 and medians["direct"] and medians["gateway"]:
        print("\ngateway - direct (median):")
        for m in ("ttfb_ms", "ttft_ms", "total_ms"):
            if m in medians["direct"] and m in medians["gateway"]:
                print(f"  {m:<10} {medians['gateway'][m] - medians['direct'][m]:>+8.0f} ms")
        print("  (gateway ttfb includes its own whole trip to Azure; compare ttft/total)")
    return 0 if any(r["status"] == 200 and not r["error"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
