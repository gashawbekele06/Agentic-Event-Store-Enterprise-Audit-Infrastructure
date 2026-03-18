from .event_store import EventStore
from .models.events import *
from typing import List
import asyncio

async def test_concurrent_double_append():
    store = EventStore("postgresql://localhost/apex_ledger")

    stream_id = "loan-test-123"
    events = [
        CreditAnalysisCompleted(
            application_id="test-123",
            agent_id="agent-1",
            session_id="session-1",
            model_version="v1.0",
            confidence_score=0.85,
            risk_tier="MEDIUM",
            recommended_limit_usd=500000,
            analysis_duration_ms=1500,
            input_data_hash="hash123"
        )
    ]

    # Start two concurrent appends
    async def append_task(task_id: int):
        try:
            version = await store.append(stream_id, events, expected_version=3)
            return f"Task {task_id} succeeded with version {version}"
        except OptimisticConcurrencyError as e:
            return f"Task {task_id} failed: {e}"

    results = await asyncio.gather(
        append_task(1),
        append_task(2),
        return_exceptions=True
    )

    # Assert exactly one succeeds, one fails
    successes = [r for r in results if "succeeded" in r]
    failures = [r for r in results if "failed" in r]

    assert len(successes) == 1, f"Expected 1 success, got {len(successes)}"
    assert len(failures) == 1, f"Expected 1 failure, got {len(failures)}"

    # Check stream has exactly 4 events (3 existing + 1 new)
    stream_events = await store.load_stream(stream_id)
    assert len(stream_events) == 4, f"Expected 4 events, got {len(stream_events)}"

    print("Double-decision concurrency test passed!")

if __name__ == "__main__":
    asyncio.run(test_concurrent_double_append())