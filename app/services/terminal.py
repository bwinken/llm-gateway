"""Plain-text views of the account endpoints, for a terminal.

``GET /v1/me``, ``/v1/status`` and ``/v1/usage`` answer in JSON; with
``?format=text`` (or ``Accept: text/plain``) they answer with what these
functions make of that same JSON, so a person can read it straight from
``curl`` or the ``gw`` script (``setup/gw``). Rendering here rather than in
the script means a change of wording or layout needs no update on anyone's
machine.

Every function is pure: a dict in, a string out. Colour (ANSI) and the
block characters are opt-in / opt-out per request (``Style``), because the
server cannot see whether the other end is a terminal, a pipe or a file.

Anything that is not ours (account names from SSO, model aliases and display
names from the config) goes through ``_clean`` first: a control character in
one of them would otherwise be an escape sequence in the reader's terminal.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any

BAR_WIDTH = 20
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# The names people know the backends by (the JSON says onprem / azure / aws).
_BACKEND_LABELS = {"onprem": "on-prem", "azure": "Azure", "aws": "AWS"}


@dataclass(frozen=True)
class Style:
    """How to draw: ``color`` adds ANSI colours, ``ascii`` keeps to ASCII
    (for a terminal whose locale is not UTF-8)."""

    color: bool = False
    ascii: bool = False

    def paint(self, text: str, *codes: str) -> str:
        if not self.color or not codes or not text:
            return text
        return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"

    def bold(self, text: str) -> str:
        return self.paint(text, "1")

    def dim(self, text: str) -> str:
        return self.paint(text, "2")

    @property
    def sep(self) -> str:
        return " - " if self.ascii else " · "


_LEVEL_CODES = {"ok": "32", "warn": "33", "crit": "31", "over": "1;31"}


def _clean(value: Any) -> str:
    """``value`` as text without control characters."""
    return _CONTROL.sub("", str(value))


def _width(text: str) -> int:
    """Columns ``text`` takes on screen: wide (CJK) characters take two,
    combining marks and colour codes none."""
    width = 0
    for ch in _ANSI.sub("", text):
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _left(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _right(text: str, width: int) -> str:
    return " " * max(0, width - _width(text)) + text


def _table(rows: list[list[str]], align: str, indent: str = "  ", gap: str = "  ") -> list[str]:
    """``rows`` as aligned lines. ``align`` has one letter per column:
    ``l`` or ``r``. Trailing blanks are dropped."""
    widths = [max(_width(row[i]) for row in rows) for i in range(len(align))]
    lines = []
    for row in rows:
        cells = [
            _right(cell, widths[i]) if align[i] == "r" else _left(cell, widths[i])
            for i, cell in enumerate(row)
        ]
        lines.append((indent + gap.join(cells)).rstrip())
    return lines


def usd(amount: float) -> str:
    """``$3.25``; amounts under a cent keep four decimals (``$0.0042``)."""
    amount = float(amount)
    if 0 < abs(amount) < 0.01:
        return f"${amount:.4f}"
    return f"${amount:,.2f}"


def tokens(count: int) -> str:
    """``950`` / ``120.0K`` / ``3.2M`` / ``1.5B``."""
    count = int(count)
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(count) >= limit:
            return f"{count / limit:.1f}{suffix}"
    return str(count)


def context_window(size: int) -> str:
    """``131072`` → ``128K``, ``200000`` → ``200K``, ``1000000`` → ``1M``."""
    size = int(size)
    base = 1024 if size % 1024 == 0 else 1000
    thousands = size / base
    if thousands >= base:
        return f"{thousands / base:.3g}M"
    return f"{thousands:.0f}K"


def duration(seconds: float) -> str:
    """``9h 28m`` / ``12m``."""
    minutes = max(int(seconds // 60), 0)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _share(used: float, limit: float) -> float:
    """Part of ``limit`` that is spent, 0..1; a $0 limit is fully spent."""
    if limit <= 0:
        return 1.0
    return min(1.0, max(0.0, used / limit))


def _level(share: float) -> str:
    """The dashboard's bands: ok (< 70 %), warn (< 90 %), crit, over."""
    if share >= 1:
        return "over"
    if share >= 0.9:
        return "crit"
    if share >= 0.7:
        return "warn"
    return "ok"


