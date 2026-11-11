"""Wait until DATABASE_URL accepts connections, then exit 0 (1 on timeout).

Run before ``alembic upgrade head`` when the database may still be starting,
e.g. the compose ``migrate`` service on a fresh host where PostgreSQL and the
gateway come up together:

    python scripts/wait_for_db.py [--timeout 60]
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from sqlalchemy import create_engine, text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds to wait (default 60)")
    args = parser.parse_args()

    url = os.getenv("DATABASE_URL", "")
    if not url:
        print("wait_for_db: DATABASE_URL is not set", file=sys.stderr)
        return 1
    engine = create_engine(url, pool_pre_ping=False)
    deadline = time.monotonic() + args.timeout
    last = ""
    while True:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            print("wait_for_db: database is ready")
            return 0
        except Exception as exc:  # any connect failure means "not yet"
            last = type(exc).__name__  # never the message: it can carry the URL
        if time.monotonic() >= deadline:
            print(f"wait_for_db: database not ready after {args.timeout:.0f}s ({last})", file=sys.stderr)
            return 1
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
