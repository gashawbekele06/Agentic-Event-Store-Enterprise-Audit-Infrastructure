"""
test_gas_town.py — Gas Town agent memory reconstruction tests.

Tests the Gas Town persistent ledger pattern:
  "Every agent action written to event store before execution = crash-safe memory."

Simulated crash scenario:
  1. Start an agent session, append 5 events
  2. Discard the in-memory agent object (simulating process crash)
  3. Call reconstruct_agent_context() from only the event stream
  4. Verify the reconstructed context contains enough info to continue correctly
  5. Verify NEEDS_RECONCILIATION detection for partial state
"""
from __future__ import annotations

import uuid

import pytest

from src.commands.handlers import (
    CreditAnalysisCompletedCommand,
    StartAgentSessionCommand,
    handle_credit_analysis_completed,
    handle_request_credit_analysis,
    handle_start_agent_session,
    handle_submit_application,
    RequestCreditAnalysisCommand,
    SubmitApplicationCommand,
)
from src.event_store import EventStore
from src.integrity.gas_town import AgentContext, reconstruct_agent_context
from src.models.events import ApplicationSubmitted, AgentContextLoaded, CreditAnalysisCompleted


@pytest.mark.asyncio
async def test_reconstruct_after_simulated_crash(store: EventStore):
    """
    Simulated crash: start session, append 5 events, discard agent object,
    reconstruct from store. Verify context is sufficient to continue.
    """
    agent_id = f"agent-credit-{uuid.uuid4().hex[:8]}"
    session_id = f"sess-{uuid.uuid4().hex[:8]}"
    app_id_1 = str(uuid.uuid4())
    app_id_2 = str(uuid.uuid4())

    # --- Set up applications ---
    for app_id in [app_id_1, app_id_2]:
        await handle_submit_application(
            SubmitApplicationCommand(
                application_id=app_id,
                applicant_id=f"applicant-{app_id[:8]}",
                requested_amount_usd=250_000.0,
                loan_purpose="working_capital",
                submission_channel="api",
            ),
            store,
        )
        await handle_request_credit_analysis(
            RequestCreditAnalysisCommand(
                application_id=app_id,
                assigned_agent_id=agent_id,
            ),
            store,
        )

    # Event 1: AgentContextLoaded (start session — Gas Town anchor)
    await handle_start_agent_session(
        StartAgentSessionCommand(
            agent_id=agent_id,
            session_id=session_id,
            context_source="event_replay",
            model_version="credit-model-v2.3",
            event_replay_from_position=0,
            context_token_count=1024,
        ),
        store,
    )

    # Events 2-3: First credit analysis
    await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=app_id_1,
            agent_id=agent_id,
            session_id=session_id,
            model_version="credit-model-v2.3",
            confidence_score=0.87,
            risk_tier="LOW",
            recommended_limit_usd=250_000.0,
            duration_ms=1200,
            input_data={"financials": "hash123"},
        ),
        store,
    )

    # Events 4-5: Second credit analysis (partial - only context loaded)
    await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=app_id_2,
            agent_id=agent_id,
            session_id=session_id,
            model_version="credit-model-v2.3",
            confidence_score=0.62,
            risk_tier="MEDIUM",
            recommended_limit_usd=200_000.0,
            duration_ms=980,
            input_data={"financials": "hash456"},
        ),
        store,
    )

    # Save recovery identifiers before simulating crash.
    # In a real deployment these come from a process table, recovery config, or DB scan.
    _recovery_agent_id = agent_id
    _recovery_session_id = session_id

    # Simulate crash — discard ALL in-memory agent state
    del agent_id
    del session_id

    # --- CRASH SIMULATION: reconstruct from store only ---
    # The key test is that reconstruct_agent_context works WITHOUT the in-memory object.
    context = await reconstruct_agent_context(
        store=store,
        agent_id=_recovery_agent_id,
        session_id=_recovery_session_id,
        token_budget=8000,
    )

    # Verify context contains sufficient information to resume
    assert isinstance(context, AgentContext)
    assert context.last_event_position >= 2, (
        f"Last event position {context.last_event_position} is too low — context incomplete"
    )
    assert context.model_version == "credit-model-v2.3", (
        f"Model version not reconstructed: {context.model_version}"
    )
    assert context.context_source == "event_replay", (
        f"Context source not reconstructed: {context.context_source}"
    )
    assert len(context.decisions_made) >= 2, (
        f"Only {len(context.decisions_made)} decisions found — expected 2"
    )
    assert len(context.applications_processed) >= 1, (
        "No applications_processed in reconstructed context"
    )
    assert context.session_health_status == "HEALTHY", (
        f"Unexpected health status: {context.session_health_status}"
    )
    assert len(context.raw_recent_events) > 0, "No recent events in reconstructed context"
    assert context.context_text, "Context text is empty"

    print("✅ Gas Town crash recovery test passed")
    print(f"   Agent model: {context.model_version}")
    print(f"   Decisions made: {context.decisions_made}")
    print(f"   Applications processed: {context.applications_processed}")
    print(f"   Health status: {context.session_health_status}")
    print(f"   Last event position: {context.last_event_position}")
    print(f"   Context text preview: {context.context_text[:200]}...")


@pytest.mark.asyncio
async def test_needs_reconciliation_detection(store: EventStore):
    """
    If agent's last event is CreditAnalysisRequested (started but not completed),
    context must be flagged as NEEDS_RECONCILIATION.
    """
    agent_id = f"agent-recon-{uuid.uuid4().hex[:8]}"
    session_id = f"sess-recon-{uuid.uuid4().hex[:8]}"

    # Start session
    await store.append(
        stream_id=f"agent-{agent_id}-{session_id}",
        events=[AgentContextLoaded(
            agent_id=agent_id,
            session_id=session_id,
            context_source="fresh",
            event_replay_from_position=0,
            context_token_count=512,
            model_version="credit-model-v2.3",
        )],
        expected_version=-1,
    )

    # Simulate a partial state: inject a "CreditAnalysisRequested"-like marker
    # We'll use FraudScreeningCompleted as a "started" signal without a matching completion
    # Actually simulate by using a custom event type that reads as "in progress"
    # Per the gas_town.py logic, CreditAnalysisRequested in last pos = NEEDS_RECONCILIATION
    from src.models.events import CreditAnalysisRequested
    app_id = str(uuid.uuid4())
    await store.append(
        stream_id=f"agent-{agent_id}-{session_id}",
        events=[CreditAnalysisRequested(
            application_id=app_id,
            assigned_agent_id=agent_id,
            priority="high",
        )],
        expected_version=1,
    )

    context = await reconstruct_agent_context(store, agent_id, session_id)

    assert context.session_health_status == "NEEDS_RECONCILIATION", (
        f"Expected NEEDS_RECONCILIATION, got {context.session_health_status}"
    )
    assert context.needs_reconciliation_reason is not None
    assert "CreditAnalysisRequested" in context.needs_reconciliation_reason

    print("✅ NEEDS_RECONCILIATION detection test passed")
    print(f"   Reason: {context.needs_reconciliation_reason}")


@pytest.mark.asyncio
async def test_empty_session_returns_healthy(store: EventStore):
    """An agent with no events returns a healthy empty context, not an error."""
    context = await reconstruct_agent_context(
        store=store,
        agent_id="nonexistent-agent",
        session_id="nonexistent-session",
    )
    assert context.session_health_status == "HEALTHY"
    assert context.context_text == "No prior session events found."
    print("✅ Empty session returns healthy context")
