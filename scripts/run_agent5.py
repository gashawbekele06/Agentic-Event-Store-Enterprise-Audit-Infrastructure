"""Run Agent 5 — DecisionOrchestratorAgent.
Prerequisites: run_agent2.py, run_agent3.py, run_agent4.py must all have
completed for the same app_id (and compliance must NOT have hard-blocked).
"""
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv()

from src.event_store import EventStore
from src.commands.decision_orchestrator import RunOrchestratorCommand, handle_orchestrator_decision

APP_ID   = "APEX-DOC-TEST-03"   # must match all previous agents
AMOUNT   = 500_000.0
AGENT_ID = "orchestrator-agent-01"


async def main():
    store = await EventStore.create(os.getenv("DATABASE_URL"))

    result = await handle_orchestrator_decision(
        RunOrchestratorCommand(
            application_id=APP_ID,
            agent_id=AGENT_ID,
            requested_amount_usd=AMOUNT,
        ),
        store,
    )

    print("✓ DecisionGenerated")
    print("  recommendation:   ", result["recommendation"])
    print("  confidence_score: ", result["confidence_score"])
    print("  override_applied: ", result["override_applied"])
    if result["override_reason"]:
        print("  override_reason:  ", result["override_reason"])
    print("  final_state:      ", result["final_state"])
    print("  executive_summary:", result["executive_summary"])
    print("  key_risks:")
    for r in result["key_risks"]:
        print(f"    - {r}")

    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
