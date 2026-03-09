"""Add owner_id column to users table (PostgreSQL migration)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import text
from sqlmodel import create_engine

from app.core.config import DATABASE_URL

engine = create_engine(DATABASE_URL)

with engine.connect() as conn:
    conn.execute(
        text("ALTER TABLE users ADD COLUMN IF NOT EXISTS owner_id INTEGER REFERENCES users(id)")
    )
    conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_users_owner_id ON users (owner_id)")
    )
    conn.commit()

print("Done: owner_id column added to users table.")
