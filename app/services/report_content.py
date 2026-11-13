"""What the two weekly report mails say: their figures and their HTML.

``usage_report`` and ``guardrail_report`` each take a session and a
``Period`` (one Asia/Taipei calendar week, Monday to Sunday) and return a
``Rendered`` mail: subject, HTML body and the PNG charts the body shows as
``cid:`` images. ``weekly_reports`` schedules and sends them; the admin's
preview renders the same thing in a browser.

Where each figure comes from:

* Tokens, requests, cost, active accounts: the gateway's own usage record,
  through ``usage_rollup.aggregate``. These are complete and match the
  admin page.
* Latency (p50 / p95), time to first token and error rate: Langfuse
  (``langfuse_reader``). ``usage_logs`` holds neither a duration nor a
  failed request.
* Guardrail verdicts, categories and the accounts behind them: Langfuse
  too (the ``guardrail*`` scores and tags ``observability`` writes).

So a report needs Langfuse for everything but its token section. When a
read fails the section says so, with the reader's error summary, and the
rest of the mail still goes out; ``Rendered.problems`` carries the same
lines to the job's status.

What counts as worth pointing out (an error rate, a slower p95, an account
with repeated unsafe verdicts) is the constants below: code, like the client
checks' thresholds, not an admin setting. The guardrail mail carries counts
per account and never the screened text. Everything that comes from outside
(model names, account names, categories, error text) is escaped.
"""

from __future__ import annotations

import html
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlmodel import Session, select

from app.core import config
from app.core.config import APP_TITLE
from app.core.logger import logger
from app.core.timeutil import LOCAL_TZ, local_day_start_utc_of, local_today
from app.models.schema import User
from app.services import langfuse_reader, model_catalog, report_charts
from app.services.langfuse_reader import LangfuseError
from app.services.usage_rollup import aggregate

BACKEND_LABELS = {"vllm": "Local (vLLM)", "azure": "Azure", "bedrock": "AWS (Bedrock)"}
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# ── Usage report: what gets a line under "Worth a look" ──
ERROR_RATE_ALERT = 0.05      # a model failing this share of its requests ...
ALERT_MIN_REQUESTS = 50      # ... or slowing down, with at least this many requests
P95_WORSE_RATIO = 1.5        # p95 at least this many times last week's
PANEL_MODELS = 9             # latency panels per backend (the busiest models)
TABLE_MODELS = 40            # table rows per backend

# ── Guardrail report ──
JUDGED = ("safe", "controversial", "unsafe")
NOT_JUDGED = ("timeout", "error", "parse_error", "pending")
NOT_JUDGED_ALERT = 0.05      # share of screened requests the guard failed to judge
UNSAFE_RISE_RATIO = 1.5      # unsafe verdicts against last week's ...
UNSAFE_RISE_MIN = 10         # ... and at least this many more
NEW_CATEGORY_MIN = 5         # a category absent last week, seen this often
# An account is pointed out when any of these holds for the week:
USER_UNSAFE_MIN = 5          # unsafe verdicts
USER_BLOCKED_MIN = 3         # requests refused
USER_JAILBREAK_MIN = 2       # verdicts in the Jailbreak category
USER_SHARE_MIN = 0.2         # unsafe share of its local requests ...
USER_SHARE_UNSAFE_MIN = 3    # ... with at least this many unsafe verdicts
OTHER_ACCOUNTS = 10          # further accounts listed below the pointed-out ones
FLAGGED_MAX_PAGES = 50       # pages of 100 flagged requests read per verdict

_INK, _INK2, _MUTED = "#0b0b0b", "#52514e", "#898781"
_GOOD, _BAD = "#006300", "#d03b3b"
_RULE, _RULE_STRONG, _TILE = "#e1e0d9", "#c3c2b7", "#f4f3ef"


@dataclass(frozen=True)
class Period:
    """One Asia/Taipei calendar week, named by its Monday."""
    start: date

    @property
    def end(self) -> date:
        return self.start + timedelta(days=7)

    @property
    def days(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(7)]

    @property
    def previous(self) -> Period:
        return Period(self.start - timedelta(days=7))

    @property
    def label(self) -> str:
        return f"{self.start.isoformat()} to {(self.end - timedelta(days=1)).isoformat()}"


def week_of(day: date) -> Period:
    """The week ``day`` is in."""
    return Period(day - timedelta(days=day.weekday()))


def last_week(today: date | None = None) -> Period:
    """The last full week before the one ``today`` is in."""
    return week_of(today or local_today()).previous


@dataclass
class Rendered:
    subject: str
    html: str
    images: dict[str, bytes] = field(default_factory=dict)
    # Sections that could not be read, one line each (the job's status line).
    problems: list[str] = field(default_factory=list)


# ── Formatting ──

def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _int(number: Any) -> str:
    return f"{int(number or 0):,}"


def _usd(amount: Any) -> str:
    return f"${float(amount or 0):,.2f}"


def _pct(share: float | None, digits: int = 1) -> str:
    return "–" if share is None else f"{share * 100:.{digits}f}%"


def _ms(value: float | None) -> str:
    if value is None:
        return "–"
    if value < 1000:
        return f"{value:.0f} ms"
    return f"{value / 1000:.1f} s" if value < 10_000 else f"{value / 1000:.0f} s"


def _ms_tick(value: float) -> str:
    if value <= 0:
        return "0"
    if value < 1000:
        return f"{value:.0f}ms"
    return f"{value / 1000:.1f}".rstrip("0").rstrip(".") + "s"


def _pct_tick(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".") + "%"


def _share(part: float, whole: float) -> float | None:
    return part / whole if whole else None


