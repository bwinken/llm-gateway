#!/usr/bin/env python3
"""Roll closed local (Asia/Taipei) days of usage_logs into usage_daily, by hand.

The gateway does this itself: every worker runs ``usage_rollup.rollup_loop``
from the lifespan (``USAGE_ROLLUP_INTERVAL_S``, default 600 s), including
the first backfill after the migration. This script is the same code for
inspecting the watermark, catching up while the gateway is stopped, and
repairing days with --recompute. It is safe to run while the gateway is up:
the advisory lock keeps it and the workers from rolling the same day.

Usage:
    # Dry run (default): show the watermark and which days would be rolled
    uv run python scripts/rollup_usage_daily.py

    # Roll every settled day not rolled yet (what the hourly timer runs)
    uv run python scripts/rollup_usage_daily.py --execute

    # Re-roll already-rolled days from usage_logs (repair), inclusive range
    uv run python scripts/rollup_usage_daily.py --recompute 2026-10-01 2026-10-31 --execute

Exit status 0 when done, 2 if another run (or a gateway worker) holds the lock.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session  # noqa: E402

from app.core.database import engine  # noqa: E402
from app.services import usage_rollup  # noqa: E402


def _print_day(day: date, rows: int) -> None:
    print(f"  {day.isoformat()}: {rows:,} row(s)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Roll closed days of usage_logs into usage_daily.")
    parser.add_argument("--execute", action="store_true",
                        help="Write to usage_daily (default: dry-run, print only).")
    parser.add_argument("--recompute", nargs=2, metavar=("FROM", "TO"), type=date.fromisoformat,
                        help="Re-roll already-rolled days FROM..TO (YYYY-MM-DD, inclusive).")
    args = parser.parse_args()

    with Session(engine) as session:
        watermark = usage_rollup.rolled_through(session)
        print(f"[rollup] rolled through: {watermark.isoformat() if watermark else '(never run)'}")

        if args.recompute:
            first, last = args.recompute
            if first > last:
                parser.error("--recompute FROM must not be after TO")
            if not args.execute:
                print(f"[rollup] dry-run: would re-roll rolled days in {first}..{last}")
                return 0
            try:
                done, skipped = usage_rollup.recompute(session, first, last, on_day=_print_day)
            except usage_rollup.RollupBusy as exc:
                print(f"[rollup] {exc}; try again later")
                return 2
            print(f"[rollup] recomputed {len(done)} day(s)")
            if skipped:
                print(f"[rollup] skipped {len(skipped)} day(s) older than the oldest usage_logs row "
                      f"({skipped[0]}..{skipped[-1]}): their raw rows are gone, the rollup is kept")
            return 0

        pending = usage_rollup.pending_days(session)
        session.rollback()
        if not pending:
            print("[rollup] nothing to roll")
            return 0
        print(f"[rollup] {len(pending)} day(s) pending: {pending[0]}..{pending[-1]}")
        if not args.execute:
            print("[rollup] dry-run — nothing written (use --execute to roll)")
            return 0
        try:
            done = usage_rollup.advance(session, on_day=_print_day)
        except usage_rollup.RollupBusy as exc:
            print(f"[rollup] {exc}; try again later")
            return 2
        print(f"[rollup] rolled {len(done)} day(s); now through {done[-1] if done else watermark}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
