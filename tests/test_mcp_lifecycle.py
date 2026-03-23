"""
test_mcp_lifecycle.py — Full loan application lifecycle via MCP tool calls only.

This test simulates exactly what a real AI agent would do:
  1. start_agent_session (Gas Town anchor)
  2. submit_application
  3. request_credit_analysis
  4. record_credit_analysis
  5. record_fraud_screening
  6. start_compliance_review
  7. record_compliance_check (x2, both pass)
  8. generate_decision
  9. record_human_review
  10. approve_application
  11. Query ledger://applications/{id} → verify FINAL_APPROVED state
  12. Query ledger://applications/{id}/compliance → verify compliance trace
  13. Query ledger://applications/{id}/audit-trail → verify full event log

NO direct Python function calls are used after setup.
All interactions go through the MCP tool functions.
"""
from __future__ import annotations

import uuid

import pytest

from src.event_store import EventStore
from src.mcp.resources import (
    resource_application,
    resource_audit_trail,
    resource_compliance,
    resource_ledger_health,
)
from src.mcp.tools import (
    tool_approve_application,
    tool_generate_decision,
    tool_record_compliance_check,
    tool_record_credit_analysis,
    tool_record_fraud_screening,
    tool_request_credit_analysis,
    tool_run_integrity_check,
    tool_start_agent_session,
    tool_start_compliance_review,
    tool_submit_application,
    tool_record_human_review,
)
from src.projections import ProjectionDaemon
from src.projections.agent_performance import AgentPerformanceLedgerProjection
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.compliance_audit import ComplianceAuditViewProjection


async def _ensure_projections(store: EventStore):
    """Set up projection tables and return instances."""
    proj_app = ApplicationSummaryProjection()
    proj_perf = AgentPerformanceLedgerProjection()
    proj_comp = ComplianceAuditViewProjection()
    await proj_app.ensure_schema(store)
    await proj_perf.ensure_schema(store)
    await proj_comp.ensure_schema(store)
    return {
        "application_summary": proj_app,
        "agent_performance": proj_perf,
        "compliance_audit": proj_comp,
    }


async def _pump_projections(store: EventStore, projections: dict, batches: int = 5):
    """Pump the daemon for N batches to catch up."""
    daemon = ProjectionDaemon(store, list(projections.values()))
    for _ in range(batches):
        await daemon._process_batch()


