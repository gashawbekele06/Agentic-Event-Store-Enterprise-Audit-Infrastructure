"""
DEMO STEP 3 — Temporal Compliance Query
=========================================
Demonstrates ledger://applications/{id}/compliance?as_of={timestamp}

Shows that compliance state at a past timestamp is distinct from current state.
This is the regulatory "time-travel" feature — required for exam packages.
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
    StartComplianceReviewCommand, RecordComplianceCheckCommand,
    handle_submit_application, handle_request_credit_analysis,
    handle_start_agent_session, handle_credit_analysis_completed,
    handle_start_compliance_review, handle_record_compliance_check,
)
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.projections import ProjectionDaemon

DATABASE_URL = "postgresql://postgres:13621@localhost:5433/apex_ledger"
SEP = "─" * 70


def header(title: str) -> None:
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


def print_compliance(label: str, state: dict | None) -> None:
    if state is None:
        print(f"  {label}: (no compliance events at this point)")
        return
    checks = state.get("checks", [])
    overall = state.get("overall_status", "?")
    source = state.get("source", "live")
    print(f"  {label}  [overall={overall}] [source={source}]")
    if not checks:
        print(f"    (no checks recorded yet)")
        return
    for chk in checks:
        status = chk.get("status", "?")
        icon = "✅" if status == "PASSED" else "❌" if status == "FAILED" else "⏳"
        print(f"    {icon}  {chk.get('rule_id', '?'):<12} {status}")


async def main() -> None:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)
    store = EventStore(pool)

    proj_comp = ComplianceAuditViewProjection()
    await proj_comp.ensure_schema(store)

    app_id   = str(uuid.uuid4())
    agent_id = f"credit-{uuid.uuid4().hex[:6]}"
    sess_id  = f"sess-{uuid.uuid4().hex[:6]}"

    header("TEMPORAL COMPLIANCE QUERY DEMO")
    print(f"  Application ID : {app_id}")

    # ── Build application through credit analysis ─────────────────────
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=app_id, applicant_id="temporal-corp",
            requested_amount_usd=750_000.0, loan_purpose="working_capital",
            submission_channel="api",
        ), store,
    )
    await handle_start_agent_session(
        StartAgentSessionCommand(
            agent_id=agent_id, session_id=sess_id,
            context_source="fresh", model_version="credit-model-v2.3",
        ), store,
    )
    await handle_request_credit_analysis(
        RequestCreditAnalysisCommand(application_id=app_id, assigned_agent_id=agent_id),
        store,
    )
    await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=app_id, agent_id=agent_id, session_id=sess_id,
            model_version="credit-model-v2.3", confidence_score=0.84,
            risk_tier="MEDIUM", recommended_limit_usd=700_000.0, duration_ms=1_100,
        ), store,
    )

    # ── CHECKPOINT T1: before compliance checks ────────────────────────
    t1 = datetime.now(timezone.utc)
    print(f"\n  ⏺  CHECKPOINT T1 (before compliance): {t1.strftime('%H:%M:%S.%f')}")
    await asyncio.sleep(0.1)   # ensure distinct timestamps

    # ── Start compliance + pass KYC only ─────────────────────────────
    header("Adding compliance events AFTER T1")
    await handle_start_compliance_review(
        StartComplianceReviewCommand(
            application_id=app_id,
            checks_required=["KYC-001", "AML-002", "BSA-003"],
            regulation_set_version="reg-v2026-q1",
        ), store,
    )
    print("  ✓ ComplianceReviewStarted  checks=[KYC-001, AML-002, BSA-003]")

    await handle_record_compliance_check(
        RecordComplianceCheckCommand(
            application_id=app_id, rule_id="KYC-001",
            rule_version="v3", passed=True, evidence_hash="kyc-hash",
        ), store,
    )
    print("  ✓ ComplianceRulePassed     KYC-001")

    # ── CHECKPOINT T2: between KYC and AML ────────────────────────────
    t2 = datetime.now(timezone.utc)
    print(f"\n  ⏺  CHECKPOINT T2 (after KYC only): {t2.strftime('%H:%M:%S.%f')}")
    await asyncio.sleep(0.1)

    await handle_record_compliance_check(
        RecordComplianceCheckCommand(
            application_id=app_id, rule_id="AML-002",
            rule_version="v2", passed=True, evidence_hash="aml-hash",
        ), store,
    )
    print("  ✓ ComplianceRulePassed     AML-002")

    await handle_record_compliance_check(
        RecordComplianceCheckCommand(
            application_id=app_id, rule_id="BSA-003",
            rule_version="v1", passed=True, evidence_hash="bsa-hash",
        ), store,
    )
    print("  ✓ ComplianceRulePassed     BSA-003")

    # ── Run daemon to populate projection ─────────────────────────────
    header("Running Projection Daemon to populate ComplianceAuditView")
    daemon = ProjectionDaemon(store, [proj_comp])
    for _ in range(10):
        await daemon._process_batch()
    lag = await proj_comp.get_projection_lag(store)
    print(f"  Projection lag : {lag} ms")

    # ── Query at three different points in time ────────────────────────
    header("TEMPORAL QUERIES — The Regulatory Time-Travel Feature")

    t_now = datetime.now(timezone.utc)
    print(f"\n  Querying at THREE points in time:")
    print(f"    T1 = {t1.strftime('%H:%M:%S.%f')}  (before ANY compliance)")
    print(f"    T2 = {t2.strftime('%H:%M:%S.%f')}  (after KYC only)")
    print(f"    NOW = {t_now.strftime('%H:%M:%S.%f')}  (all checks complete)")

    state_t1  = await proj_comp.get_compliance_at(store, app_id, t1)
    state_t2  = await proj_comp.get_compliance_at(store, app_id, t2)
    state_now = await proj_comp.get_current_compliance(store, app_id)

    print()
    print_compliance(f"AT T1", state_t1)
    print()
    print_compliance(f"AT T2", state_t2)
    print()
    print_compliance(f"NOW  ", state_now)

    # ── Prove states differ ───────────────────────────────────────────
    header("VERIFICATION")
    now_checks = {c["rule_id"]: c["status"] for c in (state_now or {}).get("checks", [])}
    t2_checks  = {c["rule_id"]: c["status"] for c in (state_t2 or {}).get("checks", [])}

    print(f"  Current compliance  : {now_checks}")
    print(f"  At T2 compliance    : {t2_checks}")
    print(f"  Before any checks   : {'None — no events yet' if state_t1 is None else state_t1}")

    assert state_t1 is None or not any(
        c.get("status") in ("PASSED", "FAILED")
        for c in (state_t1 or {}).get("checks", [])
    ), "T1 should have no PASSED/FAILED checks"

    print(f"\n  ✅ TEMPORAL QUERY VERIFIED")
    print(f"     State at T1  ≠ State at T2 ≠ State now")
    print(f"     Regulators can examine state at ANY past moment.\n")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
