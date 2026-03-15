#!/usr/bin/env python3
"""Delete usage_logs older than a retention period (default: 1 year).

Usage:
    # Dry run (default) — show how many rows would be deleted
    uv run python scripts/cleanup_usage_logs.py

    # Actually delete
    uv run python scripts/cleanup_usage_logs.py --execute

    # Custom retention (e.g. 6 months)
    uv run python scripts/cleanup_usage_logs.py --days 180 --execute
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, func, select

from app.core.database import engine
from app.models.schema import UsageLog


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean up old usage_logs.")
    parser.add_argument(
        "--days",
        type=int,
        default=365,
        help="Retention period in days (default: 365)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete rows. Without this flag, only shows count (dry run).",
    )
    args = parser.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    with Session(engine) as session:
        count_stmt = (
            select(func.count())
            .select_from(UsageLog)
            .where(UsageLog.created_at < cutoff)
        )
        row_count = session.exec(count_stmt).one()

        print(f"Retention: {args.days} days (cutoff: {cutoff.date()})")
        print(f"Rows older than cutoff: {row_count:,}")

        if row_count == 0:
            print("Nothing to clean up.")
            return

        if not args.execute:
            print("Dry run — pass --execute to delete.")
            return

        # Delete in batches to avoid long-running transactions
        batch_size = 10_000
        total_deleted = 0
        while True:
            # Subquery to find IDs for this batch
            id_stmt = (
                select(UsageLog.id)
                .where(UsageLog.created_at < cutoff)
                .limit(batch_size)
            )
            ids = session.exec(id_stmt).all()
            if not ids:
                break

            from sqlalchemy import delete
            del_stmt = delete(UsageLog).where(UsageLog.id.in_(ids))
            result = session.exec(del_stmt)
            session.commit()
            total_deleted += result.rowcount
            print(f"  Deleted batch: {result.rowcount:,} (total: {total_deleted:,})")

        print(f"Done. Total deleted: {total_deleted:,}")


if __name__ == "__main__":
    main()
