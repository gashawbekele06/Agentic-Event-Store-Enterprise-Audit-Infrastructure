"""
test_concurrency.py — The Double-Decision Concurrency Test (Phase 1 critical test).

Scenario: Two AI agents simultaneously attempt to append a CreditAnalysisCompleted
event to the same loan application stream.  Both read the stream at version 3 and
pass expected_version=3 to their append call.

Assertions:
  (a) Total events appended to the stream = 4 (the 3 existing + 1 new), not 5.
  (b) The winning task's event has stream_position = 4.
  (c) The losing task raises OptimisticConcurrencyError (not silently swallowed).

Why this matters: In the Apex loan scenario, this represents two fraud-detection
agents simultaneously flagging the same application.  Without optimistic concurrency
control, both flags are applied and the state becomes inconsistent.
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from src.event_store import EventStore
from src.models.events import (
    ApplicationSubmitted,
    CreditAnalysisCompleted,
    CreditAnalysisRequested,
    OptimisticConcurrencyError,
)


# ---------------------------------------------------------------------------
# Helper: seed a stream with 3 events so current_version = 3
# ---------------------------------------------------------------------------

async def _seed_stream(store: EventStore, stream_id: str) -> None:
    """Insert 3 events to bring the stream to version 3."""
    # Event 1: ApplicationSubmitted
    await store.append(
        stream_id=stream_id,
        events=[
            ApplicationSubmitted(
                application_id="test-concurrency",
                applicant_id="applicant-001",
                requested_amount_usd=500_000.0,
                loan_purpose="expansion",
                submission_channel="api",
            )
        ],
        expected_version=-1,
    )
    # Event 2: CreditAnalysisRequested
    await store.append(
        stream_id=stream_id,
        events=[
            CreditAnalysisRequested(
                application_id="test-concurrency",
                assigned_agent_id="credit-agent-1",
            )
        ],
        expected_version=1,
    )
    # Event 3: A placeholder credit analysis from a first agent
    await store.append(
        stream_id=stream_id,
        events=[
            CreditAnalysisCompleted(
                application_id="test-concurrency",
                agent_id="credit-agent-1",
                session_id="session-001",
                model_version="v2.0",
                confidence_score=0.82,
                risk_tier="MEDIUM",
                recommended_limit_usd=400_000.0,
                analysis_duration_ms=1200,
                input_data_hash="abc123",
            )
        ],
        expected_version=2,
    )
    # Confirm version is 3
    v = await store.stream_version(stream_id)
    assert v == 3, f"Seed failed: expected version 3, got {v}"


# ---------------------------------------------------------------------------
# The Double-Decision Test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_double_decision_concurrency(store: EventStore) -> None:
    """Two concurrent appends at expected_version=3: exactly one wins."""
    stream_id = "loan-test-concurrency"
    await _seed_stream(store, stream_id)

    # Build two competing events
    event_a = CreditAnalysisCompleted(
        application_id="test-concurrency",
        agent_id="fraud-agent-A",
        session_id="session-A",
        model_version="v2.1",
        confidence_score=0.90,
        risk_tier="LOW",
        recommended_limit_usd=600_000.0,
        analysis_duration_ms=800,
        input_data_hash="hashA",
    )
    event_b = CreditAnalysisCompleted(
        application_id="test-concurrency",
        agent_id="fraud-agent-B",
        session_id="session-B",
        model_version="v2.1",
        confidence_score=0.70,
        risk_tier="MEDIUM",
        recommended_limit_usd=450_000.0,
        analysis_duration_ms=950,
        input_data_hash="hashB",
    )

    results: list[str] = []
    winner_version: int | None = None
    concurrency_error: OptimisticConcurrencyError | None = None

    async def append_task(label: str, event):
        nonlocal winner_version, concurrency_error
        try:
            new_version = await store.append(
                stream_id=stream_id,
                events=[event],
                expected_version=3,
            )
            results.append(f"{label}:success")
            winner_version = new_version
        except OptimisticConcurrencyError as exc:
            results.append(f"{label}:conflict")
            concurrency_error = exc

    # Launch both concurrently
    await asyncio.gather(
        append_task("A", event_a),
        append_task("B", event_b),
    )

    # --- Assertion (c): the loser received OptimisticConcurrencyError ------
    successes = [r for r in results if "success" in r]
    conflicts = [r for r in results if "conflict" in r]
    assert len(successes) == 1, f"Expected 1 success, got {len(successes)}: {results}"
    assert len(conflicts) == 1, f"Expected 1 conflict, got {len(conflicts)}: {results}"
    assert concurrency_error is not None, "OptimisticConcurrencyError was not raised"
    assert concurrency_error.expected_version == 3
    assert concurrency_error.actual_version == 4  # winner already advanced

    # --- Assertion (b): winning event has stream_position = 4 ---------------
    assert winner_version == 4

    # --- Assertion (a): stream has exactly 4 events (not 5) -----------------
    events = await store.load_stream(stream_id)
    assert len(events) == 4, f"Expected 4 events in stream, got {len(events)}"

    # The 4th event should be the winner's event
    fourth = events[3]
    assert fourth.stream_position == 4
    assert fourth.event_type == "CreditAnalysisCompleted"

    # Final version check
    final_version = await store.stream_version(stream_id)
    assert final_version == 4

    print("\n✓ Double-decision concurrency test PASSED")
    print(f"  Winner: {successes[0]}")
    print(f"  Loser:  {conflicts[0]} (OptimisticConcurrencyError raised correctly)")
    print(f"  Stream events: {len(events)}, final version: {final_version}")


# ---------------------------------------------------------------------------
# Additional concurrency edge cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_new_stream_double_create(store: EventStore) -> None:
    """Two tasks trying to create the same stream: exactly one wins."""
    stream_id = "loan-double-create"
    event = ApplicationSubmitted(
        application_id="double-create",
        applicant_id="applicant-002",
        requested_amount_usd=100_000.0,
        loan_purpose="equipment",
        submission_channel="portal",
    )

    results: list[str] = []

    async def create_task(label: str):
        try:
            await store.append(
                stream_id=stream_id,
                events=[event],
                expected_version=-1,
            )
            results.append(f"{label}:created")
        except OptimisticConcurrencyError:
            results.append(f"{label}:conflict")

    await asyncio.gather(create_task("X"), create_task("Y"))

    created = [r for r in results if "created" in r]
    conflicts = [r for r in results if "conflict" in r]
    assert len(created) == 1, f"Expected 1 creation, got {created}"
    assert len(conflicts) == 1, f"Expected 1 conflict, got {conflicts}"

    v = await store.stream_version(stream_id)
    assert v == 1


@pytest.mark.asyncio
async def test_sequential_appends_succeed(store: EventStore) -> None:
    """Sequential appends with correct expected_version all succeed."""
    stream_id = "loan-sequential"

    v = await store.append(
        stream_id=stream_id,
        events=[
            ApplicationSubmitted(
                application_id="seq-001",
                applicant_id="applicant-003",
                requested_amount_usd=200_000.0,
                loan_purpose="renovation",
                submission_channel="api",
            )
        ],
        expected_version=-1,
    )
    assert v == 1

    v = await store.append(
        stream_id=stream_id,
        events=[
            CreditAnalysisRequested(
                application_id="seq-001",
                assigned_agent_id="agent-x",
            )
        ],
        expected_version=1,
    )
    assert v == 2

    events = await store.load_stream(stream_id)
    assert len(events) == 2
    assert events[0].event_type == "ApplicationSubmitted"
    assert events[1].event_type == "CreditAnalysisRequested"


@pytest.mark.asyncio
async def test_wrong_expected_version_raises(store: EventStore) -> None:
    """Append with a stale expected_version raises OptimisticConcurrencyError."""
    stream_id = "loan-stale-version"
    await store.append(
        stream_id=stream_id,
        events=[
            ApplicationSubmitted(
                application_id="stale-001",
                applicant_id="applicant-004",
                requested_amount_usd=50_000.0,
                loan_purpose="startup",
                submission_channel="branch",
            )
        ],
        expected_version=-1,
    )

    with pytest.raises(OptimisticConcurrencyError) as exc_info:
        await store.append(
            stream_id=stream_id,
            events=[
                CreditAnalysisRequested(
                    application_id="stale-001",
                    assigned_agent_id="agent-y",
                )
            ],
            expected_version=0,  # stale — actual is 1
        )

    assert exc_info.value.expected_version == 0
    assert exc_info.value.actual_version == 1
    assert exc_info.value.stream_id == stream_id
