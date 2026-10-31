#!/usr/bin/env python3
"""Run the usage-anomaly scan by hand.

The gateway runs it itself every hour (app/services/anomaly_scan.py,
``ANOMALY_SCAN_INTERVAL_S``); no timer is needed. This prints what the scan
finds right now, and persists it with --execute. Safe alongside the
gateway: findings upsert on (scope, rule, window_start).

Usage:
    uv run python scripts/scan_anomalies.py             # dry-run: print only
    uv run python scripts/scan_anomalies.py --execute   # persist to DB
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session  # noqa: E402

from app.core.database import engine  # noqa: E402
from app.services.anomaly_scan import detect_all, persist, prune_resolved  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan usage for anomalous usage patterns.")
    parser.add_argument("--execute", action="store_true",
                        help="Persist findings to anomaly_events (default: dry-run, print only).")
    args = parser.parse_args()

    with Session(engine) as session:
        findings = detect_all(session)
        for f in findings:
            print(f"[{f['severity']:>8}] {f['rule']:<16} {f['scope']:<16} "
                  f"observed={f['observed']} baseline={f['baseline']} ratio={f['ratio']}")
        print(f"[scan] {len(findings)} finding(s)")

        if args.execute:
            created, updated = persist(session, findings)
            pruned = prune_resolved(session)
            print(f"[scan] persisted: {created} new, {updated} updated; pruned {pruned} old resolved")
        else:
            print("[scan] dry-run — nothing written (use --execute to persist)")


if __name__ == "__main__":
    main()