def _bar(share: float, style: Style, width: int = BAR_WIDTH, level: str | None = None) -> str:
    filled = min(width, max(0, round(share * width)))
    if share > 0 and filled == 0:
        filled = 1  # anything spent shows
    full, empty = ("#", "-") if style.ascii else ("█", "░")
    drawn = style.paint(full * filled, _LEVEL_CODES[level]) if level else full * filled
    return f"[{drawn}{style.dim(empty * (width - filled))}]"


def _budget_row(label: str, used: float, limit: float, remaining: float, style: Style) -> list[str]:
    share = _share(used, limit)
    level = _level(share)
    return [
        label,
        _bar(share, style, level=level),
        style.paint(f"{share * 100:.0f}%", _LEVEL_CODES[level]),
        f"{usd(used)} of {usd(limit)}",
        f"{usd(remaining)} left",
    ]


def _warn(text: str, style: Style) -> str:
    return style.paint(f"! {text}", _LEVEL_CODES["over"])


def render_me(data: dict[str, Any], style: Style = Style()) -> str:
    """``GET /v1/me``: who the key belongs to, today's budget and where
    today's spend went."""
    daily = data["daily"]
    cloud = data.get("cloud")
    who = [_clean(data["username"])]
    if data.get("display_name") and data["display_name"] != data["username"]:
        who.append(_clean(data["display_name"]))
    if data.get("is_admin"):
        who.append("admin")
    lines = [style.bold(who[0]) + "".join(style.sep + part for part in who[1:]), ""]

    lines.append(style.bold("Budget today"))
    rows = [_budget_row("total", daily["used_usd"], daily["limit_usd"], daily["remaining_usd"], style)]
    if cloud:
        rows.append(_budget_row("cloud", cloud["used_usd"], cloud["limit_usd"], cloud["remaining_usd"], style))
    lines += _table(rows, "llrrr")
    lines.append(style.dim(
        f"  resets in {duration(daily['resets_in_seconds'])} (midnight, {_clean(daily['timezone'])})"
    ))

    spent = daily["remaining_usd"] <= 0
    if not daily["enforced"]:
        lines.append(style.dim("  Limits are not enforced: going over is only logged."))
    elif spent:
        lines.append(_warn("Budget spent: requests are refused (429) until the reset.", style))
    elif cloud and cloud["remaining_usd"] <= 0:
        lines.append(_warn(
            "Cloud budget spent: until the reset, cloud models are "
            + ("answered by on-prem models." if data.get("cloud_budget_fallback") else "refused (429)."),
            style,
        ))

    lines += ["", style.bold("Spent today")]
    lines += _table(
        [
            [_BACKEND_LABELS.get(name, _clean(name)), usd(entry["used_usd"]) if entry["access"] else "no access"]
            for name, entry in data["backends"].items()
        ],
        "lr",
    )
    lines.append(style.dim(
        f"  {daily['requests']:,} requests, {tokens(daily['input_tokens'])} tokens in, "
        f"{tokens(daily['output_tokens'])} out"
    ))

    concurrency = data.get("concurrency") or {}
    if concurrency.get("mode") == "enforce":
        lines += ["", f"Up to {concurrency['limit']} requests at a time."]
    elif concurrency.get("mode") == "monitor":
        lines += ["", f"More than {concurrency['limit']} requests at a time are logged (not refused)."]
    return "\n".join(lines) + "\n"