@pytest.mark.asyncio
async def test_full_lifecycle_via_mcp_tools(store: EventStore):
    """
    Complete loan application lifecycle driven entirely through MCP tool calls.
    ApplicationSubmitted → FinalApproved with compliance + human review.
    """
    projections = await _ensure_projections(store)

    # IDs
    app_id = str(uuid.uuid4())
    credit_agent_id = f"credit-{uuid.uuid4().hex[:8]}"
    credit_session_id = f"csess-{uuid.uuid4().hex[:8]}"
    fraud_agent_id = f"fraud-{uuid.uuid4().hex[:8]}"
    fraud_session_id = f"fsess-{uuid.uuid4().hex[:8]}"
    orch_agent_id = f"orch-{uuid.uuid4().hex[:8]}"
    orch_session_id = f"osess-{uuid.uuid4().hex[:8]}"
    correlation_id = str(uuid.uuid4())

    # === STEP 1: Start credit agent session (Gas Town) ===
    r = await tool_start_agent_session(store, {
        "agent_id": credit_agent_id, "session_id": credit_session_id,
        "context_source": "fresh", "model_version": "credit-model-v2.3",
        "context_token_count": 1024,
    })
    assert r.get("success"), f"start_agent_session failed: {r}"
    assert r["session_id"] == credit_session_id

    # Start fraud agent session
    r = await tool_start_agent_session(store, {
        "agent_id": fraud_agent_id, "session_id": fraud_session_id,
        "context_source": "fresh", "model_version": "fraud-model-v1.5",
    })
    assert r.get("success"), f"start fraud session failed: {r}"

    # Start orchestrator session
    r = await tool_start_agent_session(store, {
        "agent_id": orch_agent_id, "session_id": orch_session_id,
        "context_source": "fresh", "model_version": "orchestrator-v3.0",
    })
    assert r.get("success"), f"start orch session failed: {r}"

    # === STEP 2: Submit application ===
    r = await tool_submit_application(store, {
        "application_id": app_id,
        "applicant_id": "apex-corp-001",
        "requested_amount_usd": 500_000.0,
        "loan_purpose": "commercial_real_estate",
        "submission_channel": "api",
        "correlation_id": correlation_id,
    })
    assert r.get("success"), f"submit_application failed: {r}"
    assert r["stream_id"] == f"loan-{app_id}"

    # === STEP 3: Request credit analysis ===
    r = await tool_request_credit_analysis(store, {
        "application_id": app_id,
        "assigned_agent_id": credit_agent_id,
        "priority": "high",
    })
    assert r.get("success"), f"request_credit_analysis failed: {r}"

    # === STEP 4: Record credit analysis ===
    r = await tool_record_credit_analysis(store, {
        "application_id": app_id,
        "agent_id": credit_agent_id,
        "session_id": credit_session_id,
        "model_version": "credit-model-v2.3",
        "risk_tier": "LOW",
        "recommended_limit_usd": 500_000.0,
        "confidence_score": 0.91,
        "duration_ms": 1150,
        "correlation_id": correlation_id,
    })
    assert r.get("success"), f"record_credit_analysis failed: {r}"

    # ===  STEP 5: Record fraud screening ===
    r = await tool_record_fraud_screening(store, {
        "application_id": app_id,
        "agent_id": fraud_agent_id,
        "session_id": fraud_session_id,
        "fraud_score": 0.08,
        "anomaly_flags": [],
        "screening_model_version": "fraud-model-v1.5",
        "correlation_id": correlation_id,
    })
    assert r.get("success"), f"record_fraud_screening failed: {r}"

    # === STEP 6: Start compliance review ===
    r = await tool_start_compliance_review(store, {
        "application_id": app_id,
        "checks_required": ["KYC-001", "AML-002", "BSA-003"],
        "regulation_set_version": "reg-v2026-q1",
    })
    assert r.get("success"), f"start_compliance_review failed: {r}"

    # === STEP 7: Record compliance checks (all pass) ===
    for rule_id, rule_version in [("KYC-001", "v3"), ("AML-002", "v2"), ("BSA-003", "v1")]:
        r = await tool_record_compliance_check(store, {
            "application_id": app_id,
            "rule_id": rule_id,
            "rule_version": rule_version,
            "passed": True,
            "evidence_hash": f"hash-{rule_id}",
            "correlation_id": correlation_id,
        })
        assert r.get("success"), f"record_compliance_check {rule_id} failed: {r}"
        assert r["compliance_status"] == "PASSED"

    # === STEP 8: Generate decision ===
    credit_stream_id = f"agent-{credit_agent_id}-{credit_session_id}"
    r = await tool_generate_decision(store, {
        "application_id": app_id,
        "orchestrator_agent_id": orch_agent_id,
        "recommendation": "APPROVE",
        "confidence_score": 0.88,
        "contributing_agent_sessions": [credit_stream_id],
        "decision_basis_summary": "Low risk, clean fraud screen, all compliance passed.",
        "model_versions": {"credit": "credit-model-v2.3", "fraud": "fraud-model-v1.5"},
        "correlation_id": correlation_id,
    })
    assert r.get("success"), f"generate_decision failed: {r}"

    # === STEP 9: Human review ===
    r = await tool_record_human_review(store, {
        "application_id": app_id,
        "reviewer_id": "loan-officer-007",
        "override": False,
        "final_decision": "APPROVE",
        "correlation_id": correlation_id,
    })
    assert r.get("success"), f"record_human_review failed: {r}"
    assert r["final_decision"] == "APPROVE"

    # === STEP 10: Approve application ===
    r = await tool_approve_application(store, {
        "application_id": app_id,
        "approved_amount_usd": 500_000.0,
        "interest_rate": 0.065,
        "conditions": ["collateral_verified"],
        "approved_by": "loan-officer-007",
    })
    assert r.get("success"), f"approve_application failed: {r}"
    assert r["final_state"] == "FINAL_APPROVED"

    # === STEP 11: Pump projections then verify via resources ===
    await _pump_projections(store, projections, batches=10)

    # Query application resource
    app_resource = await resource_application(store, app_id, projections)
    assert "application" in app_resource, f"resource_application error: {app_resource}"
    app_data = app_resource["application"]
    assert app_data["state"] == "FINAL_APPROVED", (
        f"Expected FINAL_APPROVED, got {app_data['state']}"
    )
    assert float(app_data["approved_amount_usd"]) == 500_000.0

    # === STEP 12: Query compliance ===
    comp_resource = await resource_compliance(store, app_id, projections)
    assert "checks" in comp_resource, f"compliance resource error: {comp_resource}"
    checks = comp_resource["checks"]
    passed_ids = {c["rule_id"] for c in checks if c["status"] == "PASSED"}
    for rule_id in ["KYC-001", "AML-002", "BSA-003"]:
        assert rule_id in passed_ids, f"{rule_id} not in compliance checks: {checks}"

    # === STEP 13: Query audit trail ===
    audit_resource = await resource_audit_trail(store, app_id)
    assert "events" in audit_resource, f"audit trail error: {audit_resource}"
    event_types = {e["event_type"] for e in audit_resource["events"]}
    assert "ApplicationSubmitted" in event_types
    assert "CreditAnalysisCompleted" in event_types
    assert "DecisionGenerated" in event_types
    assert "ApplicationApproved" in event_types

    print("✅ Full MCP lifecycle test passed!")
    print(f"   Application: {app_id}")
    print(f"   Final state: {app_data['state']}")
    print(f"   Events in audit trail: {audit_resource['event_count']}")
    print(f"   Compliance checks passed: {sorted(passed_ids)}")