def _delta(current: float | None, before: float | None, worse: str | None = None) -> str:
    """Change against last week as ``▲ 12%`` / ``▼ 3%``. ``worse`` says which
    direction is bad (``"up"`` for errors and latency); without it the change
    is printed in plain ink (more tokens is neither good nor bad)."""
    if current is None or before is None:
        return f'<span style="color:{_MUTED}">–</span>'
    if not before:
        return f'<span style="color:{_MUTED}">{"new" if current else "–"}</span>'
    change = (current - before) / before
    if abs(change) < 0.005:
        return f'<span style="color:{_MUTED}">no change</span>'
    up = change > 0
    color = _INK2 if worse is None else (_BAD if up == (worse == "up") else _GOOD)
    return f'<span style="color:{color}">{"▲" if up else "▼"} {abs(change) * 100:.0f}%</span>'


def _heading(text: str) -> str:
    return (
        f'<p style="font-size:16px;font-weight:600;color:{_INK};margin:26px 0 6px;'
        f'border-bottom:1px solid {_RULE_STRONG};padding-bottom:5px">{_esc(text)}</p>'
    )


def _note(text_html: str) -> str:
    return f'<p style="font-size:12px;color:{_MUTED};margin:6px 0">{text_html}</p>'


def _table(columns: list[tuple[str, str]], rows: list[list[str]]) -> str:
    """``columns`` are ``(label, align)``; cells are HTML already."""
    head = "".join(
        f'<th style="text-align:{align};padding:6px 8px;border-bottom:1px solid {_RULE_STRONG};'
        f'color:{_INK2};font-size:12px;font-weight:600;white-space:nowrap">{_esc(label)}</th>'
        for label, align in columns
    )
    body = "".join(
        "<tr>" + "".join(
            f'<td style="text-align:{columns[i][1]};padding:6px 8px;border-bottom:1px solid {_RULE};'
            f'font-size:13px;color:{_INK};vertical-align:top{";white-space:nowrap" if i == 0 else ""}">{cell}</td>'
            for i, cell in enumerate(row)
        ) + "</tr>"
        for row in rows
    )
    return (
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;margin:4px 0 10px">'
        f"<tr>{head}</tr>{body}</table>"
    )


def _tiles(tiles: list[tuple[str, str, str]]) -> str:
    """A row of headline numbers: ``(label, value, line below as HTML)``."""
    width = 100 // max(len(tiles), 1)
    cells = "".join(
        f'<td width="{width}%" style="background:{_TILE};padding:10px 12px;border-right:6px solid #ffffff;vertical-align:top">'
        f'<div style="font-size:12px;color:{_INK2}">{_esc(label)}</div>'
        f'<div style="font-size:21px;font-weight:600;color:{_INK};line-height:1.3">{_esc(value)}</div>'
        f'<div style="font-size:12px;color:{_INK2}">{below or "&nbsp;"}</div></td>'
        for label, value, below in tiles
    )
    return f'<table cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:10px 0 4px"><tr>{cells}</tr></table>'


def _bar(share: float | None, color: str) -> str:
    """A share as a thin bar a mail client can draw: two table cells."""
    filled = max(0, min(100, round((share or 0) * 100)))
    if filled <= 0:
        return ""
    rest = f'<td width="{100 - filled}%" style="font-size:0;line-height:0">&nbsp;</td>' if filled < 100 else ""
    return (
        '<table cellpadding="0" cellspacing="0" border="0" width="120" style="display:inline-table"><tr>'
        f'<td width="{filled}%" height="8" bgcolor="{color}" style="font-size:0;line-height:0">&nbsp;</td>{rest}'
        "</tr></table>"
    )


def _swatch(color: str) -> str:
    return f'<span style="color:{color}">&#9632;</span>&nbsp;'


def _img(name: str, alt: str) -> str:
    return (
        f'<img src="cid:{name}" width="{report_charts.WIDTH_PX}" alt="{_esc(alt)}" '
        'style="display:block;width:100%;max-width:680px;height:auto;border:0;margin:6px 0">'
    )


def _callout(lines: list[str], title: str, tone: str) -> str:
    """``lines`` are HTML. ``tone`` is ``warn`` or ``ok``."""
    edge, back = (("#fab219", "#fff8e6") if tone == "warn" else ("#0ca30c", "#eef8ee"))
    items = "".join(f'<li style="margin:3px 0">{line}</li>' for line in lines)
    return (
        f'<table cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:12px 0"><tr>'
        f'<td width="4" bgcolor="{edge}" style="font-size:0">&nbsp;</td>'
        f'<td style="background:{back};padding:10px 14px;font-size:13px;color:{_INK}">'
        f'<div style="font-weight:600;margin-bottom:2px">{_esc(title)}</div>'
        + (f'<ul style="margin:4px 0 0 18px;padding:0">{items}</ul>' if lines else "")
        + "</td></tr></table>"
    )


def _unavailable(what: str, error: str) -> str:
    return (
        f'<p style="font-size:13px;color:{_INK2};background:{_TILE};padding:10px 12px;margin:8px 0">'
        f"{_esc(what)} could not be read from Langfuse: {_esc(error)}</p>"
    )


def _page(title: str, period: Period, parts: list[str], notes: list[str]) -> str:
    made = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    footer = "".join(f"<div>{note}</div>" for note in notes)
    return (
        f'<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:{_INK};line-height:1.5;max-width:700px">'
        f'<p style="font-size:20px;font-weight:600;margin:0">{_esc(title)}</p>'
        f'<p style="font-size:13px;color:{_INK2};margin:2px 0 0">{_esc(APP_TITLE)} &middot; week of {_esc(period.label)} '
        "(Monday to Sunday, Asia/Taipei)</p>"
        + "".join(parts)
        + f'<div style="color:{_MUTED};font-size:12px;border-top:1px solid {_RULE};padding-top:10px;margin-top:24px">'
        f"{footer}<div>Generated {made} (Asia/Taipei) by {_esc(APP_TITLE)}. This is an automated message: "
        "replies are not read. Schedule and recipients: Admin &rarr; Settings &rarr; Weekly reports.</div></div></div>"
    )


def _chart(images: dict[str, bytes], problems: list[str], name: str, alt: str, draw) -> str:
    """Draw one chart into ``images`` and return its ``<img>``; a chart that
    fails costs the picture, not the mail (the table beside it stays)."""
    try:
        images[name] = draw()
    except Exception as exc:
        logger.warning("Report chart failed | chart={} error={}: {}", name, type(exc).__name__, exc)
        problems.append(f"chart {name}: {type(exc).__name__}")
        return ""
    return _img(name, alt)


