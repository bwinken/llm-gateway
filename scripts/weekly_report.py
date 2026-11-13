#!/usr/bin/env python3
"""Build a weekly report by hand: look at it, or send it.

The gateway sends the reports itself on the schedule set in Admin ->
Settings -> Weekly reports (app/services/weekly_reports.py); no timer is
needed. This builds one now from live data (the gateway's usage record and
Langfuse) and writes it as an HTML page to open in a browser: the quickest
way to see what the Langfuse queries return on this installation. With
--send it mails the report instead, to mailboxes of the company domain
(only while Langfuse monitoring is on, like every send). The schedule is not
touched either way.

Usage:
    uv run python scripts/weekly_report.py usage                    # -> weekly-usage.html (last week)
    uv run python scripts/weekly_report.py guardrail --out /tmp/g.html
    uv run python scripts/weekly_report.py usage --week 2026-11-02  # the week containing that day
    uv run python scripts/weekly_report.py usage --send alice,bob   # mail it
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session  # noqa: E402

from app.core.database import engine  # noqa: E402
from app.services import weekly_reports  # noqa: E402
from app.services.report_content import week_of  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Build one weekly report now.")
    parser.add_argument("report", choices=sorted(weekly_reports.REPORTS))
    parser.add_argument("--week", type=date.fromisoformat, default=None,
                        help="Any day (YYYY-MM-DD) of the week to report (default: the last full week).")
    parser.add_argument("--out", default=None, help="Where to write the page (default: weekly-<report>.html).")
    parser.add_argument("--send", default=None, metavar="RECIPIENTS",
                        help="Mail it to these usernames / addresses (comma-separated) instead of writing a page.")
    args = parser.parse_args()
    period = week_of(args.week) if args.week else weekly_reports.current_period()

    with Session(engine) as session:
        if args.send:
            try:
                recipients = weekly_reports.parse_recipients(args.send)
            except ValueError as exc:
                sys.exit(f"[report] {exc}")
            ok, detail, _rendered = weekly_reports.send(
                session, args.report, recipients, period=period, kind="requested", actor="scripts/weekly_report.py",
            )
            print(f"[report] {'sent to' if ok else 'NOT sent to'} {', '.join(recipients)}" + (f" ({detail})" if detail else ""))
            sys.exit(0 if ok else 1)
        rendered = weekly_reports.render(session, args.report, period)

    out = Path(args.out or f"weekly-{args.report}.html")
    out.write_text(
        '<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:24px">'
        + weekly_reports.preview_html(rendered) + "</body></html>",
        encoding="utf-8",
    )
    print(f"[report] week of {period.label}")
    print(f"[report] subject: {rendered.subject}")
    for problem in rendered.problems:
        print(f"[report] problem: {problem}")
    print(f"[report] wrote {out}")


if __name__ == "__main__":
    main()
