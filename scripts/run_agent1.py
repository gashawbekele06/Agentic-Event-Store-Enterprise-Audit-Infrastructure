"""Run Agent 1 — DocumentProcessingAgent end-to-end."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv()

from src.event_store import EventStore
from src.commands.handlers import SubmitApplicationCommand, handle_submit_application
from src.commands.document_processing import ProcessDocumentsCommand, handle_process_documents


async def main():
    store = await EventStore.create(os.getenv("DATABASE_URL"))

    app_id = "APEX-DOC-TEST-03"

    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=app_id,
            applicant_id="COMP-001",
            requested_amount_usd=500000.0,
            loan_purpose="Working capital",
            submission_channel="portal",
        ),
        store,
    )
    print("✓ Application submitted:", app_id)

    result = await handle_process_documents(
        ProcessDocumentsCommand(
            application_id=app_id,
            agent_id="doc-agent-01",
            documents_dir="documents/COMP-001",
        ),
        store,
    )

    print("✓ docpkg stream:     ", result["docpkg_stream"])
    print("  overall_confidence:", result["quality"]["overall_confidence"])
    print("  is_coherent:       ", result["quality"]["is_coherent"])
    print("  critical_missing:  ", result["critical_missing_fields"])
    print("  reextraction:      ", result["reextraction_recommended"])
    print("  auditor_notes:     ", result["quality"]["auditor_notes"])
    print("✓ credit trigger:    ", result["credit_analysis_requested"])

    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