def _sampling_note() -> str | None:
    rate = langfuse_reader.sample_rate()
    if rate >= 1.0:
        return None
    return (
        f"Langfuse records {rate:.0%} of requests (LANGFUSE_SAMPLE_RATE): every count read from it is a sample "
        "of that size. Percentiles and rates are unaffected; token figures are complete."
    )


# ── Usage report: figures ──

def _backend_order(names) -> list[str]:
    extra = sorted(set(names) - set(model_catalog.BACKEND_ORDER))
    return [b for b in model_catalog.BACKEND_ORDER if b in names] + extra


def usage_figures(session: Session, period: Period) -> dict[str, Any]:
    """The week's usage from the gateway's own record."""
    by_day = aggregate(session, period.start, period.end, ("day", "backend"))
    by_model = aggregate(session, period.start, period.end, ("backend", "model", "model_type"))
    before = aggregate(session, period.previous.start, period.previous.end, ("backend",))
    accounts = len(aggregate(session, period.start, period.end, ("user_id",)))

    def tokens(row: dict) -> int:
        return int(row["input_tokens"]) + int(row["output_tokens"])

    totals: dict[str, dict[str, Any]] = {}
    daily: dict[str, list[float]] = {}
    for (day, backend), row in by_day.items():
        total = totals.setdefault(backend, {"requests": 0, "input_tokens": 0, "output_tokens": 0, "tokens": 0, "cost": 0.0})
        total["requests"] += int(row["requests"])
        total["input_tokens"] += int(row["input_tokens"])
        total["output_tokens"] += int(row["output_tokens"])
        total["tokens"] += tokens(row)
        total["cost"] += float(row["cost_usd"])
        daily.setdefault(backend, [0.0] * 7)[(day - period.start).days] += tokens(row)
    models = [
        {
            "backend": backend, "model": model, "type": model_type, "requests": int(row["requests"]),
            "input_tokens": int(row["input_tokens"]), "output_tokens": int(row["output_tokens"]),
            "tokens": tokens(row), "cost": float(row["cost_usd"]),
        }
        for (backend, model, model_type), row in by_model.items()
    ]
    previous = {
        backend: {"tokens": tokens(row), "requests": int(row["requests"])} for (backend,), row in before.items()
    }
    return {
        "backends": _backend_order(set(totals) | set(previous)), "totals": totals, "daily": daily,
        "models": models, "previous": previous, "accounts": accounts,
    }


_COUNT = {"measure": "count", "aggregation": "count"}
_LATENCY = [{"measure": "latency", "aggregation": "p50"}, {"measure": "latency", "aggregation": "p95"}]
_TTFT = [{"measure": "timeToFirstToken", "aggregation": "p50"}, {"measure": "timeToFirstToken", "aggregation": "p95"}]


def _observations(client, start: date, end: date, measures: list[dict]) -> dict[str, dict[str, Any]]:
    """Per model over local days ``[start, end)``: requests that succeeded,
    failed (level ERROR) or were refused by the guardrail (WARNING), and the
    percentiles of the ones that succeeded."""
    rows = langfuse_reader.metrics(client, {
        "view": "observations",
        "dimensions": [{"field": "providedModelName"}, {"field": "level"}],
        "metrics": [_COUNT, *measures],
        "filters": [],
        "fromTimestamp": langfuse_reader.iso(local_day_start_utc_of(start)),
        "toTimestamp": langfuse_reader.iso(local_day_start_utc_of(end)),
    })
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        model = str(row.get("providedModelName") or "(no model)")
        level = str(row.get("level") or "DEFAULT").upper()
        entry = out.setdefault(model, {"ok": 0, "errors": 0, "blocked": 0})
        number = langfuse_reader.count(row)
        if level == "ERROR":
            entry["errors"] += number
        elif level == "WARNING":
            entry["blocked"] += number
        else:
            entry["ok"] += number
            if level == "DEFAULT":
                for measure in measures:
                    key = f"{measure['measure']}_{measure['aggregation']}"
                    entry[key] = langfuse_reader.value(row, measure["measure"], measure["aggregation"])
    return out


def observed_figures(period: Period) -> dict[str, Any]:
    """Latency and failures per model from Langfuse: the week, the week
    before, and each local day (a percentile cannot be re-bucketed, and the
    API's own day buckets are UTC days, so each day is its own query)."""
    with langfuse_reader.new_client() as client:
        week = _observations(client, period.start, period.end, _LATENCY)
        previous = _observations(client, period.previous.start, period.previous.end, _LATENCY)
        days = [_observations(client, day, day + timedelta(days=1), _LATENCY) for day in period.days]
        try:
            ttft: dict[str, dict[str, Any]] | None = _observations(client, period.start, period.end, _TTFT)
        except LangfuseError as exc:
            logger.warning("Report: time to first token not read | error={}", exc)
            ttft = None
    return {"week": week, "previous": previous, "days": days, "ttft": ttft}