def render_status(data: dict[str, Any], style: Style = Style()) -> str:
    """``GET /v1/status``: the models the account can use and whether the
    on-prem ones are up."""
    onprem = data["onprem"]
    headline = f"{onprem['up']} of {onprem['total']} on-prem models up"
    if onprem["down"]:
        headline = style.paint(headline, _LEVEL_CODES["crit"])
    lines = [style.bold("Models") + style.sep + headline]

    up_mark, down_mark, cloud_mark = ("*", "x", "-") if style.ascii else ("●", "○", "·")
    rows = []
    for model in data["models"]:
        state = model["state"]
        if state == "up":
            mark, shown = style.paint(up_mark, _LEVEL_CODES["ok"]), "up"
        elif state == "down":
            mark, shown = style.paint(down_mark, _LEVEL_CODES["crit"]), style.paint("DOWN", _LEVEL_CODES["over"])
        else:
            mark, shown = style.dim(cloud_mark), ""
        load = ""
        if state == "up" and "running" in model:
            load = f"{model['running']} running, {model.get('waiting', 0)} waiting"
        name = _clean(model["id"])
        if model.get("display_name") and model["display_name"] != model["id"]:
            name += style.dim(f" ({_clean(model['display_name'])})")
        rows.append([
            mark,
            name,
            _clean(model["type"]),
            shown,
            load,
            f"{context_window(model['context_window'])} ctx" if model.get("context_window") else "",
            f"{usd(model['input_price_per_1m'])} / {usd(model['output_price_per_1m'])}",
        ])
    # One table for every backend, so the columns line up down the page.
    table = _table(rows, "lllllrr") if rows else []
    backend = None
    for model, line in zip(data["models"], table):
        if model["backend"] != backend:
            backend = model["backend"]
            lines += ["", style.bold(_BACKEND_LABELS.get(backend, _clean(backend)))]
        lines.append(line)

    lines += ["", style.dim("Prices are USD per 1M tokens, input / output.")]
    if onprem["down"]:
        lines.append(style.dim(
            "A request for a model that is down is answered by another model of its type, when one is up."
        ))
    if any(model["state"] == "unchecked" for model in data["models"]):
        lines.append(style.dim("Cloud models are not health-checked."))
    return "\n".join(lines) + "\n"


def render_usage(data: dict[str, Any], style: Style = Style()) -> str:
    """``GET /v1/usage``: one line per day, then this month by model."""
    days = data["days"]
    count = len(days)
    title = "Usage" + style.sep + ("today" if count == 1 else f"last {count} days")
    lines = [style.bold(title) + style.dim(f" ({_clean(data['timezone'])})"), ""]

    top = max((day["cost_usd"] for day in days), default=0.0)
    full = "#" if style.ascii else "█"
    rows = [["day", "requests", "tokens in", "tokens out", "cost", ""]]
    for index, day in enumerate(days):
        when = date.fromisoformat(day["date"])
        label = when.strftime("%a %m-%d") + (" *" if index == count - 1 else "")
        bar = full * max(1 if day["cost_usd"] > 0 else 0, round(12 * day["cost_usd"] / top)) if top > 0 else ""
        rows.append([
            label, f"{day['requests']:,}", tokens(day["input_tokens"]), tokens(day["output_tokens"]),
            usd(day["cost_usd"]), style.paint(bar, _LEVEL_CODES["ok"]),
        ])
    if count > 1:
        rows.append([
            "total",
            f"{sum(day['requests'] for day in days):,}",
            tokens(sum(day["input_tokens"] for day in days)),
            tokens(sum(day["output_tokens"] for day in days)),
            usd(sum(day["cost_usd"] for day in days)),
            "",
        ])
    table = _table(rows, "lrrrrl")
    table[0] = style.dim(table[0])
    lines += table
    lines.append(style.dim("  * today so far"))

    month = data["month"]
    lines += [
        "",
        style.bold(f"This month ({_clean(month['month'])})") + style.sep
        + f"{usd(month['cost_usd'])}, {month['requests']:,} requests, "
        f"{tokens(month['input_tokens'])} tokens in, {tokens(month['output_tokens'])} out",
    ]
    if month["models"]:
        rows = [["model", "backend", "requests", "cost", "share"]]
        for model in month["models"]:
            share = model["cost_usd"] / month["cost_usd"] * 100 if month["cost_usd"] > 0 else 0.0
            rows.append([
                _clean(model["model"]),
                _BACKEND_LABELS.get(model["backend"], _clean(model["backend"])),
                f"{model['requests']:,}",
                usd(model["cost_usd"]),
                f"{share:.0f}%",
            ])
        table = _table(rows, "llrrr")
        table[0] = style.dim(table[0])
        lines += table
    return "\n".join(lines) + "\n"
