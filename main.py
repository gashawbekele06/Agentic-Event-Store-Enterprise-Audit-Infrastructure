"""Entry point — schema migration and basic smoke test."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import asyncpg


DATABASE_URL = "postgresql://postgres:13621@localhost:5433/apex_ledger"
SCHEMA_PATH = Path(__file__).resolve().parent / "src" / "schema.sql"


async def migrate() -> None:
    """Apply the event-store schema to the database."""
    # Ensure the database exists
    sys_conn = await asyncpg.connect(
        DATABASE_URL.rsplit("/", 1)[0] + "/postgres"
    )
    db_name = DATABASE_URL.rsplit("/", 1)[1].split("?")[0]
    try:
        await sys_conn.execute(f'CREATE DATABASE "{db_name}"')
        print(f"Created database '{db_name}'")
    except asyncpg.DuplicateDatabaseError:
        print(f"Database '{db_name}' already exists")
    finally:
        await sys_conn.close()

    # Apply schema DDL
    conn = await asyncpg.connect(DATABASE_URL)
    schema_sql = SCHEMA_PATH.read_text()
    await conn.execute(schema_sql)
    await conn.close()
    print("Schema applied successfully.")


async def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "migrate":
        await migrate()
    else:
        print("The Ledger — Agentic Event Store & Enterprise Audit Infrastructure")
        print()
        print("Usage:")
        print("  python main.py migrate    Apply database schema")
        print()
        print("See README.md for full setup and usage instructions.")


if __name__ == "__main__":
    asyncio.run(main())
