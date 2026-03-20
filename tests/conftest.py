"""
conftest.py — Shared fixtures for the test suite.

Provides an EventStore backed by a real PostgreSQL database,
with automatic schema creation and cleanup per test session.

Set the DATABASE_URL environment variable, or the default is used.
"""
from __future__ import annotations

import os
from pathlib import Path

import asyncpg
import pytest_asyncio

from src.event_store import EventStore

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:13621@localhost:5433/apex_ledger_test"
)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "src" / "schema.sql"


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _db_pool():
    """Create the test database schema once per session."""
    sys_conn = None
    try:
        sys_conn = await asyncpg.connect(
            DATABASE_URL.rsplit("/", 1)[0] + "/postgres"
        )
        db_name = DATABASE_URL.rsplit("/", 1)[1].split("?")[0]
        await sys_conn.execute(f'CREATE DATABASE "{db_name}"')
    except asyncpg.DuplicateDatabaseError:
        pass
    except Exception:
        pass
    finally:
        if sys_conn:
            await sys_conn.close()

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)

    # Apply schema
    schema_sql = SCHEMA_PATH.read_text()
    async with pool.acquire() as conn:
        await conn.execute(schema_sql)

    yield pool
    await pool.close()


@pytest_asyncio.fixture(loop_scope="session")
async def store(_db_pool: asyncpg.Pool):
    """Provide a fresh EventStore per test, with tables truncated."""
    async with _db_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE events, event_streams, projection_checkpoints, outbox CASCADE"
        )
    return EventStore(_db_pool)
