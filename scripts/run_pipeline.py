"""
Full Apex Loan Pipeline — all 5 agents in sequence.

Usage:
    uv run python scripts/run_pipeline.py
    uv run python scripts/run_pipeline.py --app APEX-PIPE-001 --company COMP-005 --amount 750000

Flow:
    Submit → Agent1 (docs) → Agent2 (credit) → Agent3 (fraud)
           → Agent4 (compliance) → Agent5 (decision)
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv()

from src.event_store import EventStore
from src.registry.client import ApplicantRegistryClient
from src.commands.handlers import (
    SubmitApplicationCommand, handle_submit_application,
    UploadDocumentsCommand, handle_upload_documents,
)
from src.commands.document_processing import ProcessDocumentsCommand, handle_process_documents
from src.commands.credit_analysis import RunCreditAnalysisCommand, handle_credit_analysis
from src.commands.fraud_detection import RunFraudDetectionCommand, handle_fraud_detection
from src.commands.compliance_agent import RunComplianceCommand, handle_compliance_check
from src.commands.decision_orchestrator import RunOrchestratorCommand, handle_orchestrator_decision


def _separator(title: str) -> None:
    print(f"\n{'─' * 55}")
    print(f"  {title}")
    print(f"{'─' * 55}")


async def run_pipeline(
    app_id: str,
    company_id: str,
    amount: float,
    documents_dir: str,
) -> dict:

    store    = await EventStore.create(os.getenv("DATABASE_URL"))
    registry = await ApplicantRegistryClient.create(os.getenv("DATABASE_URL"))

    results = {}

    try:
        # ── Submit ────────────────────────────────────────────────────────────
        _separator("SUBMIT APPLICATION")
        await handle_submit_application(
            SubmitApplicationCommand(
                application_id=app_id,
                applicant_id=company_id,
                requested_amount_usd=amount,
                loan_purpose="Commercial working capital",
                submission_channel="portal",
            ),
            store,
        )
        print(f"  app_id:     {app_id}")
        print(f"  company_id: {company_id}")
        print(f"  amount:     ${amount:,.0f}")

        # ── Upload documents (records file paths as events) ───────────────────
        _separator("UPLOAD DOCUMENTS")
        registered = await handle_upload_documents(
            UploadDocumentsCommand(
                application_id=app_id,
                company_id=company_id,
                documents_base_dir=str(Path(documents_dir).parent),
            ),
            store,
        )
        for f in registered:
            print(f"  registered: {f}")

        # ── Agent 1: Document Processing ──────────────────────────────────────
        _separator("AGENT 1 — DocumentProcessingAgent")
        r1 = await handle_process_documents(
            ProcessDocumentsCommand(
                application_id=app_id,
                agent_id="doc-agent-01",
            ),
            store,
        )
        results["agent1"] = r1
        print(f"  docpkg_stream:      {r1['docpkg_stream']}")
        print(f"  overall_confidence: {r1['quality']['overall_confidence']}")
        print(f"  is_coherent:        {r1['quality']['is_coherent']}")
        print(f"  critical_missing:   {r1['critical_missing_fields']}")
        print(f"  reextraction:       {r1['reextraction_recommended']}")
        print(f"  auditor_notes:      {r1['quality']['auditor_notes']}")

        # ── Agent 2: Credit Analysis ───────────────────────────────────────────
        _separator("AGENT 2 — CreditAnalysisAgent")
        r2 = await handle_credit_analysis(
            RunCreditAnalysisCommand(
                application_id=app_id,
                applicant_id=company_id,
                agent_id="credit-agent-01",
                requested_amount_usd=amount,
            ),
            store,
            registry,
        )
        results["agent2"] = r2
        print(f"  risk_tier:             {r2['risk_tier']}")
        print(f"  confidence_score:      {r2['confidence_score']}")
        print(f"  recommended_limit_usd: ${r2['recommended_limit_usd']:,.0f}")
        print(f"  rationale:             {r2['rationale']}")
        if r2["data_quality_caveats"]:
            for c in r2["data_quality_caveats"]:
                print(f"  caveat: {c}")

        # ── Agent 3: Fraud Detection ───────────────────────────────────────────
        _separator("AGENT 3 — FraudDetectionAgent")
        r3 = await handle_fraud_detection(
            RunFraudDetectionCommand(
                application_id=app_id,
                applicant_id=company_id,
                agent_id="fraud-agent-01",
            ),
            store,
            registry,
        )
        results["agent3"] = r3
        print(f"  fraud_score:     {r3['fraud_score']}")
        print(f"  risk_level:      {r3['risk_level']}")
        print(f"  anomalies_found: {r3['anomalies_found']}")
        print(f"  recommendation:  {r3['recommendation']}")
        for a in r3["anomalies"]:
            print(f"    [{a['anomaly_type']}] {a['description']}")

        # ── Agent 4: Compliance ────────────────────────────────────────────────
        _separator("AGENT 4 — ComplianceAgent")
        r4 = await handle_compliance_check(
            RunComplianceCommand(
                application_id=app_id,
                applicant_id=company_id,
                agent_id="compliance-agent-01",
                requested_amount_usd=amount,
            ),
            store,
            registry,
        )
        results["agent4"] = r4
        print(f"  overall_verdict: {r4['overall_verdict']}")
        print(f"  has_hard_block:  {r4['has_hard_block']}")
        print(f"  rules:           {r4['rules_passed']}/{r4['rules_evaluated']} passed")
        print(f"  summary:         {r4['summary']}")

        if r4["has_hard_block"]:
            print(f"\n  ⛔ HARD BLOCK: {r4['block_reason']}")
            print("  → ApplicationDeclined written. Pipeline ends here.")
            results["final_state"] = "DECLINED_COMPLIANCE"
            return results

        # ── Agent 5: Decision Orchestrator ─────────────────────────────────────
        _separator("AGENT 5 — DecisionOrchestratorAgent")
        r5 = await handle_orchestrator_decision(
            RunOrchestratorCommand(
                application_id=app_id,
                agent_id="orchestrator-agent-01",
                requested_amount_usd=amount,
            ),
            store,
        )
        results["agent5"] = r5
        print(f"  recommendation:   {r5['recommendation']}")
        print(f"  confidence_score: {r5['confidence_score']}")
        print(f"  override_applied: {r5['override_applied']}")
        if r5["override_reason"]:
            print(f"  override_reason:  {r5['override_reason']}")
        print(f"  final_state:      {r5['final_state']}")
        print(f"  executive_summary: {r5['executive_summary']}")

        results["final_state"] = r5["final_state"]

    finally:
        await store.close()
        await registry.close()

    _separator("PIPELINE COMPLETE")
    print(f"  application_id: {app_id}")
    print(f"  final_state:    {results.get('final_state', 'UNKNOWN')}")

    return results


def parse_args():
    import uuid
    default_app = f"APEX-PIPE-{uuid.uuid4().hex[:6].upper()}"
    parser = argparse.ArgumentParser(description="Run full Apex loan pipeline")
    parser.add_argument("--app",     default=default_app,  help="Application ID (auto-generated if omitted)")
    parser.add_argument("--company", default="COMP-002",   help="Company ID")
    parser.add_argument("--amount",  default=500_000.0, type=float, help="Requested amount USD")
    parser.add_argument("--docs",    default=None,         help="Documents directory (default: documents/{company})")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    docs_dir = args.docs or f"documents/{args.company}"
    asyncio.run(run_pipeline(args.app, args.company, args.amount, docs_dir))
