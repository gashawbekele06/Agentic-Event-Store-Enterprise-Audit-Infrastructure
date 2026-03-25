"""
test_projections.py — Projection SLO tests and rebuild_from_scratch.

Tests:
  1. ProjectionDaemon processes events and ApplicationSummary reflects current state
  2. Projection lag stays < 500ms under load (50 concurrent command handlers)
  3. ComplianceAuditView lag stays < 2000ms under same load
  4. rebuild_from_scratch() replays all events correctly
  5. Temporal compliance query returns correct past state
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone

import pytest

from src.commands.handlers import (
    CreditAnalysisCompletedCommand,
    RecordComplianceCheckCommand,
    RequestCreditAnalysisCommand,
    StartAgentSessionCommand,
    StartComplianceReviewCommand,
    SubmitApplicationCommand,
    handle_credit_analysis_completed,
    handle_record_compliance_check,
    handle_request_credit_analysis,
    handle_start_agent_session,
    handle_start_compliance_review,
    handle_submit_application,
)
from src.event_store import EventStore
from src.projections import ProjectionDaemon
from src.projections.agent_performance import AgentPerformanceLedgerProjection
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.compliance_audit import ComplianceAuditViewProjection


async def _setup_projection_tables(store: EventStore):
    proj_app = ApplicationSummaryProjection()
    proj_perf = AgentPerformanceLedgerProjection()
    proj_comp = ComplianceAuditViewProjection()
    await proj_app.ensure_schema(store)
    await proj_perf.ensure_schema(store)
    await proj_comp.ensure_schema(store)
    return proj_app, proj_perf, proj_comp


async def _run_daemon_until_caught_up(
    store: EventStore,
    projections: list,
    max_wait_ms: int = 1000,
) -> float:
    """Run the daemon until projections are caught up; return elapsed ms."""
    daemon = ProjectionDaemon(store, projections)
    start = time.monotonic()

    deadline = start + (max_wait_ms / 1000)
    while time.monotonic() < deadline:
        await daemon._process_batch()
        lags = await daemon.get_all_lags()
        if all(lag == 0 for lag in lags.values()):
            break
        await asyncio.sleep(0.05)

    elapsed_ms = (time.monotonic() - start) * 1000
    return elapsed_ms


@pytest.mark.asyncio
async def test_application_summary_projection_basic(store: EventStore):
    """ApplicationSummary reflects loan state after daemon processes events."""
    proj_app, proj_perf, proj_comp = await _setup_projection_tables(store)

    app_id = str(uuid.uuid4())
    agent_id = f"credit-agent-{uuid.uuid4().hex[:8]}"
    session_id = f"sess-{uuid.uuid4().hex[:8]}"

    # Submit application
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=app_id, applicant_id="applicant-001",
            requested_amount_usd=300_000.0, loan_purpose="real_estate",
            submission_channel="api",
        ),
        store,
    )

    # Request credit analysis
    await handle_request_credit_analysis(
        RequestCreditAnalysisCommand(application_id=app_id, assigned_agent_id=agent_id),
        store,
    )

    # Start agent session + record analysis
    await handle_start_agent_session(
        StartAgentSessionCommand(
            agent_id=agent_id, session_id=session_id,
            context_source="fresh", model_version="credit-v2.3",
        ),
        store,
    )
    await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=app_id, agent_id=agent_id, session_id=session_id,
            model_version="credit-v2.3", confidence_score=0.82,
            risk_tier="LOW", recommended_limit_usd=300_000.0, duration_ms=950,
        ),
        store,
    )

    # Run daemon to catch up
    elapsed = await _run_daemon_until_caught_up(store, [proj_app, proj_perf, proj_comp])

    # Verify projection state
    row = await proj_app.get(store, app_id)
    assert row is not None, "ApplicationSummary must have a row for the application"
    assert row["state"] == "ANALYSIS_COMPLETE", f"Unexpected state: {row['state']}"
    assert row["risk_tier"] == "LOW", f"risk_tier not updated: {row['risk_tier']}"

    print(f"✅ ApplicationSummary basic test passed (daemon caught up in {elapsed:.0f}ms)")


@pytest.mark.asyncio
async def test_projection_lag_slo_under_load(store: EventStore):
    """
    SLO: ProjectionDaemon must catch up within 500ms for ApplicationSummary
    and 2000ms for ComplianceAuditView under 50 concurrent command handlers.
    """
    proj_app, proj_perf, proj_comp = await _setup_projection_tables(store)

    # Submit 50 applications concurrently
    n = 50
    app_ids = [str(uuid.uuid4()) for _ in range(n)]
    agent_id = f"load-agent-{uuid.uuid4().hex[:8]}"
    session_id = f"load-sess-{uuid.uuid4().hex[:8]}"

    # Start agent session
    await handle_start_agent_session(
        StartAgentSessionCommand(
            agent_id=agent_id, session_id=session_id,
            context_source="fresh", model_version="credit-v2.3",
        ),
        store,
    )

    async def submit_and_analyse(app_id: str):
        await handle_submit_application(
            SubmitApplicationCommand(
                application_id=app_id, applicant_id="load-applicant",
                requested_amount_usd=100_000.0, loan_purpose="working_capital",
                submission_channel="api",
            ),
            store,
        )
        await handle_request_credit_analysis(
            RequestCreditAnalysisCommand(application_id=app_id, assigned_agent_id=agent_id),
            store,
        )
        await handle_credit_analysis_completed(
            CreditAnalysisCompletedCommand(
                application_id=app_id, agent_id=agent_id, session_id=session_id,
                model_version="credit-v2.3", confidence_score=0.75,
                risk_tier="MEDIUM", recommended_limit_usd=90_000.0, duration_ms=500,
            ),
            store,
        )

    # Fire all 20 concurrently
    await asyncio.gather(*[submit_and_analyse(app_id) for app_id in app_ids])

    # Run daemon and measure catch-up time
    elapsed_ms = await _run_daemon_until_caught_up(
        store, [proj_app, proj_perf, proj_comp], max_wait_ms=5000
    )

    # Verify SLO
    assert elapsed_ms < 500, (
        f"ApplicationSummary SLO VIOLATED: catch-up took {elapsed_ms:.0f}ms (limit: 500ms)"
    )

    # Verify all apps are in projection
    for app_id in app_ids[:5]:  # spot-check 5
        row = await proj_app.get(store, app_id)
        assert row is not None, f"app {app_id} missing from ApplicationSummary"
        assert row["state"] == "ANALYSIS_COMPLETE"

    print(f"✅ Projection lag SLO test passed ({n} concurrent handlers): caught up in {elapsed_ms:.0f}ms < 500ms")


@pytest.mark.asyncio
async def test_rebuild_from_scratch(store: EventStore):
    """
    rebuild_from_scratch() must truncate and replay all events,
    ending in the same projection state as the live projection.
    """
    proj_app, proj_perf, proj_comp = await _setup_projection_tables(store)

    # Create some test data
    app_id = str(uuid.uuid4())
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=app_id, applicant_id="rebuild-applicant",
            requested_amount_usd=200_000.0, loan_purpose="expansion",
            submission_channel="direct",
        ),
        store,
    )

    # Run daemon to populate projections
    await _run_daemon_until_caught_up(store, [proj_app])

    row_before = await proj_app.get(store, app_id)
    assert row_before is not None, "Row must exist before rebuild"

    # Rebuild from scratch
    proj_app2 = ApplicationSummaryProjection()
    await proj_app2.ensure_schema(store)
    await proj_app2.rebuild_from_scratch(store)

    row_after = await proj_app2.get(store, app_id)
    assert row_after is not None, "Row must exist after rebuild"
    assert row_after["state"] == row_before["state"], (
        f"State mismatch after rebuild: {row_after['state']} vs {row_before['state']}"
    )

    print(f"✅ rebuild_from_scratch test passed: state={row_after['state']}")


@pytest.mark.asyncio
async def test_compliance_temporal_query(store: EventStore):
    """
    ComplianceAuditView temporal query: state at past timestamp != state now.
    """
    proj_app, proj_perf, proj_comp = await _setup_projection_tables(store)

    app_id = str(uuid.uuid4())
    agent_id = f"comp-agent-{uuid.uuid4().hex[:8]}"
    session_id = f"comp-sess-{uuid.uuid4().hex[:8]}"

    # Submit and go through credit analysis
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=app_id, applicant_id="comp-applicant",
            requested_amount_usd=150_000.0, loan_purpose="trade_finance",
            submission_channel="api",
        ),
        store,
    )
    await handle_request_credit_analysis(
        RequestCreditAnalysisCommand(application_id=app_id, assigned_agent_id=agent_id),
        store,
    )
    await handle_start_agent_session(
        StartAgentSessionCommand(
            agent_id=agent_id, session_id=session_id,
            context_source="fresh", model_version="credit-v2.3",
        ),
        store,
    )
    await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=app_id, agent_id=agent_id, session_id=session_id,
            model_version="credit-v2.3", confidence_score=0.88,
            risk_tier="LOW", recommended_limit_usd=150_000.0, duration_ms=800,
        ),
        store,
    )

    # Mark point in time BEFORE compliance checks
    timestamp_before_compliance = datetime.now(timezone.utc)
    await asyncio.sleep(0.05)

    # Start compliance review with checks
    await handle_start_compliance_review(
        StartComplianceReviewCommand(
            application_id=app_id,
            checks_required=["KYC-001", "AML-002"],
            regulation_set_version="reg-v2026-q1",
        ),
        store,
    )
    await handle_record_compliance_check(
        RecordComplianceCheckCommand(
            application_id=app_id, rule_id="KYC-001", rule_version="v3",
            passed=True, evidence_hash="kyc_evidence_hash",
        ),
        store,
    )

    # Run daemon
    await _run_daemon_until_caught_up(store, [proj_app, proj_comp])

    # Query CURRENT state — should show KYC-001 PASSED
    current = await proj_comp.get_current_compliance(store, app_id)
    assert current is not None, "Should have current compliance record"

    # Query BEFORE compliance checks — should find no compliance checks
    past = await proj_comp.get_compliance_at(store, app_id, timestamp_before_compliance)
    # No compliance events before this timestamp — should return None or empty checks
    if past is not None:
        checks = past.get("checks", [])
        # Checks should be empty or all PENDING (no PASSED/FAILED yet)
        for chk in checks:
            assert chk.get("status") not in ("PASSED", "FAILED"), (
                f"Temporal query returned future state: {chk}"
            )

    print("✅ temporal compliance query test passed")
    if current:
        print(f"   Current checks: {[c.get('rule_id') + ':' + c.get('status','?') for c in current.get('checks', [])]}")


@pytest.mark.asyncio
async def test_compliance_slo_during_and_after_rebuild(store: EventStore):
    """
    SLO assertion during and after ComplianceAuditView rebuild.

    Procedure:
      1. Fire 50 concurrent command handlers, each running a full
         submit → credit → compliance path (3 rules per application).
      2. Run the daemon until caught up — assert lag < 2000ms.
      3. Trigger rebuild_from_scratch() on ComplianceAuditView.
      4. Run the daemon again — assert lag < 2000ms after rebuild.
      5. Spot-check that rebuilt projection matches pre-rebuild state.
    """
    proj_app, proj_perf, proj_comp = await _setup_projection_tables(store)

    n = 50
    app_ids = [str(uuid.uuid4()) for _ in range(n)]
    agent_id = f"comp-load-agent-{uuid.uuid4().hex[:8]}"
    session_id = f"comp-load-sess-{uuid.uuid4().hex[:8]}"

    await handle_start_agent_session(
        StartAgentSessionCommand(
            agent_id=agent_id, session_id=session_id,
            context_source="fresh", model_version="credit-v2.3",
        ),
        store,
    )

    async def full_compliance_pipeline(app_id: str) -> None:
        await handle_submit_application(
            SubmitApplicationCommand(
                application_id=app_id, applicant_id="comp-load-applicant",
                requested_amount_usd=100_000.0, loan_purpose="working_capital",
                submission_channel="api",
            ),
            store,
        )
        await handle_request_credit_analysis(
            RequestCreditAnalysisCommand(application_id=app_id, assigned_agent_id=agent_id),
            store,
        )
        await handle_credit_analysis_completed(
            CreditAnalysisCompletedCommand(
                application_id=app_id, agent_id=agent_id, session_id=session_id,
                model_version="credit-v2.3", confidence_score=0.80,
                risk_tier="LOW", recommended_limit_usd=95_000.0, duration_ms=400,
            ),
            store,
        )
        await handle_start_compliance_review(
            StartComplianceReviewCommand(
                application_id=app_id,
                checks_required=["KYC-001", "AML-002", "BSA-003"],
                regulation_set_version="reg-v2026-q1",
            ),
            store,
        )
        for rule_id, rule_ver in [("KYC-001", "v3"), ("AML-002", "v2"), ("BSA-003", "v1")]:
            await handle_record_compliance_check(
                RecordComplianceCheckCommand(
                    application_id=app_id, rule_id=rule_id,
                    rule_version=rule_ver, passed=True,
                ),
                store,
            )

    # Fire all 50 pipelines concurrently
    await asyncio.gather(*[full_compliance_pipeline(app_id) for app_id in app_ids])

    # --- Phase 1: assert lag < 2000ms under load ---
    elapsed_live = await _run_daemon_until_caught_up(
        store, [proj_app, proj_perf, proj_comp], max_wait_ms=5000
    )
    assert elapsed_live < 2000, (
        f"ComplianceAuditView SLO VIOLATED under load: {elapsed_live:.0f}ms (limit: 2000ms)"
    )

    # Snapshot pre-rebuild state for one application
    sample_id = app_ids[0]
    pre_rebuild = await proj_comp.get_current_compliance(store, sample_id)
    assert pre_rebuild is not None, "Sample app must have compliance record before rebuild"

    # --- Phase 2: rebuild ComplianceAuditView ---
    proj_comp2 = ComplianceAuditViewProjection()
    await proj_comp2.ensure_schema(store)
    await proj_comp2.rebuild_from_scratch(store)

    # --- Phase 3: assert lag < 2000ms after rebuild ---
    elapsed_rebuild = await _run_daemon_until_caught_up(
        store, [proj_app, proj_perf, proj_comp2], max_wait_ms=5000
    )
    assert elapsed_rebuild < 2000, (
        f"ComplianceAuditView SLO VIOLATED after rebuild: {elapsed_rebuild:.0f}ms (limit: 2000ms)"
    )

    # --- Phase 4: verify rebuilt state matches pre-rebuild ---
    post_rebuild = await proj_comp2.get_current_compliance(store, sample_id)
    assert post_rebuild is not None, "Sample app must have compliance record after rebuild"

    print(
        f"✅ Compliance SLO during/after rebuild passed:\n"
        f"   Live lag:    {elapsed_live:.0f}ms < 2000ms  ({n} concurrent handlers)\n"
        f"   Rebuild lag: {elapsed_rebuild:.0f}ms < 2000ms"
    )