@pytest.mark.asyncio
async def test_mcp_structured_error_on_wrong_state(store: EventStore):
    """
    MCP tools must return structured errors (not exceptions) when called
    out of order. Validates that domain errors are surfaced correctly to LLMs.
    """
    app_id = str(uuid.uuid4())
    orch_agent_id = f"orch-{uuid.uuid4().hex[:8]}"

    # Try to generate a decision without submitting an application first
    r = await tool_generate_decision(store, {
        "application_id": app_id,
        "orchestrator_agent_id": orch_agent_id,
        "recommendation": "APPROVE",
        "confidence_score": 0.9,
    })

    # Should return structured error, not raise
    assert "error_type" in r, "Expected structured error response"
    assert r["error_type"] in ("StreamNotFoundError", "DomainError"), (
        f"Unexpected error type: {r['error_type']}"
    )
    assert "suggested_action" in r, "Error must include suggested_action for LLM"
    print(f"✅ Structured error test passed: {r['error_type']} - {r.get('message')}")


@pytest.mark.asyncio
async def test_mcp_confidence_floor_override(store: EventStore):
    """
    When confidence_score < 0.6, recommendation must be REFER regardless of input.
    This regulatory floor is enforced by the domain, not the API layer.
    """
    projections = await _ensure_projections(store)

    app_id = str(uuid.uuid4())
    credit_agent_id = f"credit-floor-{uuid.uuid4().hex[:8]}"
    credit_session_id = f"csess-floor-{uuid.uuid4().hex[:8]}"
    orch_agent_id = f"orch-floor-{uuid.uuid4().hex[:8]}"
    orch_session_id = f"osess-floor-{uuid.uuid4().hex[:8]}"

    # Full setup through compliance review
    await tool_start_agent_session(store, {
        "agent_id": credit_agent_id, "session_id": credit_session_id,
        "context_source": "fresh", "model_version": "credit-v2.3",
    })
    await tool_start_agent_session(store, {
        "agent_id": orch_agent_id, "session_id": orch_session_id,
        "context_source": "fresh", "model_version": "orch-v3.0",
    })
    await tool_submit_application(store, {
        "application_id": app_id, "applicant_id": "floor-test",
        "requested_amount_usd": 100_000.0, "loan_purpose": "test",
        "submission_channel": "api",
    })
    await tool_request_credit_analysis(store, {
        "application_id": app_id, "assigned_agent_id": credit_agent_id,
    })
    await tool_record_credit_analysis(store, {
        "application_id": app_id, "agent_id": credit_agent_id,
        "session_id": credit_session_id, "model_version": "credit-v2.3",
        "risk_tier": "HIGH", "recommended_limit_usd": 80_000.0,
        "confidence_score": 0.95,
    })
    await tool_start_compliance_review(store, {
        "application_id": app_id, "checks_required": ["KYC-001"],
        "regulation_set_version": "reg-v2026",
    })
    await tool_record_compliance_check(store, {
        "application_id": app_id, "rule_id": "KYC-001", "rule_version": "v3",
        "passed": True,
    })

    # Generate decision with confidence < 0.6 but recommendation=APPROVE
    credit_stream = f"agent-{credit_agent_id}-{credit_session_id}"
    r = await tool_generate_decision(store, {
        "application_id": app_id,
        "orchestrator_agent_id": orch_agent_id,
        "recommendation": "APPROVE",   # should be overridden to REFER
        "confidence_score": 0.45,       # below 0.6 floor
        "contributing_agent_sessions": [credit_stream],
        "decision_basis_summary": "Low confidence test",
    })

    assert r.get("success"), f"generate_decision failed: {r}"

    # Verify final decision is REFER (domain rule enforced)
    events = await store.load_stream(f"loan-{app_id}")
    decision_events = [e for e in events if e.event_type == "DecisionGenerated"]
    assert len(decision_events) == 1
    assert decision_events[0].payload["recommendation"] == "REFER", (
        f"Confidence floor not enforced: {decision_events[0].payload['recommendation']}"
    )

    print("✅ Confidence floor test passed: APPROVE → REFER at confidence=0.45")


@pytest.mark.asyncio
async def test_integrity_check_via_mcp(store: EventStore):
    """Run integrity check through MCP tool and verify structured result."""
    app_id = str(uuid.uuid4())

    # Submit a basic application first
    await tool_submit_application(store, {
        "application_id": app_id, "applicant_id": "integrity-test",
        "requested_amount_usd": 50_000.0, "loan_purpose": "test",
        "submission_channel": "api",
    })

    r = await tool_run_integrity_check(store, {
        "entity_type": "loan",
        "entity_id": app_id,
    })

    assert r.get("success"), f"run_integrity_check failed: {r}"
    assert r["chain_valid"] is True
    assert r["tamper_detected"] is False
    assert r["events_verified_count"] >= 1
    assert "integrity_hash" in r

    print(f"✅ Integrity check via MCP passed: {r['events_verified_count']} events verified,"
          f" chain_valid={r['chain_valid']}")
