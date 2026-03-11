"""
Migrate data from SQLite to PostgreSQL.

Usage:
    # First migration (test run):
    python scripts/migrate_sqlite_to_pg.py <sqlite_db_path>

    # Preview only (no writes):
    python scripts/migrate_sqlite_to_pg.py <sqlite_db_path> --dry-run

    # Re-sync before go-live (only migrates new data since last run):
    python scripts/migrate_sqlite_to_pg.py <sqlite_db_path> --sync

Example:
    python scripts/migrate_sqlite_to_pg.py ./llm_gateway.db
    python scripts/migrate_sqlite_to_pg.py ./llm_gateway.db --dry-run
    python scripts/migrate_sqlite_to_pg.py ./llm_gateway.db --sync

Reads the PostgreSQL connection from DATABASE_URL in .env.
Migrates: users, usage_logs (preserving IDs and timestamps).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add project root to path so we can import app modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3

from dotenv import load_dotenv

load_dotenv()

from sqlmodel import Session, SQLModel, create_engine, select, func

from app.core.config import DATABASE_URL
from app.models.schema import UsageLog, User


def migrate(sqlite_path: str, *, dry_run: bool = False, sync: bool = False) -> None:
    if not Path(sqlite_path).exists():
        print(f"ERROR: SQLite file not found: {sqlite_path}")
        sys.exit(1)

    if "sqlite" in DATABASE_URL:
        print("ERROR: DATABASE_URL points to SQLite. Set it to PostgreSQL in .env first.")
        sys.exit(1)

    mode = "DRY-RUN" if dry_run else ("SYNC" if sync else "FULL")
    print(f"Mode: {mode}\n")

    # --- Source: SQLite ---
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row

    # --- Target: PostgreSQL ---
    pg_engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
    SQLModel.metadata.create_all(pg_engine)

    with Session(pg_engine) as session:
        # ---- Migrate users ----
        src_users = src.execute("SELECT * FROM users ORDER BY id").fetchall()
        migrated_users = 0
        skipped_users = 0
        updated_users = 0

        for row in src_users:
            existing = session.exec(
                select(User).where(User.username == row["username"])
            ).first()
            if existing:
                if sync:
                    # Update fields that may have changed
                    changed = False
                    if existing.api_key != row["api_key"]:
                        existing.api_key = row["api_key"]
                        changed = True
                    if existing.daily_limit_usd != row["daily_limit_usd"]:
                        existing.daily_limit_usd = row["daily_limit_usd"]
                        changed = True
                    if existing.is_admin != bool(row["is_admin"]):
                        existing.is_admin = bool(row["is_admin"])
                        changed = True
                    if changed:
                        if not dry_run:
                            session.add(existing)
                        updated_users += 1
                        print(f"  UPDATE user '{row['username']}' (id={existing.id})")
                    else:
                        skipped_users += 1
                        print(f"  SKIP   user '{row['username']}' (no changes)")
                else:
                    print(f"  SKIP   user '{row['username']}' (already exists, id={existing.id})")
                    skipped_users += 1
                continue

            if not dry_run:
                user = User(
                    username=row["username"],
                    password_hash=row["password_hash"] if "password_hash" in row.keys() else "",
                    api_key=row["api_key"],
                    daily_limit_usd=row["daily_limit_usd"],
                    is_admin=bool(row["is_admin"]),
                    created_at=row["created_at"],
                )
                session.add(user)
                session.flush()
                print(f"  OK     user '{user.username}' -> id={user.id}")
            else:
                print(f"  OK     user '{row['username']}' (dry-run)")
            migrated_users += 1

        if not dry_run:
            session.commit()

        summary = f"\nUsers: {migrated_users} migrated, {skipped_users} skipped"
        if sync:
            summary += f", {updated_users} updated"
        print(summary + "\n")

        # ---- Build old_id -> new_id mapping ----
        id_map: dict[int, int] = {}
        if not dry_run:
            for row in src_users:
                pg_user = session.exec(
                    select(User).where(User.username == row["username"])
                ).first()
                if pg_user:
                    id_map[row["id"]] = pg_user.id  # type: ignore[index]
        else:
            # In dry-run, assume 1:1 mapping for preview
            for row in src_users:
                id_map[row["id"]] = row["id"]

        # ---- Determine cutoff for incremental sync ----
        cutoff = None
        if sync:
            # Find the latest usage_log timestamp in PostgreSQL
            result = session.exec(
                select(func.max(UsageLog.created_at))
            ).first()
            if result:
                cutoff = result
                print(f"Sync mode: only migrating usage_logs after {cutoff}\n")
            else:
                print("Sync mode: PostgreSQL has no usage_logs, migrating all\n")

        # ---- Migrate usage_logs ----
        if cutoff:
            src_logs = src.execute(
                "SELECT * FROM usage_logs WHERE created_at > ? ORDER BY id",
                (str(cutoff),),
            ).fetchall()
        else:
            src_logs = src.execute("SELECT * FROM usage_logs ORDER BY id").fetchall()

        migrated_logs = 0
        skipped_logs = 0

        for row in src_logs:
            old_user_id = row["user_id"]
            new_user_id = id_map.get(old_user_id)
            if new_user_id is None:
                skipped_logs += 1
                continue

            if not dry_run:
                log = UsageLog(
                    user_id=new_user_id,
                    model=row["model"],
                    model_type=row["model_type"] if "model_type" in row.keys() else "",
                    input_tokens=row["input_tokens"],
                    output_tokens=row["output_tokens"],
                    cost_usd=row["cost_usd"],
                    endpoint=row["endpoint"] if "endpoint" in row.keys() else "",
                    created_at=row["created_at"],
                )
                session.add(log)
            migrated_logs += 1

        if not dry_run:
            session.commit()
        print(f"Usage logs: {migrated_logs} migrated, {skipped_logs} skipped")

    src.close()
    if dry_run:
        print("\n(Dry-run complete, no data was written)")
    else:
        print("\nMigration complete.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/migrate_sqlite_to_pg.py <sqlite_db_path> [--dry-run] [--sync]")
        sys.exit(1)

    sqlite_path = sys.argv[1]
    flags = sys.argv[2:]
    dry_run = "--dry-run" in flags
    sync = "--sync" in flags

    print(f"Migrating from: {sqlite_path}")
    print(f"Migrating to:   {DATABASE_URL.split('@')[-1] if '@' in DATABASE_URL else DATABASE_URL}\n")
    migrate(sqlite_path, dry_run=dry_run, sync=sync)
