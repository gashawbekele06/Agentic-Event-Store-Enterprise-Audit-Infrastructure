"""Run Agent 3 — FraudDetectionAgent.
Prerequisite: run_agent1.py must have completed for the same app_id.
"""
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv()

from src.event_store import EventStore
from src.registry.client import ApplicantRegistryClient
from src.commands.fraud_detection import RunFraudDetectionCommand, handle_fraud_detection

APP_ID    = "APEX-DOC-TEST-03"   # must match run_agent1.py
APPLICANT = "COMP-001"
AGENT_ID  = "fraud-agent-01"


async def main():
    store    = await EventStore.create(os.getenv("DATABASE_URL"))
    registry = await ApplicantRegistryClient.create(os.getenv("DATABASE_URL"))

    result = await handle_fraud_detection(
        RunFraudDetectionCommand(
            application_id=APP_ID,
            applicant_id=APPLICANT,
            agent_id=AGENT_ID,
        ),
        store,
        registry,
    )

    print("✓ FraudScreeningCompleted")
    print("  fraud_score:     ", result["fraud_score"])
    print("  risk_level:      ", result["risk_level"])
    print("  anomalies_found: ", result["anomalies_found"])
    print("  prior_default:   ", result["prior_default"])
    print("  recommendation:  ", result["recommendation"])
    for a in result["anomalies"]:
        print(f"    [{a['anomaly_type']}] {a['description']} (severity={a['severity']})")

    await store.close()
    await registry.close()


if __name__ == "__main__":
    asyncio.run(main())
