"""
DEMO STEP 4 — Upcasting & Immutability
========================================
1. Manually insert a v1 CreditAnalysisCompleted event (as legacy data would look)
2. Load it through EventStore.load_stream() — arrives as v2 with inferred fields
3. Query the raw database row — stored payload is UNCHANGED (v1, no model_version)

This proves the core event sourcing guarantee: upcasting is a READ-TIME transform.
The past is immutable. Only the view changes.
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
import asyncio
import json
import uuid
from datetime import datetime, timezone

import asyncpg

from src.event_store import EventStore

DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/apex_ledger"
SEP = "─" * 70


def header(title: str) -> None:
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


async def main() -> None:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)
    store = EventStore(pool)

    app_id    = str(uuid.uuid4())
    stream_id = f"loan-{app_id}"
    event_id  = str(uuid.uuid4())

    header("UPCASTING & IMMUTABILITY DEMO")
    print(f"  Stream : {stream_id}")
    print(f"  EventID: {event_id}")

    # ── Step 1: Directly insert a v1 event (simulating legacy data) ───
    header("STEP 1 · Directly INSERT a v1 CreditAnalysisCompleted into PostgreSQL")

    v1_payload = {
        "application_id": app_id,
        "agent_id": "legacy-credit-agent",
        "session_id": "legacy-session-001",
        # NOTE: NO model_version, NO confidence_score — this is pre-2026 schema
        "risk_tier": "MEDIUM",
        "recommended_limit_usd": 300_000.0,
        "analysis_duration_ms": 2_200,
        "input_data_hash": "sha256-legacy-inputs",
    }

    async with pool.acquire() as conn:
        # Create stream entry
        await conn.execute(
            """
            INSERT INTO event_streams (stream_id, aggregate_type, current_version)
            VALUES ($1, 'LoanApplication', 1)
            ON CONFLICT (stream_id) DO UPDATE SET current_version=1
            """,
            stream_id,
        )
        # Insert raw v1 event — event_version=1 explicitly
        await conn.execute(
            """
            INSERT INTO events
                (event_id, stream_id, stream_position, event_type,
                 event_version, payload, metadata, recorded_at)
            VALUES ($1, $2, 1, 'CreditAnalysisCompleted', 1, $3::jsonb, '{}'::jsonb, $4)
            """,
            event_id, stream_id,
            json.dumps(v1_payload),
            datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc),   # pre-2026 date
        )

    print(f"  ✓ Raw INSERT complete — event_version=1")
    print(f"  Stored payload keys: {sorted(v1_payload.keys())}")
    print(f"  Note: 'model_version' is ABSENT from stored payload")

    # ── Step 2: Load through EventStore — arrives as v2 ───────────────
    header("STEP 2 · Load via EventStore.load_stream() — upcaster applied")

    events = await store.load_stream(stream_id)
    assert len(events) == 1
    loaded = events[0]

    print(f"  event_type    : {loaded.event_type}")
    print(f"  event_version : {loaded.event_version}  ← was 1, now 2 (upcasted)")
    print(f"  Payload keys  : {sorted(loaded.payload.keys())}")
    print()
    print(f"  model_version    : {loaded.payload.get('model_version')!r}")
    print(f"    → inferred: 'legacy-pre-2026' (recorded before model tracking existed)")
    print()
    print(f"  confidence_score : {loaded.payload.get('confidence_score')!r}")
    print(f"    → null: genuinely unknown — fabrication would be worse than null")
    print()
    print(f"  risk_tier        : {loaded.payload.get('risk_tier')!r}  (original, unchanged)")

    assert loaded.event_version == 2, "Should arrive as v2"
    assert loaded.payload.get("model_version") == "legacy-pre-2026"
    assert loaded.payload.get("confidence_score") is None

    # ── Step 3: Query raw DB — stored payload UNCHANGED ───────────────
    header("STEP 3 · Direct PostgreSQL query — stored payload is UNCHANGED")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT event_version, payload FROM events WHERE event_id = $1::uuid",
            event_id,
        )

    raw_version = row["event_version"]
    raw_payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])

    print(f"  Raw event_version in DB : {raw_version}  ← still 1 (immutable)")
    print(f"  Raw payload keys in DB  : {sorted(raw_payload.keys())}")
    print()
    print(f"  'model_version' in raw payload   : {'model_version' in raw_payload}")
    print(f"  'confidence_score' in raw payload: {'confidence_score' in raw_payload}")

    assert raw_version == 1, f"DB must still show v1, got v{raw_version}"
    assert "model_version" not in raw_payload, "model_version must NOT be in stored payload"
    assert "confidence_score" not in raw_payload, "confidence_score must NOT be in stored payload"

    # ── Summary ───────────────────────────────────────────────────────
    header("VERIFICATION SUMMARY")
    print(f"  Stored in DB        → event_version=1  no model_version  no confidence_score")
    print(f"  Loaded via store    → event_version=2  model_version=legacy-pre-2026  confidence=None")
    print(f"  DB after load       → event_version=1  UNCHANGED  ← the immutability guarantee")
    print()
    print(f"  ✅ UPCASTING IMMUTABILITY PROVEN")
    print(f"     The past cannot be altered. Only the read-time view evolves.\n")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
