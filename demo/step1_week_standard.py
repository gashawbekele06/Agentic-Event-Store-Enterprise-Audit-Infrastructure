"""
DEMO STEP 1 — The Week Standard
================================
"Show me the complete decision history of application ID X"

Drives a full loan lifecycle through MCP-style command handlers,
then queries the complete audit trail. Must complete in < 60 seconds.
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
import asyncio
import time
import uuid
from datetime import datetime, timezone

import asyncpg

from src.event_store import EventStore
from src.commands.handlers import (
    SubmitApplicationCommand, RequestCreditAnalysisCommand,
    StartAgentSessionCommand, CreditAnalysisCompletedCommand,
    FraudScreeningCompletedCommand, StartComplianceReviewCommand,
    RecordComplianceCheckCommand, GenerateDecisionCommand,
    HumanReviewCompletedCommand, ApproveApplicationCommand,
    handle_submit_application, handle_request_credit_analysis,
    handle_start_agent_session, handle_credit_analysis_completed,
    handle_fraud_screening_completed, handle_start_compliance_review,
    handle_record_compliance_check, handle_generate_decision,
    handle_human_review_completed, handle_approve_application,
)
from src.integrity.audit_chain import run_integrity_check

DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/apex_ledger"
SEP = "─" * 70


def header(title: str) -> None:
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


async def main() -> None:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)
    store = EventStore(pool)

    wall_start = time.monotonic()

    app_id        = str(uuid.uuid4())
    corr_id       = str(uuid.uuid4())
    credit_agent  = f"credit-agent-{uuid.uuid4().hex[:6]}"
    credit_sess   = f"credit-sess-{uuid.uuid4().hex[:6]}"
    fraud_agent   = f"fraud-agent-{uuid.uuid4().hex[:6]}"
    fraud_sess    = f"fraud-sess-{uuid.uuid4().hex[:6]}"
    orch_agent    = f"orch-agent-{uuid.uuid4().hex[:6]}"
    orch_sess     = f"orch-sess-{uuid.uuid4().hex[:6]}"

    header("APEX FINANCIAL SERVICES — LEDGER DEMO")
    print(f"  Application ID : {app_id}")
    print(f"  Correlation ID : {corr_id}")

    # ── 1. Start agent sessions (Gas Town anchors) ─────────────────────
    header("PHASE 1 · Starting Agent Sessions (Gas Town anchors)")
    for agent_id, sess_id, model in [
        (credit_agent, credit_sess, "credit-model-v2.3"),
        (fraud_agent,  fraud_sess,  "fraud-model-v1.5"),
        (orch_agent,   orch_sess,   "orchestrator-v3.0"),
    ]:
        await handle_start_agent_session(
            StartAgentSessionCommand(
                agent_id=agent_id, session_id=sess_id,
                context_source="fresh", model_version=model,
            ), store,
        )
        print(f"  ✓ Session started — agent={agent_id}  model={model}")

    # ── 2. Submit application ──────────────────────────────────────────
    header("PHASE 2 · Application Submission")
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=app_id, applicant_id="apex-corp-001",
            requested_amount_usd=2_500_000.0,
            loan_purpose="commercial_real_estate",
            submission_channel="api", correlation_id=corr_id,
        ), store,
    )
    print(f"  ✓ ApplicationSubmitted  amount=$2,500,000  channel=api")

    await handle_request_credit_analysis(
        RequestCreditAnalysisCommand(
            application_id=app_id, assigned_agent_id=credit_agent,
            priority="high", correlation_id=corr_id,
        ), store,
    )
    print(f"  ✓ CreditAnalysisRequested  agent={credit_agent}")

    # ── 3. Credit analysis ────────────────────────────────────────────
    header("PHASE 3 · Credit Analysis (Agent)")
    await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=app_id, agent_id=credit_agent,
            session_id=credit_sess, model_version="credit-model-v2.3",
            confidence_score=0.87, risk_tier="LOW",
            recommended_limit_usd=2_500_000.0,
            duration_ms=1_240, input_data={"financials": "q4-2025"},
            correlation_id=corr_id,
        ), store,
    )
    print(f"  ✓ CreditAnalysisCompleted  risk=LOW  confidence=0.87  limit=$2,500,000")

    # ── 4. Fraud screening ────────────────────────────────────────────
    header("PHASE 4 · Fraud Screening (Agent)")
    await handle_fraud_screening_completed(
        FraudScreeningCompletedCommand(
            application_id=app_id, agent_id=fraud_agent,
            session_id=fraud_sess, fraud_score=0.04,
            anomaly_flags=[], screening_model_version="fraud-model-v1.5",
            correlation_id=corr_id,
        ), store,
    )
    print(f"  ✓ FraudScreeningCompleted  score=0.04  flags=[]  (clean)")

    # ── 5. Compliance checks ──────────────────────────────────────────
    header("PHASE 5 · Compliance Review")
    await handle_start_compliance_review(
        StartComplianceReviewCommand(
            application_id=app_id,
            checks_required=["KYC-001", "AML-002", "BSA-003", "DORA-101"],
            regulation_set_version="reg-v2026-q1",
            correlation_id=corr_id,
        ), store,
    )
    print(f"  ✓ ComplianceReviewStarted  checks=[KYC-001, AML-002, BSA-003, DORA-101]")

    checks = [("KYC-001", "v3"), ("AML-002", "v2"), ("BSA-003", "v1"), ("DORA-101", "v1")]
    for rule_id, rule_ver in checks:
        await handle_record_compliance_check(
            RecordComplianceCheckCommand(
                application_id=app_id, rule_id=rule_id,
                rule_version=rule_ver, passed=True,
                evidence_hash=f"sha256-{rule_id.lower()}", correlation_id=corr_id,
            ), store,
        )
        print(f"  ✓ ComplianceRulePassed  rule={rule_id}  version={rule_ver}")

    # ── 6. Decision ───────────────────────────────────────────────────
    header("PHASE 6 · AI Orchestrator Decision")
    credit_stream = f"agent-{credit_agent}-{credit_sess}"
    await handle_generate_decision(
        GenerateDecisionCommand(
            application_id=app_id, orchestrator_agent_id=orch_agent,
            recommendation="APPROVE", confidence_score=0.91,
            contributing_agent_sessions=[credit_stream],
            decision_basis_summary=(
                "Low credit risk, clean fraud screen, "
                "all 4 compliance rules passed under reg-v2026-q1."
            ),
            model_versions={"credit": "credit-model-v2.3", "fraud": "fraud-model-v1.5"},
            correlation_id=corr_id,
        ), store,
    )
    print(f"  ✓ DecisionGenerated  recommendation=APPROVE  confidence=0.91")

    # ── 7. Human review + approval ────────────────────────────────────
    header("PHASE 7 · Human Review & Formal Approval")
    await handle_human_review_completed(
        HumanReviewCompletedCommand(
            application_id=app_id, reviewer_id="loan-officer-007",
            override=False, final_decision="APPROVE", correlation_id=corr_id,
        ), store,
    )
    print(f"  ✓ HumanReviewCompleted  reviewer=loan-officer-007  override=False")

    await handle_approve_application(
        ApproveApplicationCommand(
            application_id=app_id, approved_amount_usd=2_500_000.0,
            interest_rate=0.0625, conditions=["collateral_verified", "board_resolution"],
            approved_by="loan-officer-007",
        ), store,
    )
    print(f"  ✓ ApplicationApproved  amount=$2,500,000  rate=6.25%")

    # ── 8. Query complete event history ───────────────────────────────
    header("PHASE 8 · COMPLETE DECISION HISTORY (The Week Standard)")
    events = await store.load_stream(f"loan-{app_id}")
    print(f"\n  {'#':<4} {'Event Type':<32} {'Stream Pos':<12} {'Global Pos':<12} Timestamp")
    print(f"  {'─'*4} {'─'*32} {'─'*12} {'─'*12} {'─'*26}")
    for ev in events:
        print(
            f"  {ev.stream_position:<4} {ev.event_type:<32} "
            f"{ev.stream_position:<12} {ev.global_position:<12} "
            f"{ev.recorded_at.strftime('%H:%M:%S.%f')}"
        )

    # ── 9. Causal links ───────────────────────────────────────────────
    header("PHASE 9 · Causal Link Verification")
    decision_event = next((e for e in events if e.event_type == "DecisionGenerated"), None)
    if decision_event:
        sessions = decision_event.payload.get("contributing_agent_sessions", [])
        print(f"  DecisionGenerated references {len(sessions)} contributing session(s):")
        for s in sessions:
            print(f"    → {s}")
        print(f"  Recommendation : {decision_event.payload['recommendation']}")
        print(f"  Confidence     : {decision_event.payload['confidence_score']}")
        print(f"  Model versions : {decision_event.payload.get('model_versions', {})}")

    # ── 10. Cryptographic integrity ───────────────────────────────────
    header("PHASE 10 · Cryptographic Audit Chain Integrity")
    result = await run_integrity_check(store, "loan", app_id)
    print(f"  Events verified   : {result.events_verified_count}")
    print(f"  Chain valid       : {result.chain_valid}")
    print(f"  Tamper detected   : {result.tamper_detected}")
    print(f"  Integrity hash    : {result.integrity_hash[:32]}…")

    elapsed = (time.monotonic() - wall_start) * 1000
    header("RESULT")
    print(f"  Total events in stream : {len(events)}")
    print(f"  Final state            : FINAL_APPROVED")
    print(f"  Chain integrity        : {'✅ VALID' if result.chain_valid else '❌ BROKEN'}")
    print(f"\n  ⏱  Completed in {elapsed:.0f} ms  (limit: 60 000 ms)")
    assert elapsed < 60_000, f"FAILED — took {elapsed:.0f}ms, limit is 60,000ms"
    print(f"\n  ✅ WEEK STANDARD MET — full audit trail in {elapsed:.0f} ms\n")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
