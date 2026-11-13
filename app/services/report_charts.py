"""PNG charts for the weekly report mails.

A mail client (Outlook above all) renders neither SVG nor scripts, so a
chart reaches the reader as a PNG attached inline (``cid:``). matplotlib is
imported on first use, through its object API and the Agg canvas only (no
pyplot: its global state is not thread-safe and the reports render in a job
thread), so a broken install costs the charts, never the gateway or the
mail: ``weekly_reports`` catches whatever a chart raises and sends the
tables without it.

Every figure is 680 CSS px wide and drawn at twice that. The look is fixed
here once: thin marks, a 2 px surface gap between stacked segments, solid
hairline grid, one y axis, values in ink (never in the series colour), a
legend whenever there is more than one series. A chart never stands alone:
the mail prints the same numbers in a table beside it.
"""

from __future__ import annotations

import io
import math
import os
import tempfile
from collections.abc import Callable, Sequence

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"

# Categorical slots, in their fixed order (validated together on SURFACE).
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
# Colour follows the backend, wherever it appears.
BACKEND_COLORS = {"vllm": BLUE, "azure": ORANGE, "bedrock": AQUA}
# Status colours: only where the colour means a state.
CRITICAL, WARNING = "#d03b3b", "#fab219"

WIDTH_PX = 680
_SCALE = 2
_PX = 72.0 / 100.0  # one CSS px in points, at 100 px per inch

Series = tuple[str, str, Sequence[float | None]]  # (name, colour, one value per label)


def compact(number: float) -> str:
    """1,284 / 12.9K / 4.2M / 1.3B."""
    number = float(number)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(number) >= limit * 10 or (suffix != "K" and abs(number) >= limit):
            text = f"{number / limit:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return f"{number:,.0f}"


def _cache_dir() -> None:
    """Give matplotlib a cache directory it can write (its font list). Its
    default is under the home directory, which the container's user does not
    have: it would then make a new temporary one, and rebuild the font list,
    on every start."""
    if os.environ.get("MPLCONFIGDIR"):
        return
    uid = os.getuid() if hasattr(os, "getuid") else 0
    path = os.path.join(tempfile.gettempdir(), f"llm-gateway-matplotlib-{uid}")
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError:
        return  # matplotlib falls back by itself
    os.environ["MPLCONFIGDIR"] = path


def _figure(height_px: float):
    _cache_dir()
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    fig = Figure(figsize=(WIDTH_PX / 100.0, height_px / 100.0), dpi=100 * _SCALE, facecolor=SURFACE)
    FigureCanvasAgg(fig)
    return fig


def _png(fig) -> bytes:
    out = io.BytesIO()
    fig.savefig(out, format="png", facecolor=SURFACE)
    return out.getvalue()


def _style(ax, y_format: Callable[[float], str]) -> None:
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.spines["bottom"].set_linewidth(1 * _PX)
    ax.grid(axis="y", color=GRID, linewidth=1 * _PX, linestyle="-")
    ax.set_axisbelow(True)
    ax.tick_params(axis="both", length=0, labelsize=8, labelcolor=MUTED, pad=4)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=4, min_n_ticks=3))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _pos: y_format(v)))


def _legend(fig, entries: list[tuple[str, str]], line: bool) -> None:
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    handles = [
        Line2D([0], [0], color=color, linewidth=2 * _PX, solid_capstyle="round") if line
        else Patch(facecolor=color, edgecolor="none")
        for _name, color in entries
    ]
    fig.legend(
        handles, [name for name, _color in entries], loc="upper left", bbox_to_anchor=(0.0, 1.0),
        ncols=len(entries), frameon=False, fontsize=8, labelcolor=INK_SECONDARY,
        handlelength=1.2, handleheight=0.9, columnspacing=1.6, borderaxespad=0.6,
    )


def _top(values: Sequence[float | None]) -> float:
    numbers = [v for v in values if v is not None and not math.isnan(v)]
    return max(numbers) if numbers else 0.0


def stacked_columns(labels: Sequence[str], series: Sequence[Series], y_format: Callable[[float], str] = compact) -> bytes:
    """One column per label, the series stacked in the order given, each
    column's total on its cap."""
    from matplotlib.patches import FancyBboxPatch, Rectangle

    height_px = 250
    fig = _figure(height_px)
    ax = fig.add_axes((0.075, 0.12, 0.9, 0.72))
    _style(ax, y_format)
    count = len(labels)
    totals = [sum((s[2][i] or 0.0) for s in series) for i in range(count)]
    y_max = (max(totals) if totals and max(totals) > 0 else 1.0) * 1.14
    ax.set_xlim(-0.5, count - 0.5)
    ax.set_ylim(0, y_max)
    ax.set_xticks(range(count), labels)

    plot_w = WIDTH_PX * 0.9
    plot_h = height_px * 0.72
    width = min(24.0 / (plot_w / max(count, 1)), 0.6)  # a bar is at most 24 px thick
    px_y = y_max / plot_h          # one px, in data units, along y
    px_x = count / plot_w
    gap = 2 * px_y
    for i in range(count):
        base = 0.0
        present = [(color, (values[i] or 0.0)) for _name, color, values in series if (values[i] or 0.0) > 0]
        for position, (color, amount) in enumerate(present):
            last = position == len(present) - 1
            top = base + amount
            height = max(top - base - (0 if last else gap), px_y)
            if last and height > 8 * px_y:
                # The data end is rounded (4 px); the foot of the cap stays
                # square by covering the lower corners with a plain block.
                ax.add_patch(FancyBboxPatch(
                    (i - width / 2, base), width, height, linewidth=0, facecolor=color,
                    boxstyle=f"round,pad=0,rounding_size={4 * px_x}", mutation_aspect=px_y / px_x,
                ))
                ax.add_patch(Rectangle((i - width / 2, base), width, height / 2, linewidth=0, facecolor=color))
            else:
                ax.add_patch(Rectangle((i - width / 2, base), width, height, linewidth=0, facecolor=color))
            base = top
        if totals[i] > 0:
            ax.text(i, totals[i] + 5 * px_y, y_format(totals[i]), ha="center", va="bottom", fontsize=8, color=INK_SECONDARY)
    if len(series) > 1:
        _legend(fig, [(name, color) for name, color, _values in series], line=False)
    return _png(fig)


