"""Run Agent 4 — ComplianceAgent.
Prerequisite: run_agent1.py must have completed for the same app_id.
"""
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv()

from src.event_store import EventStore
from src.registry.client import ApplicantRegistryClient
from src.commands.compliance_agent import RunComplianceCommand, handle_compliance_check

APP_ID    = "APEX-DOC-TEST-03"   # must match run_agent1.py
APPLICANT = "COMP-001"
AMOUNT    = 500_000.0
AGENT_ID  = "compliance-agent-01"


async def main():
    store    = await EventStore.create(os.getenv("DATABASE_URL"))
    registry = await ApplicantRegistryClient.create(os.getenv("DATABASE_URL"))

    result = await handle_compliance_check(
        RunComplianceCommand(
            application_id=APP_ID,
            applicant_id=APPLICANT,
            agent_id=AGENT_ID,
            requested_amount_usd=AMOUNT,
        ),
        store,
        registry,
    )

    print("✓ ComplianceCheckCompleted")
    print("  overall_verdict:  ", result["overall_verdict"])
    print("  has_hard_block:   ", result["has_hard_block"])
    print("  rules_evaluated:  ", result["rules_evaluated"])
    print("  rules_passed:     ", result["rules_passed"])
    print("  rules_failed:     ", result["rules_failed"])
    print("  summary:          ", result["summary"])
    if result["has_hard_block"]:
        print("  block_reason:     ", result["block_reason"])
        print("  → ApplicationDeclined written to loan stream")

    await store.close()
    await registry.close()


if __name__ == "__main__":
    asyncio.run(main())
