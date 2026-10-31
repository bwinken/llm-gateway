#!/usr/bin/env python3
"""Preview or run usage_logs retention by hand.

The gateway runs retention itself once a day when USAGE_LOG_RETENTION_DAYS is
set (app/services/usage_retention.py); it is off by default, because
usage_logs is the billing and audit record. Both paths share the guards: a
day is deleted only once it is in usage_daily (reports keep it), and only
whole local days go.

Usage:
    # Dry run (default) — show the cutoff and how many rows would be deleted
    uv run python scripts/cleanup_usage_logs.py

    # Actually delete
    uv run python scripts/cleanup_usage_logs.py --execute

    # Custom retention (e.g. 6 months)
    uv run python scripts/cleanup_usage_logs.py --days 180 --execute
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session  # noqa: E402

from app.core.database import engine  # noqa: E402
from app.services.usage_retention import count_before, purge_before, purge_cutoff  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean up old usage_logs.")
    parser.add_argument("--days", type=int, default=365,
                        help="Retention period in days (default: 365)")
    parser.add_argument("--execute", action="store_true",
                        help="Actually delete rows. Without this flag, only shows count (dry run).")
    args = parser.parse_args()
    if args.days <= 0:
        parser.error("--days must be at least 1")

    with Session(engine) as session:
        cutoff = purge_cutoff(session, args.days)
        if cutoff is None:
            print("The usage_daily rollup has not run yet: nothing can be deleted without "
                  "losing it from reports. Start the gateway (it rolls up in the background) "
                  "or run scripts/rollup_usage_daily.py --execute first.")
            return
        row_count = count_before(session, cutoff)
        print(f"Retention: {args.days} days (cutoff: {cutoff.isoformat()}, capped at the rollup watermark)")
        print(f"Rows older than cutoff: {row_count:,}")
        if row_count == 0:
            print("Nothing to clean up.")
            return
        if not args.execute:
            print("Dry run — pass --execute to delete.")
            return
        total = purge_before(
            session, cutoff,
            on_batch=lambda n, t: print(f"  Deleted batch: {n:,} (total: {t:,})"),
        )
        print(f"Done. Total deleted: {total:,}")


if __name__ == "__main__":
    main()
