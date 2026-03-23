"""
DEMO STEP 2 — Concurrency Under Pressure
==========================================
Two AI agents simultaneously attempt to write a CreditAnalysisCompleted
event to the same loan stream at expected_version=3.
Exactly one wins. The other gets OptimisticConcurrencyError and retries.
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
import asyncio
import uuid

import asyncpg

from src.event_store import EventStore
from src.models.events import (
    ApplicationSubmitted, CreditAnalysisRequested, AgentContextLoaded,
    CreditAnalysisCompleted, OptimisticConcurrencyError,
)

DATABASE_URL = "postgresql://postgres:13621@localhost:5433/apex_ledger"
SEP = "─" * 70


def header(title: str) -> None:
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


async def main() -> None:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)
    store = EventStore(pool)

    app_id    = str(uuid.uuid4())
    agent_a   = f"agent-alpha-{uuid.uuid4().hex[:6]}"
    agent_b   = f"agent-beta-{uuid.uuid4().hex[:6]}"
    sess_a    = f"sess-{uuid.uuid4().hex[:6]}"
    sess_b    = f"sess-{uuid.uuid4().hex[:6]}"

    header("DOUBLE-DECISION CONCURRENCY TEST")
    print(f"  Application ID  : {app_id}")
    print(f"  Agent Alpha     : {agent_a}")
    print(f"  Agent Beta      : {agent_b}")

    # ── Seed stream to version 3 ──────────────────────────────────────
    header("SETUP — Seed loan stream to version 3")

    await store.append(f"loan-{app_id}", [
        ApplicationSubmitted(
            application_id=app_id, applicant_id="test-corp",
            requested_amount_usd=500_000.0,
            loan_purpose="equipment", submission_channel="api",
        )
    ], expected_version=-1)
    print("  stream_position=1  ApplicationSubmitted")

    await store.append(f"loan-{app_id}", [
        CreditAnalysisRequested(
            application_id=app_id, assigned_agent_id=agent_a, priority="high"
        )
    ], expected_version=1)
    print("  stream_position=2  CreditAnalysisRequested")

    # Both agents start their sessions (stream now at version 2 for loan)
    # We use the agent streams separately — just seed one extra event on loan
    await store.append(f"agent-{agent_a}-{sess_a}", [
        AgentContextLoaded(
            agent_id=agent_a, session_id=sess_a,
            context_source="fresh", model_version="credit-model-v2.3",
        )
    ], expected_version=-1)
    await store.append(f"agent-{agent_b}-{sess_b}", [
        AgentContextLoaded(
            agent_id=agent_b, session_id=sess_b,
            context_source="fresh", model_version="credit-model-v2.3",
        )
    ], expected_version=-1)

    # Add a third loan event to bring it to version 3 for the demo
    await store.append(f"loan-{app_id}", [
        AgentContextLoaded(
            agent_id=agent_a, session_id=sess_a,
            context_source="fresh", model_version="credit-model-v2.3",
        )
    ], expected_version=2)
    print("  stream_position=3  AgentContextLoaded (loan stream at version 3)")

    version_before = await store.stream_version(f"loan-{app_id}")
    print(f"\n  Loan stream is now at version {version_before}")
    print(f"  Both agents will call append with expected_version={version_before}")

    # ── Race ──────────────────────────────────────────────────────────
    header("RACE — Two agents append simultaneously at expected_version=3")

    results   = {}
    occ_agent = None
    win_agent = None

    async def agent_append(agent_id: str, session_id: str, label: str) -> None:
        nonlocal occ_agent, win_agent
        event = CreditAnalysisCompleted(
            application_id=app_id,
            agent_id=agent_id, session_id=session_id,
            model_version="credit-model-v2.3",
            confidence_score=0.82, risk_tier="MEDIUM",
            recommended_limit_usd=450_000.0,
            analysis_duration_ms=900,
            input_data_hash="abc123",
        )
        try:
            new_v = await store.append(
                stream_id=f"loan-{app_id}",
                events=[event],
                expected_version=version_before,   # both see version 3
            )
            results[label] = ("WIN", new_v)
            win_agent = label
            print(f"  [{label}] ✅ SUCCEEDED — new stream_version={new_v}")
        except OptimisticConcurrencyError as e:
            results[label] = ("OCC", e)
            occ_agent = label
            print(f"  [{label}] ❌ OptimisticConcurrencyError")
            print(f"           expected_version={e.expected_version}  actual_version={e.actual_version}")
            print(f"           suggested_action={e.suggested_action}")

            # Retry: reload stream and append
            print(f"  [{label}] 🔄 Reloading stream and retrying …")
            current_v = await store.stream_version(f"loan-{app_id}")
            new_v = await store.append(
                stream_id=f"loan-{app_id}",
                events=[event],
                expected_version=current_v,
            )
            print(f"  [{label}] ✅ RETRY SUCCEEDED — new stream_version={new_v}")

    # Fire concurrently
    await asyncio.gather(
        agent_append(agent_a, sess_a, "ALPHA"),
        agent_append(agent_b, sess_b, "BETA"),
    )

    # ── Verify ────────────────────────────────────────────────────────
    header("VERIFICATION")
    final_version = await store.stream_version(f"loan-{app_id}")
    events = await store.load_stream(f"loan-{app_id}")
    credit_events = [e for e in events if e.event_type == "CreditAnalysisCompleted"]

    print(f"  Final stream version : {final_version}")
    print(f"  CreditAnalysisCompleted events : {len(credit_events)}")
    for ce in credit_events:
        print(f"    stream_position={ce.stream_position}  agent={ce.payload.get('agent_id')}")

    assert len(credit_events) == 2, (
        f"Expected 2 CreditAnalysisCompleted events (one per retry), got {len(credit_events)}"
    )
    assert results["ALPHA"][0] != results["BETA"][0], (
        "Both agents got the same result — concurrency control failed!"
    )

    print(f"\n  ✅ CONCURRENCY CONTROL VERIFIED")
    print(f"     Winner   : {win_agent}")
    print(f"     Loser    : {occ_agent}  (received OCC, retried, eventually succeeded)")
    print(f"     Stream length = {final_version}  (both decisions preserved)\n")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
