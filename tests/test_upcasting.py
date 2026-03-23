"""
test_upcasting.py — Immutability test for the UpcasterRegistry.

Critical guarantee of event sourcing:
  Upcasters MUST transform events at read time only.
  The stored payload in PostgreSQL MUST remain unchanged.

Test:
  1. Write a v1 event directly to the store
  2. Load via EventStore.load_stream() → verify it arrives as v2 (upcasted)
  3. Query raw events table → verify stored payload is unchanged (v1)

Any implementation where upcasting modifies the stored event has broken
the core immutability guarantee of event sourcing.
"""
from __future__ import annotations

import json
import uuid

import pytest

from src.event_store import EventStore
from src.models.events import ApplicationSubmitted, CreditAnalysisCompleted


@pytest.mark.asyncio
async def test_upcast_credit_analysis_v1_to_v2(store: EventStore):
    """
    CreditAnalysisCompleted v1 events (missing model_version/confidence_score)
    must be transparently upcasted to v2 on load without touching stored bytes.
    """
    app_id = str(uuid.uuid4())

    # ----- 1. Set up a loan stream with the app submitted -----
    await store.append(
        stream_id=f"loan-{app_id}",
        events=[ApplicationSubmitted(
            application_id=app_id,
            applicant_id="applicant-001",
            requested_amount_usd=500_000.0,
            loan_purpose="working_capital",
            submission_channel="api",
        )],
        expected_version=-1,
    )

    # ----- 2. Insert a v1 CreditAnalysisCompleted DIRECTLY into DB -----
    # Bypasses the EventStore so we can inject a v1 payload without model_version
    v1_event_id = str(uuid.uuid4())
    v1_payload = {
        "application_id": app_id,
        "agent_id": "agent-credit-001",
        "session_id": "sess-001",
        "risk_tier": "MEDIUM",
        "recommended_limit_usd": 450_000.0,
        "analysis_duration_ms": 1200,
        "input_data_hash": "abc123",
        # Deliberately omit model_version and confidence_score (v1 schema)
    }

    async with store._pool.acquire() as conn:
        # Insert into event_streams first (the stream already exists)
        # Insert directly into events at position 2
        await conn.execute(
            """
            INSERT INTO events
                (event_id, stream_id, stream_position, event_type, event_version, payload, metadata, recorded_at)
            VALUES
                ($1::uuid, $2, 2, 'CreditAnalysisCompleted', 1, $3::jsonb, '{}'::jsonb, clock_timestamp())
            """,
            v1_event_id,
            f"loan-{app_id}",
            json.dumps(v1_payload),
        )
        # Also update event_streams current_version
        await conn.execute(
            "UPDATE event_streams SET current_version=2 WHERE stream_id=$1",
            f"loan-{app_id}",
        )

    # ----- 3. Load via EventStore — should be upcasted to v2 -----
    events = await store.load_stream(f"loan-{app_id}")
    credit_events = [e for e in events if e.event_type == "CreditAnalysisCompleted"]
    assert len(credit_events) == 1, "Expected exactly one CreditAnalysisCompleted event"

    loaded = credit_events[0]

    # After upcast: event_version should be 2
    assert loaded.event_version == 2, (
        f"Expected event_version=2 after upcast, got {loaded.event_version}"
    )

    # After upcast: model_version should be the legacy sentinel
    assert "model_version" in loaded.payload, "Upcasted payload must contain model_version"
    assert loaded.payload["model_version"] == "legacy-pre-2026", (
        f"Expected 'legacy-pre-2026', got {loaded.payload['model_version']}"
    )

    # After upcast: confidence_score should be present (None is acceptable)
    assert "confidence_score" in loaded.payload, (
        "Upcasted payload must contain confidence_score key (None is acceptable)"
    )

    # ----- 4. Verify stored bytes are UNCHANGED -----
    async with store._pool.acquire() as conn:
        raw_row = await conn.fetchrow(
            "SELECT payload, event_version FROM events WHERE event_id=$1::uuid",
            v1_event_id,
        )

    assert raw_row is not None, "Raw event must still exist in DB"

    raw_payload = raw_row["payload"] if isinstance(raw_row["payload"], dict) else json.loads(raw_row["payload"])
    raw_version = raw_row["event_version"]

    # CRITICAL: stored version must still be 1
    assert raw_version == 1, (
        f"IMMUTABILITY VIOLATION: stored event_version was modified to {raw_version} (expected 1)"
    )

    # CRITICAL: stored payload must NOT contain model_version (v1 field)
    assert "model_version" not in raw_payload, (
        "IMMUTABILITY VIOLATION: 'model_version' was written into the stored v1 payload"
    )
    assert "confidence_score" not in raw_payload, (
        "IMMUTABILITY VIOLATION: 'confidence_score' was written into stored v1 payload"
    )

    print("✅ Immutability test passed:")
    print(f"   Loaded (upcasted): event_version={loaded.event_version}, "
          f"model_version={loaded.payload.get('model_version')}")
    print(f"   Stored (raw):      event_version={raw_version}, "
          f"model_version=ABSENT (correct)")


@pytest.mark.asyncio
async def test_upcast_chain_does_not_alter_db(store: EventStore):
    """
    Additional guard: loading the same v1 event multiple times must not
    progressively mutate the stored record.
    """
    app_id = str(uuid.uuid4())
    v1_event_id = str(uuid.uuid4())
    v1_payload = {
        "application_id": app_id,
        "agent_id": "agent-multi",
        "session_id": "sess-multi",
        "risk_tier": "HIGH",
        "recommended_limit_usd": 100_000.0,
        "analysis_duration_ms": 800,
        "input_data_hash": "def456",
    }

    # Need a parent stream
    await store.append(
        stream_id=f"loan-{app_id}",
        events=[ApplicationSubmitted(
            application_id=app_id,
            applicant_id="applicant-002",
            requested_amount_usd=100_000.0,
            loan_purpose="equipment",
            submission_channel="portal",
        )],
        expected_version=-1,
    )

    async with store._pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO events
                (event_id, stream_id, stream_position, event_type, event_version, payload, metadata, recorded_at)
            VALUES ($1::uuid, $2, 2, 'CreditAnalysisCompleted', 1, $3::jsonb, '{}'::jsonb, clock_timestamp())
            """,
            v1_event_id, f"loan-{app_id}", json.dumps(v1_payload),
        )
        await conn.execute(
            "UPDATE event_streams SET current_version=2 WHERE stream_id=$1",
            f"loan-{app_id}",
        )

    # Load three times
    for _ in range(3):
        events = await store.load_stream(f"loan-{app_id}")
        credit = [e for e in events if e.event_type == "CreditAnalysisCompleted"]
        assert credit[0].event_version == 2

    # Check DB is still v1
    async with store._pool.acquire() as conn:
        raw_version = await conn.fetchval(
            "SELECT event_version FROM events WHERE event_id=$1::uuid", v1_event_id
        )
    assert raw_version == 1, f"Version drifted after multiple loads: {raw_version}"
    print("✅ Multiple-load immutability test passed")
