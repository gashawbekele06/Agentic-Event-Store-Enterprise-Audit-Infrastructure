"""Run Agent 2 — CreditAnalysisAgent.
Prerequisite: run_agent1.py must have completed for the same app_id.
"""
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv()

from src.event_store import EventStore
from src.registry.client import ApplicantRegistryClient
from src.commands.credit_analysis import RunCreditAnalysisCommand, handle_credit_analysis

APP_ID      = "APEX-DOC-TEST-03"   # must match run_agent1.py
APPLICANT   = "COMP-001"
AMOUNT      = 500_000.0
AGENT_ID    = "credit-agent-01"


async def main():
    store    = await EventStore.create(os.getenv("DATABASE_URL"))
    registry = await ApplicantRegistryClient.create(os.getenv("DATABASE_URL"))

    result = await handle_credit_analysis(
        RunCreditAnalysisCommand(
            application_id=APP_ID,
            applicant_id=APPLICANT,
            agent_id=AGENT_ID,
            requested_amount_usd=AMOUNT,
        ),
        store,
        registry,
    )

    print("✓ CreditAnalysisCompleted")
    print("  risk_tier:            ", result["risk_tier"])
    print("  confidence_score:     ", result["confidence_score"])
    print("  recommended_limit_usd:", result["recommended_limit_usd"])
    print("  rationale:            ", result["rationale"])
    print("  caveats:              ", result["data_quality_caveats"])
    print("  duration_ms:          ", result["duration_ms"])

    await store.close()
    await registry.close()


if __name__ == "__main__":
    asyncio.run(main())
