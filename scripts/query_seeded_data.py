"""
Query the event store after seeding to verify data is loaded correctly.

Shows:
  - Total events and streams
  - Event type breakdown
  - Applications with compliance flags (from profiles)
  - Sample application event history

Usage:
    uv run python scripts/query_seeded_data.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import asyncpg


async def main() -> None:
    dsn = os.getenv(
        "DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/apex_ledger",
    )
    conn = await asyncpg.connect(dsn)

    # ── Total counts ──────────────────────────────────────────────────────────
    total_events  = await conn.fetchval("SELECT COUNT(*) FROM events")
    total_streams = await conn.fetchval("SELECT COUNT(*) FROM event_streams")
    loan_streams  = await conn.fetchval(
        "SELECT COUNT(*) FROM event_streams WHERE stream_id LIKE 'loan-APEX-%'"
    )
    print(f"Total events  : {total_events}")
    print(f"Total streams : {total_streams}  (loan-APEX: {loan_streams})")

    # ── Event type breakdown ──────────────────────────────────────────────────
    rows = await conn.fetch(
        "SELECT event_type, COUNT(*) AS n FROM events GROUP BY event_type ORDER BY n DESC"
    )
    print("\nEvent type breakdown:")
    for r in rows:
        print(f"  {r['event_type']:<35s}  {r['n']:>5}")

    # ── Applications with compliance flags ────────────────────────────────────
    flag_rows = await conn.fetch(
        """
        SELECT stream_id, payload->>'rule_id' AS rule_id, payload->>'severity' AS sev
        FROM events
        WHERE event_type = 'ComplianceRuleFailed'
        ORDER BY stream_id
        """
    )
    if flag_rows:
        print(f"\nApplications with compliance flags ({len(flag_rows)} total):")
        for r in flag_rows:
            print(f"  {r['stream_id']:<25s}  {r['rule_id']:<20s}  {r['sev']}")
    else:
        print("\nNo ComplianceRuleFailed events found.")

    # ── Sample: first loan application full history ───────────────────────────
    first_stream = await conn.fetchval(
        "SELECT stream_id FROM event_streams WHERE stream_id LIKE 'loan-APEX-%' ORDER BY stream_id LIMIT 1"
    )
    if first_stream:
        evs = await conn.fetch(
            "SELECT stream_position, event_type, recorded_at FROM events "
            "WHERE stream_id = $1 ORDER BY stream_position",
            first_stream,
        )
        print(f"\nSample stream: {first_stream}  ({len(evs)} events)")
        for e in evs:
            ts = str(e["recorded_at"])[:19] if e["recorded_at"] else "—"
            print(f"  v{e['stream_position']:<3}  {ts}  {e['event_type']}")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
