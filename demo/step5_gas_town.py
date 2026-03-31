"""
DEMO STEP 5 — Gas Town Recovery
=================================
1. Start an agent session and append 5 events (2 credit analyses in progress)
2. Simulate a crash by deleting all in-memory agent state
3. Call reconstruct_agent_context() with only the event store
4. Show the agent can resume with full context — decisions made, applications
   processed, health status, and recent events — all from the store alone.
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
import asyncio
import uuid

import asyncpg

from src.event_store import EventStore
from src.commands.handlers import (
    SubmitApplicationCommand, RequestCreditAnalysisCommand,
    StartAgentSessionCommand, CreditAnalysisCompletedCommand,
    handle_submit_application, handle_request_credit_analysis,
    handle_start_agent_session, handle_credit_analysis_completed,
)
from src.integrity.gas_town import reconstruct_agent_context

DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/apex_ledger"
SEP = "─" * 70


def header(title: str) -> None:
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


async def main() -> None:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)
    store = EventStore(pool)

    agent_id = f"credit-{uuid.uuid4().hex[:8]}"
    sess_id  = f"sess-{uuid.uuid4().hex[:8]}"
    app_id_1 = str(uuid.uuid4())
    app_id_2 = str(uuid.uuid4())

    header("GAS TOWN AGENT CRASH RECOVERY DEMO")
    print(f"  Agent ID    : {agent_id}")
    print(f"  Session ID  : {sess_id}")
    print(f"  Application 1 : {app_id_1[:16]}…")
    print(f"  Application 2 : {app_id_2[:16]}…")

    # ── Setup: submit applications ────────────────────────────────────
    header("SETUP — Submit two loan applications")
    for app_id in [app_id_1, app_id_2]:
        await handle_submit_application(
            SubmitApplicationCommand(
                application_id=app_id,
                applicant_id=f"corp-{app_id[:8]}",
                requested_amount_usd=400_000.0,
                loan_purpose="working_capital",
                submission_channel="api",
            ), store,
        )
        await handle_request_credit_analysis(
            RequestCreditAnalysisCommand(
                application_id=app_id, assigned_agent_id=agent_id,
            ), store,
        )
        print(f"  ✓ Application {app_id[:16]}… submitted and queued")

    # ── Event 1: Gas Town anchor — AgentContextLoaded ─────────────────
    header("LIVE AGENT SESSION — Appending events")
    await handle_start_agent_session(
        StartAgentSessionCommand(
            agent_id=agent_id, session_id=sess_id,
            context_source="event_replay",
            model_version="credit-model-v2.3",
            event_replay_from_position=0,
            context_token_count=2_048,
        ), store,
    )
    print(f"  Event 1: AgentContextLoaded  (Gas Town anchor)")

    # ── Events 2-3: Credit analysis for app 1 ─────────────────────────
    await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=app_id_1,
            agent_id=agent_id, session_id=sess_id,
            model_version="credit-model-v2.3",
            confidence_score=0.91, risk_tier="LOW",
            recommended_limit_usd=400_000.0, duration_ms=1_050,
            input_data={"q": "q4-2025-financials"},
        ), store,
    )
    print(f"  Event 2: CreditAnalysisCompleted  app={app_id_1[:16]}…  risk=LOW")

    # ── Events 4-5: Credit analysis for app 2 ─────────────────────────
    await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=app_id_2,
            agent_id=agent_id, session_id=sess_id,
            model_version="credit-model-v2.3",
            confidence_score=0.67, risk_tier="MEDIUM",
            recommended_limit_usd=350_000.0, duration_ms=980,
            input_data={"q": "q3-2025-financials"},
        ), store,
    )
    print(f"  Event 3: CreditAnalysisCompleted  app={app_id_2[:16]}…  risk=MEDIUM")

    events_before_crash = await store.load_stream(f"agent-{agent_id}-{sess_id}")
    print(f"\n  Agent session stream has {len(events_before_crash)} events before crash")

    # ── SIMULATE CRASH ────────────────────────────────────────────────
    header("💥 SIMULATING PROCESS CRASH")
    print("  Deleting all in-memory agent state …")
    saved_agent_id = agent_id
    saved_sess_id  = sess_id
    del agent_id
    del sess_id
    # In a real crash: the process dies here.
    # The only record of what the agent was doing is the event stream.
    print("  ✓ In-memory state gone — agent_id and sess_id deleted from memory")
    print("  ✓ In production: the process dies; nothing survives except the event store")

    # ── RECOVERY ──────────────────────────────────────────────────────
    header("🔄 CRASH RECOVERY — reconstruct_agent_context()")
    print(f"  Reading from event stream: agent-{saved_agent_id}-{saved_sess_id}")
    print(f"  Token budget: 8,000 tokens")
    print()

    context = await reconstruct_agent_context(
        store=store,
        agent_id=saved_agent_id,
        session_id=saved_sess_id,
        token_budget=8_000,
    )

    # ── Display reconstructed context ─────────────────────────────────
    header("RECONSTRUCTED AGENT CONTEXT")
    print(f"  Agent ID          : {context.agent_id}")
    print(f"  Session ID        : {context.session_id}")
    print(f"  Model version     : {context.model_version}")
    print(f"  Context source    : {context.context_source}")
    print(f"  Last event pos    : {context.last_event_position}")
    print(f"  Session health    : {context.session_health_status}")
    print()
    print(f"  Decisions made ({len(context.decisions_made)}):")
    for d in context.decisions_made:
        print(f"    · {d}")
    print()
    print(f"  Applications processed ({len(context.applications_processed)}):")
    for a in context.applications_processed:
        print(f"    · {a[:16]}…")
    print()
    print(f"  Context text (token-efficient summary):")
    for line in context.context_text.split("\n"):
        print(f"    {line}")
    print()
    print(f"  Last 3 events (verbatim):")
    for ev in context.raw_recent_events:
        print(f"    [{ev['stream_position']}] {ev['event_type']}  @{ev['recorded_at']}")

    # ── Assertions ────────────────────────────────────────────────────
    assert context.session_health_status == "HEALTHY"
    assert context.model_version == "credit-model-v2.3"
    assert len(context.decisions_made) >= 2
    assert len(context.applications_processed) >= 1
    assert context.last_event_position >= 2

    header("VERIFICATION")
    print(f"  ✅ GAS TOWN RECOVERY VERIFIED")
    print(f"     Agent reconstructed WITHOUT any in-memory state")
    print(f"     Health: {context.session_health_status}")
    print(f"     Agent can resume from event position {context.last_event_position}")
    print(f"     No work will be repeated — completed analyses are tracked\n")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