def _model_backends(models: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    """``model -> backend`` and ``model -> type``: where the week's usage
    says the model was served (the backend with most of its requests), else
    where the configuration has it, else on-prem (an unknown alias falls
    back to vLLM)."""
    backend_of: dict[str, str] = {}
    type_of: dict[str, str] = {}
    busiest: dict[str, int] = {}
    for row in models:
        if row["requests"] > busiest.get(row["model"], -1):
            busiest[row["model"]] = row["requests"]
            backend_of[row["model"]] = row["backend"]
            type_of[row["model"]] = row["type"]
    snapshots = {
        "vllm": config.get_model_routing_snapshot(),
        "azure": config.get_azure_models_snapshot(),
        "bedrock": config.get_bedrock_models_snapshot(),
    }
    for backend, snapshot in snapshots.items():
        for alias, entry in snapshot.items():
            backend_of.setdefault(alias, backend)
            type_of.setdefault(alias, str((entry or {}).get("type") or "llm"))
    return backend_of, type_of


def _error_rate(entry: dict | None) -> float | None:
    if not entry:
        return None
    return _share(entry["errors"], entry["ok"] + entry["errors"])


def _sum(entries, key: str) -> int:
    return sum(int(entry.get(key) or 0) for entry in entries)


# ── Usage report: the mail ──

def usage_report(session: Session, period: Period) -> Rendered:
    usage = usage_figures(session, period)
    session.commit()  # nothing to write: hand the connection back before the Langfuse calls
    observed: dict[str, Any] | None = None
    error = ""
    if not langfuse_reader.is_configured():
        error = "Langfuse is not configured."
    else:
        try:
            observed = observed_figures(period)
        except LangfuseError as exc:
            error = str(exc)
            logger.warning("Usage report: Langfuse read failed | error={}", error)
    return render_usage(period, usage, observed, error)


def render_usage(period: Period, usage: dict[str, Any], observed: dict[str, Any] | None, error: str = "") -> Rendered:
    images: dict[str, bytes] = {}
    problems: list[str] = [f"langfuse: {error}"] if error else []
    parts: list[str] = []
    labels = list(WEEKDAYS)
    backends = usage["backends"]
    totals, previous = usage["totals"], usage["previous"]
    backend_of, type_of = _model_backends(usage["models"])

    all_tokens = sum(t["tokens"] for t in totals.values())
    all_requests = sum(t["requests"] for t in totals.values())
    all_cost = sum(t["cost"] for t in totals.values())
    before_tokens = sum(p["tokens"] for p in previous.values())
    before_requests = sum(p["requests"] for p in previous.values())

    week = (observed or {}).get("week") or {}
    last = (observed or {}).get("previous") or {}
    days = (observed or {}).get("days") or []
    ttft = (observed or {}).get("ttft")
    overall = _share(_sum(week.values(), "errors"), _sum(week.values(), "ok") + _sum(week.values(), "errors"))
    overall_before = _share(_sum(last.values(), "errors"), _sum(last.values(), "ok") + _sum(last.values(), "errors"))

    parts.append(_tiles([
        ("Tokens", report_charts.compact(all_tokens), _delta(all_tokens, before_tokens) + " vs last week"),
        ("Requests", report_charts.compact(all_requests), _delta(all_requests, before_requests) + " vs last week"),
        ("Cost", _usd(all_cost), ""),
        ("Error rate", _pct(overall) if observed is not None else "–",
         (_delta(overall, overall_before, worse="up") + " vs last week") if observed is not None else "no Langfuse data"),
        ("Active accounts", _int(usage["accounts"]), ""),
    ]))

    # Worth a look: failing or slowing models.
    notable: list[str] = []
    for model in sorted(week, key=lambda m: -(week[m]["ok"] + week[m]["errors"])):
        entry = week[model]
        served = entry["ok"] + entry["errors"]
        rate = _error_rate(entry)
        where = BACKEND_LABELS.get(backend_of.get(model, "vllm"), backend_of.get(model, "vllm"))
        if served >= ALERT_MIN_REQUESTS and rate is not None and rate >= ERROR_RATE_ALERT:
            notable.append(
                f"<b>{_esc(model)}</b> ({_esc(where)}) failed {_pct(rate)} of its requests "
                f"({_int(entry['errors'])} of {_int(served)})."
            )
        now95, then = entry.get("latency_p95"), last.get(model) or {}
        then95 = then.get("latency_p95")
        if (
            now95 and then95 and entry["ok"] >= ALERT_MIN_REQUESTS and then.get("ok", 0) >= ALERT_MIN_REQUESTS
            and now95 >= then95 * P95_WORSE_RATIO
        ):
            notable.append(
                f"<b>{_esc(model)}</b> ({_esc(where)}) got slower: p95 {_ms(now95)}, from {_ms(then95)} last week."
            )
    if notable:
        parts.append(_callout(notable, "⚠ Worth a look", "warn"))

    # 1. Tokens by backend.
    parts.append(_heading("Token usage by backend"))
    if not all_tokens:
        parts.append(f'<p style="font-size:13px;color:{_INK2}">No usage was recorded this week.</p>')
    else:
        rows = []
        for backend in backends:
            total = totals.get(backend) or {"requests": 0, "input_tokens": 0, "output_tokens": 0, "tokens": 0, "cost": 0.0}
            color = report_charts.BACKEND_COLORS.get(backend, _MUTED)
            share = _share(total["tokens"], all_tokens)
            rows.append([
                _swatch(color) + _esc(BACKEND_LABELS.get(backend, backend)),
                _int(total["requests"]), _int(total["input_tokens"]), _int(total["output_tokens"]),
                f"<b>{_int(total['tokens'])}</b>", f"{_bar(share, color)}&nbsp;{_pct(share)}",
                _delta(total["tokens"], (previous.get(backend) or {}).get("tokens", 0)), _usd(total["cost"]),
            ])
        rows.append([
            "<b>Total</b>", _int(all_requests), _int(sum(t["input_tokens"] for t in totals.values())),
            _int(sum(t["output_tokens"] for t in totals.values())), f"<b>{_int(all_tokens)}</b>", "",
            _delta(all_tokens, before_tokens), _usd(all_cost),
        ])
        parts.append(_table(
            [("Backend", "left"), ("Requests", "right"), ("Input tokens", "right"), ("Output tokens", "right"),
             ("Total tokens", "right"), ("Share", "left"), ("vs last week", "right"), ("Cost", "right")],
            rows,
        ))
        series = [
            (BACKEND_LABELS.get(b, b), report_charts.BACKEND_COLORS.get(b, _MUTED), usage["daily"].get(b, [0.0] * 7))
            for b in backends
        ]
        parts.append(_chart(
            images, problems, "tokens_by_day", "Tokens per day, stacked by backend",
            lambda: report_charts.stacked_columns(labels, series),
        ))
        ordered = model_catalog.sort_usage_rows(usage["models"])
        parts.append(_note("Tokens per model (input + output), in the order the model lists use."))
        parts.append(_table(
            [("Model", "left"), ("Backend", "left"), ("Requests", "right"), ("Input", "right"), ("Output", "right"),
             ("Total", "right"), ("Share", "right"), ("Cost", "right")],
            [
                [_esc(m["model"]), _esc(BACKEND_LABELS.get(m["backend"], m["backend"])), _int(m["requests"]),
                 _int(m["input_tokens"]), _int(m["output_tokens"]), _int(m["tokens"]),
                 _pct(_share(m["tokens"], all_tokens)), _usd(m["cost"])]
                for m in ordered[: TABLE_MODELS * 2]
            ],
        ))

    # 2. Latency and 3. error rate, both from Langfuse.
    parts.append(_heading("Latency (p50 / p95) by model"))
    if observed is None:
        parts.append(_unavailable("Latency and error rate", error or "no data"))
    elif not week:
        parts.append(f'<p style="font-size:13px;color:{_INK2}">Langfuse holds no requests for this week.</p>')
    else:
        parts.append(_note(
            "Whole-request latency of the requests that succeeded, per local day: p50 is the typical request, "
            "p95 the slow tail. Each panel has its own scale. TTFT is time to the first output (streams only)."
        ))
        known = {b: [m for m in week if backend_of.get(m, "vllm") == b] for b in _backend_order({backend_of.get(m, "vllm") for m in week})}
        for backend, names in known.items():
            label = BACKEND_LABELS.get(backend, backend)
            busiest = [m for m in sorted(names, key=lambda m: -week[m]["ok"]) if week[m]["ok"] > 0][:PANEL_MODELS]
            parts.append(f'<p style="font-size:14px;font-weight:600;margin:16px 0 2px">{_esc(label)}</p>')
            if busiest:
                panels = [
                    (model, [
                        ("p50", report_charts.BLUE, [(day.get(model) or {}).get("latency_p50") for day in days]),
                        ("p95", report_charts.ORANGE, [(day.get(model) or {}).get("latency_p95") for day in days]),
                    ])
                    for model in busiest
                ]
                parts.append(_chart(
                    images, problems, f"latency_{backend}", f"Daily p50 and p95 latency per model, {label}",
                    lambda panels=panels: report_charts.line_panels(labels, panels, _ms_tick),
                ))
            ordered = model_catalog.sort_usage_rows(
                [{"model": m, "backend": backend, "type": type_of.get(m, "llm")} for m in names]
            )[:TABLE_MODELS]
            rows = []
            for item in ordered:
                model = item["model"]
                entry, then = week[model], last.get(model) or {}
                first = (ttft or {}).get(model) or {}
                rate = _error_rate(entry)
                rate_html = _pct(rate)
                if rate is not None and rate >= ERROR_RATE_ALERT and entry["ok"] + entry["errors"] >= ALERT_MIN_REQUESTS:
                    rate_html = f'<b style="color:{_BAD}">⚠ {rate_html}</b>'
                rows.append([
                    _esc(model), _int(entry["ok"] + entry["errors"]), _ms(entry.get("latency_p50")),
                    f"<b>{_ms(entry.get('latency_p95'))}</b>",
                    _delta(entry.get("latency_p95"), then.get("latency_p95"), worse="up"),
                    _ms(first.get("timeToFirstToken_p50")), _ms(first.get("timeToFirstToken_p95")),
                    _int(entry["errors"]), rate_html,
                ])
            parts.append(_table(
                [("Model", "left"), ("Requests", "right"), ("p50", "right"), ("p95", "right"), ("p95 vs last week", "right"),
                 ("TTFT p50", "right"), ("TTFT p95", "right"), ("Errors", "right"), ("Error rate", "right")],
                rows,
            ))

        parts.append(_heading("Error rate"))
        parts.append(_note(
            "Failed requests (connection failure or an error status from the model server) over all requests. "
            "Requests refused by the guardrail are left out of both."
        ))
        rows = []
        series = []
        for backend, names in known.items():
            ok, errors = _sum((week[m] for m in names), "ok"), _sum((week[m] for m in names), "errors")
            then = [last[m] for m in last if backend_of.get(m, "vllm") == backend]
            before_rate = _share(_sum(then, "errors"), _sum(then, "ok") + _sum(then, "errors"))
            color = report_charts.BACKEND_COLORS.get(backend, _MUTED)
            rows.append([
                _swatch(color) + _esc(BACKEND_LABELS.get(backend, backend)), _int(ok + errors), _int(errors),
                f"<b>{_pct(_share(errors, ok + errors))}</b>", _pct(before_rate),
                _delta(_share(errors, ok + errors), before_rate, worse="up"),
            ])
            per_day: list[float | None] = []
            for day in days:
                entries = [day[m] for m in day if backend_of.get(m, "vllm") == backend]
                rate = _share(_sum(entries, "errors"), _sum(entries, "ok") + _sum(entries, "errors"))
                per_day.append(None if rate is None else rate * 100)
            series.append((BACKEND_LABELS.get(backend, backend), color, per_day))
        rows.append([
            "<b>All</b>", _int(_sum(week.values(), "ok") + _sum(week.values(), "errors")), _int(_sum(week.values(), "errors")),
            f"<b>{_pct(overall)}</b>", _pct(overall_before), _delta(overall, overall_before, worse="up"),
        ])
        parts.append(_table(
            [("Backend", "left"), ("Requests", "right"), ("Errors", "right"), ("Error rate", "right"),
             ("Last week", "right"), ("Change", "right")],
            rows,
        ))
        parts.append(_chart(
            images, problems, "error_rate", "Error rate per day, by backend",
            lambda: report_charts.lines(labels, series, _pct_tick),
        ))

    notes = [
        "Tokens, requests and cost come from the gateway's usage record and are complete. Latency and error rate "
        "come from Langfuse, where a request is dated by the moment it finished.",
    ]
    sampling = _sampling_note() if observed is not None else None
    if sampling:
        notes.append(_esc(sampling))
    subject = f"[{APP_TITLE}] Weekly usage report, {period.label}"
    if notable:
        subject += f" · ⚠ {len(notable)} to look at"
    return Rendered(subject, _page("Weekly usage report", period, parts, notes), images, problems)


# ── Guardrail report: figures ──

def _score_counts(client, start: date, end: date) -> Counter:
    """``(score name, value) -> count`` of the guardrail scores over local
    days ``[start, end)``."""
    rows = langfuse_reader.metrics(client, {
        "view": "scores-categorical",
        "dimensions": [{"field": "name"}, {"field": "stringValue"}],
        "metrics": [_COUNT],
        "filters": [],
        "fromTimestamp": langfuse_reader.iso(local_day_start_utc_of(start)),
        "toTimestamp": langfuse_reader.iso(local_day_start_utc_of(end)),
    })
    counts: Counter = Counter()
    for row in rows:
        name = str(row.get("name") or "")
        if name in ("guardrail", "guardrail_action", "guardrail_category"):
            counts[(name, str(row.get("stringValue") or ""))] += langfuse_reader.count(row)
    return counts


def _flagged_accounts(client, start: date, end: date, statuses: tuple[str, ...], max_pages: int) -> tuple[dict[str, dict], bool]:
    """Per account (the trace's user): how many of its requests were judged
    ``unsafe`` / ``controversial``, how many were refused, and the categories
    named. ``(accounts, truncated)``."""
    accounts: dict[str, dict] = {}
    truncated = False
    for status in statuses:
        items, cut = langfuse_reader.scores(
            client, name="guardrail_action", trace_tags=[f"guardrail:{status}"],
            start=local_day_start_utc_of(start), end=local_day_start_utc_of(end), max_pages=max_pages,
        )
        truncated = truncated or cut
        for item in items:
            trace = item.get("trace") if isinstance(item.get("trace"), dict) else {}
            name = str(trace.get("userId") or "(unknown)")
            entry = accounts.setdefault(name, {"unsafe": 0, "controversial": 0, "blocked": 0, "categories": Counter()})
            entry[status] += 1
            if str(item.get("stringValue") or "") == "block":
                entry["blocked"] += 1
            for tag in trace.get("tags") or []:
                if isinstance(tag, str) and tag.startswith("guardrail_category:"):
                    entry["categories"][tag.partition(":")[2]] += 1
    return accounts, truncated


def guardrail_figures(period: Period) -> dict[str, Any]:
    with langfuse_reader.new_client() as client:
        days = [_score_counts(client, day, day + timedelta(days=1)) for day in period.days]
        previous = _score_counts(client, period.previous.start, period.previous.end)
        accounts: dict[str, dict] | None = None
        before: dict[str, dict] = {}
        truncated, accounts_error = False, ""
        try:
            accounts, truncated = _flagged_accounts(
                client, period.start, period.end, ("unsafe", "controversial"), FLAGGED_MAX_PAGES,
            )
            before, _cut = _flagged_accounts(
                client, period.previous.start, period.previous.end, ("unsafe",), FLAGGED_MAX_PAGES // 2,
            )
        except LangfuseError as exc:
            accounts_error = str(exc)
            logger.warning("Guardrail report: accounts not read | error={}", accounts_error)
    week: Counter = Counter()
    for counts in days:
        week.update(counts)
    return {
        "week": week, "days": days, "previous": previous, "accounts": accounts, "accounts_before": before,
        "accounts_truncated": truncated, "accounts_error": accounts_error,
    }


def account_reasons(entry: dict, local_requests: int | None) -> list[str]:
    """Why an account is pointed out (empty: it is not)."""
    reasons = []
    if entry["unsafe"] >= USER_UNSAFE_MIN:
        reasons.append(f"{entry['unsafe']} unsafe verdicts")
    if entry["blocked"] >= USER_BLOCKED_MIN:
        reasons.append(f"{entry['blocked']} requests refused")
    jailbreak = entry["categories"].get("Jailbreak", 0)
    if jailbreak >= USER_JAILBREAK_MIN:
        reasons.append(f"{jailbreak} jailbreak attempts")
    if (
        local_requests and entry["unsafe"] >= USER_SHARE_UNSAFE_MIN
        and entry["unsafe"] / local_requests >= USER_SHARE_MIN
    ):
        reasons.append(f"{entry['unsafe'] / local_requests:.0%} of its local requests judged unsafe")
    return reasons


def _local_requests(session: Session, period: Period, names: list[str]) -> tuple[dict[str, int], set[str]]:
    """``username -> local (vLLM) requests this week`` for the accounts that
    still exist, and which of them are service accounts."""
    if not names:
        return {}, set()
    rows = session.exec(
        select(User.id, User.username, User.is_service_account).where(User.username.in_(names))  # type: ignore[attr-defined]
    ).all()
    ids = {user_id: username for user_id, username, _service in rows}
    usage = aggregate(session, period.start, period.end, ("user_id", "backend"), user_ids=list(ids))
    requests = {username: 0 for username in ids.values()}
    for (user_id, backend), row in usage.items():
        if backend == "vllm" and user_id in ids:
            requests[ids[user_id]] += int(row["requests"])
    return requests, {username for _id, username, service in rows if service}


# ── Guardrail report: the mail ──

def guardrail_report(session: Session, period: Period) -> Rendered:
    figures: dict[str, Any] | None = None
    error = ""
    if not langfuse_reader.is_configured():
        error = "Langfuse is not configured."
    else:
        try:
            figures = guardrail_figures(period)
        except LangfuseError as exc:
            error = str(exc)
            logger.warning("Guardrail report: Langfuse read failed | error={}", error)
    requests: dict[str, int] = {}
    service: set[str] = set()
    if figures and figures["accounts"]:
        requests, service = _local_requests(session, period, list(figures["accounts"]))
        session.commit()
    return render_guardrail(period, figures, requests, service, error)


def render_guardrail(
    period: Period, figures: dict[str, Any] | None, requests: dict[str, int], service: set[str], error: str = "",
) -> Rendered:
    images: dict[str, bytes] = {}
    problems: list[str] = [f"langfuse: {error}"] if error else []
    parts: list[str] = []
    subject = f"[{APP_TITLE}] Guardrail weekly report, {period.label}"
    settings = config.get_guardrail_settings()
    policy = (
        f"Guardrail as configured now: <b>{'on' if settings.get('active') else 'off'}</b>, action "
        f"<b>{_esc(settings.get('action') or 'monitor')}</b>, threshold <b>{_esc(settings.get('block_threshold') or 'unsafe')}</b>. "
        "It screens requests to local (vLLM) models only."
    )
    parts.append(f'<p style="font-size:13px;color:{_INK2};margin:10px 0 0">{policy}</p>')
    if figures is None:
        parts.append(_unavailable("The guardrail verdicts", error or "no data"))
        return Rendered(subject, _page("Guardrail weekly report", period, parts, []), images, problems)

    week, before = figures["week"], figures["previous"]

    def verdicts(counts: Counter, statuses: tuple[str, ...]) -> int:
        return sum(counts[("guardrail", status)] for status in statuses)

    judged, missed = verdicts(week, JUDGED), verdicts(week, NOT_JUDGED)
    unsafe, controversial = week[("guardrail", "unsafe")], week[("guardrail", "controversial")]
    unsafe_before, controversial_before = before[("guardrail", "unsafe")], before[("guardrail", "controversial")]
    blocked = week[("guardrail_action", "block")]
    screened = judged + missed

    accounts = figures["accounts"] or {}
    accounts_before = figures["accounts_before"] or {}
    pointed = {name: account_reasons(entry, requests.get(name)) for name, entry in accounts.items()}
    pointed = {name: reasons for name, reasons in pointed.items() if reasons}

    categories = {value: number for (name, value), number in week.items() if name == "guardrail_category" and number}
    categories_before = {value: number for (name, value), number in before.items() if name == "guardrail_category"}

    alerts: list[str] = []
    if not settings.get("active"):
        alerts.append("The guardrail is <b>off</b> now: nothing is being screened.")
    if settings.get("active") and not screened:
        alerts.append(
            "No verdict was recorded this week although the guardrail is on: check the guard server "
            "(Admin &rarr; Security &rarr; Guardrail &rarr; Test connection) and that Langfuse is receiving data."
        )
    if screened and missed / screened >= NOT_JUDGED_ALERT:
        detail = ", ".join(
            f"{_int(week[('guardrail', status)])} {status.replace('_', ' ')}" for status in NOT_JUDGED if week[("guardrail", status)]
        )
        alerts.append(
            f"The guard did not judge <b>{_pct(missed / screened)}</b> of the requests it was given ({_esc(detail)}). "
            "The guardrail fails open: those requests went through unscreened."
        )
    if unsafe >= unsafe_before * UNSAFE_RISE_RATIO and unsafe - unsafe_before >= UNSAFE_RISE_MIN:
        alerts.append(f"Unsafe verdicts rose to <b>{_int(unsafe)}</b>, from {_int(unsafe_before)} last week.")
    fresh = [
        f"{_esc(name)} ({_int(number)})" for name, number in sorted(categories.items(), key=lambda kv: -kv[1])
        if number >= NEW_CATEGORY_MIN and not categories_before.get(name)
    ]
    if fresh:
        alerts.append(f"Categories that did not appear last week: <b>{', '.join(fresh)}</b>.")
    if pointed:
        names = ", ".join(_esc(name) for name in sorted(pointed, key=lambda n: -accounts[n]["unsafe"])[:8])
        more = f" and {len(pointed) - 8} more" if len(pointed) > 8 else ""
        alerts.append(
            f"<b>{len(pointed)} account{'s' if len(pointed) != 1 else ''}</b> to look at: {names}{more} (details below)."
        )
    if alerts:
        parts.append(_callout(alerts, f"⚠ {len(alerts)} thing{'s' if len(alerts) != 1 else ''} to look at", "warn"))
        subject += f" · ⚠ {len(alerts)} to look at"
    else:
        parts.append(_callout([], "✓ Nothing stood out this week", "ok"))

    parts.append(_tiles([
        ("Screened", report_charts.compact(screened), _delta(screened, verdicts(before, JUDGED + NOT_JUDGED)) + " vs last week"),
        ("Unsafe", _int(unsafe), _delta(unsafe, unsafe_before, worse="up") + " vs last week"),
        ("Controversial", _int(controversial), _delta(controversial, controversial_before, worse="up") + " vs last week"),
        ("Refused", _int(blocked), "action block" if blocked else "none"),
        ("Not judged", _pct(_share(missed, screened)), "went through unscreened" if missed else "none"),
    ]))

    # 1. Verdicts.
    parts.append(_heading("Verdicts"))
    rows = []
    for status in (*JUDGED, *NOT_JUDGED, "skipped"):
        number, then = week[("guardrail", status)], before[("guardrail", status)]
        if not number and not then:
            continue
        mark = {"unsafe": _swatch(report_charts.CRITICAL), "controversial": _swatch(report_charts.WARNING)}.get(status, "")
        rows.append([
            mark + _esc(status.replace("_", " ")), _int(number),
            _pct(_share(number, screened)) if status != "skipped" else "–", _int(then),
            _delta(number, then, worse=None if status in ("safe", "skipped") else "up"),
        ])
    if rows:
        parts.append(_table(
            [("Verdict", "left"), ("Requests", "right"), ("Share of screened", "right"), ("Last week", "right"), ("Change", "right")],
            rows,
        ))
        parts.append(_note(
            "<i>timeout</i>, <i>error</i>, <i>parse error</i> and <i>pending</i> are requests the guard did not judge. "
            "<i>skipped</i> had no new user text to screen."
        ))
    else:
        parts.append(f'<p style="font-size:13px;color:{_INK2}">No verdicts were recorded this week.</p>')
    if unsafe or controversial:
        series = [
            ("Unsafe", report_charts.CRITICAL, [float(day[("guardrail", "unsafe")]) for day in figures["days"]]),
            ("Controversial", report_charts.WARNING, [float(day[("guardrail", "controversial")]) for day in figures["days"]]),
        ]
        parts.append(_chart(
            images, problems, "flagged_by_day", "Unsafe and controversial verdicts per day",
            lambda: report_charts.stacked_columns(list(WEEKDAYS), series),
        ))
    actions = [(value, week[("guardrail_action", value)]) for value in ("pass", "flag", "block")]
    if any(number for _value, number in actions):
        parts.append(_note(
            "What the policy did with them: "
            + ", ".join(f"<b>{_int(number)}</b> {value}" for value, number in actions)
            + ". <i>flag</i> would have been refused under action <i>block</i>."
        ))

    # 2. Categories.
    parts.append(_heading("Categories"))
    if categories:
        most = max(categories.values())
        parts.append(_table(
            [("Category", "left"), ("Verdicts", "right"), ("", "left"), ("Last week", "right"), ("Change", "right")],
            [
                [_esc(name), _int(number), _bar(number / most, report_charts.BLUE), _int(categories_before.get(name, 0)),
                 _delta(number, categories_before.get(name, 0), worse="up")]
                for name, number in sorted(categories.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
        ))
        parts.append(_note("A verdict can name more than one category, so these add up to more than the flagged requests."))
    else:
        parts.append(f'<p style="font-size:13px;color:{_INK2}">No category was reported this week.</p>')

    # 3. Accounts.
    parts.append(_heading("Accounts"))
    if figures["accounts_error"]:
        parts.append(_unavailable("The accounts behind the verdicts", figures["accounts_error"]))
        problems.append(f"langfuse accounts: {figures['accounts_error']}")
    elif not accounts:
        parts.append(f'<p style="font-size:13px;color:{_INK2}">No request was judged unsafe or controversial.</p>')
    else:
        def account_row(name: str, reasons: list[str] | None) -> list[str]:
            entry = accounts[name]
            local = requests.get(name)
            top = ", ".join(f"{_esc(c)} ({n})" for c, n in entry["categories"].most_common(3)) or "–"
            then = (accounts_before.get(name) or {}).get("unsafe", 0)
            label = f"<b>⚠ {_esc(name)}</b>" if reasons else _esc(name)
            if name in service:
                label += f' <span style="color:{_MUTED}">(service account)</span>'
            elif local is None and name != "(unknown)":
                label += f' <span style="color:{_MUTED}">(no longer an account)</span>'
            if reasons:
                # Why it is pointed out, under the name: a column of its own
                # makes the table wider than a mail window.
                label += f'<div style="font-size:12px;color:{_INK2};white-space:normal">{_esc("; ".join(reasons))}</div>'
            return [
                label, f"<b>{_int(entry['unsafe'])}</b>", _int(entry["controversial"]), _int(entry["blocked"]),
                _int(local) if local is not None else "–",
                _pct(_share(entry["unsafe"], local)) if local else "–",
                _int(then) if then else f'<span style="color:{_MUTED}">–</span>', top,
            ]

        columns = [
            ("Account", "left"), ("Unsafe", "right"), ("Controv.", "right"), ("Refused", "right"),
            ("Requests", "right"), ("Share", "right"), ("Last week", "right"), ("Top categories", "left"),
        ]
        ranked = sorted(accounts, key=lambda n: (-accounts[n]["unsafe"], -accounts[n]["controversial"], n))
        if pointed:
            parts.append(f'<p style="font-size:14px;font-weight:600;margin:12px 0 2px">⚠ To look at ({len(pointed)})</p>')
            parts.append(_table(columns, [account_row(name, pointed[name]) for name in ranked if name in pointed]))
        others = [name for name in ranked if name not in pointed and accounts[name]["unsafe"]][:OTHER_ACCOUNTS]
        if others:
            parts.append('<p style="font-size:14px;font-weight:600;margin:12px 0 2px">Other accounts with unsafe verdicts</p>')
            parts.append(_table(columns, [account_row(name, None) for name in others]))
        quiet = len([n for n in accounts if n not in pointed]) - len(others)
        if quiet > 0:
            parts.append(_note(f"{quiet} more account{'s' if quiet != 1 else ''} had fewer or only controversial verdicts."))
        parts.append(_note(
            "<i>Requests</i> is the account's requests to local models this week, <i>Share</i> the unsafe part of "
            "them, <i>Last week</i> its unsafe verdicts the week before."
        ))
        parts.append(_note(
            f"An account is pointed out for {USER_UNSAFE_MIN}+ unsafe verdicts, {USER_BLOCKED_MIN}+ refused requests, "
            f"{USER_JAILBREAK_MIN}+ jailbreak attempts, or {USER_SHARE_MIN:.0%}+ of its local requests judged unsafe "
            f"(at least {USER_SHARE_UNSAFE_MIN})."
        ))
        if figures["accounts_truncated"]:
            parts.append(_note(
                f"Only the first {FLAGGED_MAX_PAGES * langfuse_reader.PAGE_SIZE:,} flagged requests per verdict were read: "
                "the counts per account are a lower bound this week."
            ))
        where = langfuse_reader.host()
        link = f'<a href="{_esc(where)}">Langfuse</a>' if where.startswith(("http://", "https://")) else "Langfuse"
        parts.append(_note(
            f"To see what was asked: in {link}, filter Tracing by the user and the tag <code>guardrail:unsafe</code> "
            "(prompts are there only when LANGFUSE_CAPTURE_IO is on)."
        ))

    notes = [
        "This mail holds counts only, never the screened text. The guard model (Qwen3Guard-Gen) makes mistakes in both "
        "directions: a count here is a reason to look, not a finding.",
        "All figures come from Langfuse, where a request is dated by the moment it finished. "
        "Under action <i>monitor</i> nothing is refused.",
    ]
    sampling = _sampling_note()
    if sampling:
        notes.append(_esc(sampling))
    return Rendered(subject, _page("Guardrail weekly report", period, parts, notes), images, problems)