def _draw_lines(ax, count: int, series: Sequence[Series], y_format: Callable[[float], str], end_labels: bool) -> None:
    top = max((_top(values) for _name, _color, values in series), default=0.0)
    y_max = (top if top > 0 else 1.0) * 1.22
    ax.set_xlim(-0.35, count - 1 + (1.35 if end_labels else 0.35))
    ax.set_ylim(0, y_max)
    ends: list[tuple[int, float, str]] = []
    for _name, color, values in series:
        ys = [math.nan if v is None else float(v) for v in values]
        ax.plot(range(count), ys, color=color, linewidth=2 * _PX, solid_capstyle="round", solid_joinstyle="round", zorder=3)
        known = [i for i, y in enumerate(ys) if not math.isnan(y)]
        # A day with no neighbour draws no segment: give it a dot, and mark
        # where each line ends.
        alone = [i for i in known if (i - 1) not in known and (i + 1) not in known]
        marks = sorted(set(alone) | ({known[-1]} if known else set()))
        ax.plot(
            marks, [ys[i] for i in marks], linestyle="none", marker="o", markersize=8 * _PX, color=color,
            markeredgecolor=SURFACE, markeredgewidth=2 * _PX, zorder=4,
        )
        if known:
            ends.append((known[-1], ys[known[-1]], y_format(ys[known[-1]])))
    if end_labels and ends:
        # Selective direct labels: the last value of each line. Lines that end
        # together would print on top of each other, so labels closer than a
        # line of text are spread just enough to stay readable.
        ends.sort(key=lambda e: e[1])
        step = y_max * 0.11
        placed: list[float] = []
        for _x, y, _text in ends:
            placed.append(max(y, placed[-1] + step) if placed else y)
        for (x, _y, text), at in zip(ends, placed):
            ax.text(x + 0.18, at, text, ha="left", va="center", fontsize=7.5, color=INK_SECONDARY, zorder=5)


def lines(labels: Sequence[str], series: Sequence[Series], y_format: Callable[[float], str]) -> bytes:
    """One plot, one line per series over the labels (a missing value breaks
    the line), the last value of each printed at its end."""
    fig = _figure(230)
    ax = fig.add_axes((0.075, 0.13, 0.9, 0.7))
    _style(ax, y_format)
    ax.set_xticks(range(len(labels)), labels)
    _draw_lines(ax, len(labels), series, y_format, end_labels=True)
    if len(series) > 1:
        _legend(fig, [(name, color) for name, color, _values in series], line=True)
    return _png(fig)


def line_panels(
    labels: Sequence[str], panels: Sequence[tuple[str, Sequence[Series]]], y_format: Callable[[float], str],
    columns: int = 3,
) -> bytes:
    """Small multiples: one panel per ``(title, series)``, every panel the
    same series in the same colours (one legend for the figure), each on its
    own y scale starting at zero, since the panels' magnitudes differ by
    orders and what is read is each one's own course over the week."""
    count = len(panels)
    rows = max(1, math.ceil(count / columns))
    panel_h, head = 150, 30
    height_px = head + rows * panel_h
    fig = _figure(height_px)
    left, right, gutter = 0.065, 0.01, 0.06
    cell_w = (1 - left - right - gutter * (columns - 1)) / columns
    short = [label[:1] for label in labels] if len(labels) > 5 else list(labels)
    for index, (title, series) in enumerate(panels):
        row, column = divmod(index, columns)
        top_px = head + row * panel_h
        ax = fig.add_axes((
            left + column * (cell_w + gutter),
            1 - (top_px + panel_h - 26) / height_px,
            cell_w,
            (panel_h - 26 - 24) / height_px,
        ))
        _style(ax, y_format)
        ax.set_xticks(range(len(labels)), short)
        ax.tick_params(axis="both", labelsize=7)
        ax.set_title(title, loc="left", fontsize=8, color=INK, pad=5, fontweight="bold")
        _draw_lines(ax, len(labels), series, y_format, end_labels=True)
    if panels and len(panels[0][1]) > 1:
        _legend(fig, [(name, color) for name, color, _values in panels[0][1]], line=True)
    return _png(fig)
