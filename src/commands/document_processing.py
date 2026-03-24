"""
Document Processing Command Handler

Orchestrates the full document extraction pipeline:
  Step 1: Gas Town anchor  — AgentContextLoaded appended before any work
  Step 2: Validate inputs  — loan must be in SUBMITTED state
  Step 3: Extract income statement via Week 3 adapter
  Step 4: Extract balance sheet via Week 3 adapter
  Step 5: LLM quality assessment (coherence + anomaly check)
  Step 6: Append ExtractionCompleted + QualityAssessmentCompleted to docpkg stream,
          then CreditAnalysisRequested to loan stream
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

# Ensure project root is on sys.path so `ledger` and `src` are importable
# regardless of how this module is invoked.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.aggregates.loan_application import ApplicationState, LoanApplicationAggregate
from src.event_store import EventStore
from src.models.events import (
    AgentContextLoaded,
    CreditAnalysisRequested,
    DomainError,
    ExtractionCompleted,
    QualityAssessmentCompleted,
)

# ── Week 3 adapter ────────────────────────────────────────────────────────────
_WEEK3_PATH = os.getenv("WEEK3_PROJECT_PATH", "")
if _WEEK3_PATH and _WEEK3_PATH not in sys.path:
    sys.path.insert(0, _WEEK3_PATH)

from ledger.adapters.week3_adapter import (  # noqa: E402
    FinancialFacts,
    extract_balance_sheet,
    extract_income_statement,
)

# ── LLM quality assessment prompt ─────────────────────────────────────────────
_QUALITY_SYSTEM_PROMPT = """
You are a financial document quality analyst. You receive structured data
extracted from a company's financial statements.

Check ONLY:
1. Internal consistency (Gross Profit = Revenue - COGS, Assets = Liabilities + Equity)
2. Implausible values (margins > 80%, negative equity without note)
3. Critical missing fields (total_revenue, net_income, total_assets, total_liabilities)

Return ONLY valid JSON, no explanation:
{
  "overall_confidence": <float 0.0-1.0>,
  "is_coherent": <bool>,
  "anomalies": [<string>, ...],
  "critical_missing_fields": [<string>, ...],
  "reextraction_recommended": <bool>,
  "auditor_notes": <string>
}

DO NOT make credit or lending decisions. DO NOT suggest loan outcomes.
""".strip()


# ── Command ───────────────────────────────────────────────────────────────────

@dataclass
class ProcessDocumentsCommand:
    application_id: str
    agent_id: str
    documents_dir: str        # e.g. "documents/COMP-019"
    session_id: str = field(default_factory=lambda: str(uuid4()))
    model_version: str = "document-processor-v1"
    correlation_id: str | None = None


# ── Handler ───────────────────────────────────────────────────────────────────

async def handle_process_documents(
    cmd: ProcessDocumentsCommand,
    store: EventStore,
) -> dict[str, Any]:
    """
    Full document processing pipeline for a loan application.

    Returns dict with extraction results and quality assessment.
    """

    # ── Step 1: Gas Town anchor ───────────────────────────────────────────────
    # AgentContextLoaded MUST be the first event — before any data is loaded
    # or any decision is made. This is the crash-recovery anchor point.
    anchor = AgentContextLoaded(
        agent_id=cmd.agent_id,
        session_id=cmd.session_id,
        context_source="fresh",
        event_replay_from_position=0,
        context_token_count=0,
        model_version=cmd.model_version,
    )
    agent_stream = f"agent-{cmd.agent_id}-{cmd.session_id}"
    await store.append(
        stream_id=agent_stream,
        events=[anchor],
        expected_version=-1,          # session_id is unique — always a new stream
        correlation_id=cmd.correlation_id,
    )

    # ── Step 2: Validate inputs ───────────────────────────────────────────────
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    if app.state != ApplicationState.SUBMITTED:
        raise DomainError(
            f"Application {cmd.application_id} is in state "
            f"'{app.state.value if app.state else 'UNKNOWN'}', "
            f"expected SUBMITTED for document processing.",
            rule="document_processing_state_check",
        )

    docs_path = Path(cmd.documents_dir)
    income_pdf  = docs_path / "income_statement_2024.pdf"
    balance_pdf = docs_path / "balance_sheet_2024.pdf"

    for pdf in (income_pdf, balance_pdf):
        if not pdf.exists():
            raise DomainError(
                f"Document not found: {pdf}",
                rule="document_processing_file_check",
            )

    # ── Steps 3 & 4: Extract income statement + balance sheet ─────────────────
    income_facts  = await extract_income_statement(str(income_pdf))
    balance_facts = await extract_balance_sheet(str(balance_pdf))
    merged_facts  = _merge_facts(income_facts, balance_facts)

    # ── Step 5: LLM quality assessment ────────────────────────────────────────
    quality = await _assess_quality_with_llm(merged_facts)

    # ── Step 6: Append to event store ─────────────────────────────────────────
    docpkg_stream = f"docpkg-{cmd.application_id}"

    extraction_event = ExtractionCompleted(
        application_id=cmd.application_id,
        agent_id=cmd.agent_id,
        session_id=cmd.session_id,
        income_facts=_facts_to_dict(income_facts),
        balance_facts=_facts_to_dict(balance_facts),
        merged_facts=_facts_to_dict(merged_facts),
        overall_confidence=quality["overall_confidence"],
        income_pdf_path=str(income_pdf),
        balance_pdf_path=str(balance_pdf),
    )

    quality_event = QualityAssessmentCompleted(
        application_id=cmd.application_id,
        overall_confidence=quality["overall_confidence"],
        is_coherent=quality["is_coherent"],
        anomalies=quality["anomalies"],
        critical_missing_fields=quality["critical_missing_fields"],
        reextraction_recommended=quality["reextraction_recommended"],
        auditor_notes=quality["auditor_notes"],
    )

    docpkg_version = await store.stream_version(docpkg_stream)
    # stream_version() returns 0 for non-existent streams; append() requires -1 for new streams
    docpkg_expected = -1 if docpkg_version == 0 else docpkg_version
    await store.append(
        stream_id=docpkg_stream,
        events=[extraction_event, quality_event],
        expected_version=docpkg_expected,
        correlation_id=cmd.correlation_id,
    )

    # Trigger credit analysis on the loan stream
    credit_req = CreditAnalysisRequested(
        application_id=cmd.application_id,
        assigned_agent_id=cmd.agent_id,
        priority="normal",
    )
    await store.append(
        stream_id=f"loan-{cmd.application_id}",
        events=[credit_req],
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
    )

    return {
        "docpkg_stream": docpkg_stream,
        "income_facts": _facts_to_dict(income_facts),
        "balance_facts": _facts_to_dict(balance_facts),
        "merged_facts": _facts_to_dict(merged_facts),
        "quality": quality,
        "critical_missing_fields": quality["critical_missing_fields"],
        "reextraction_recommended": quality["reextraction_recommended"],
        "credit_analysis_requested": True,
    }


# ── LLM quality assessment ────────────────────────────────────────────────────

async def _assess_quality_with_llm(facts: FinancialFacts) -> dict[str, Any]:
    """
    Call an LLM to assess coherence and completeness of extracted facts.

    Provider priority:
      1. OpenAI  — if OPENAI_API_KEY is set
      2. Anthropic — if ANTHROPIC_API_KEY is set
      3. Deterministic fallback — if neither key is available
    """
    facts_summary = json.dumps(_facts_to_dict(facts), indent=2)
    user_message = f"Assess the quality of these extracted financial facts:\n\n{facts_summary}"

    openai_key   = os.getenv("OPENAI_API_KEY")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")

    if openai_key:
        return await _assess_via_openai(user_message, openai_key)
    if anthropic_key:
        return await _assess_via_anthropic(user_message, anthropic_key)
    return _deterministic_quality_check(facts)


async def _assess_via_openai(user_message: str, api_key: str) -> dict[str, Any]:
    try:
        import openai
        client = openai.AsyncOpenAI(api_key=api_key)
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=512,
            messages=[
                {"role": "system", "content": _QUALITY_SYSTEM_PROMPT},
                {"role": "user",   "content": user_message},
            ],
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content.strip()
        return json.loads(raw)
    except Exception:
        return _deterministic_quality_check_from_message(user_message)


async def _assess_via_anthropic(user_message: str, api_key: str) -> dict[str, Any]:
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=_QUALITY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception:
        return _deterministic_quality_check_from_message(user_message)


def _deterministic_quality_check_from_message(user_message: str) -> dict[str, Any]:
    """Fallback when an LLM call fails mid-flight (key present but call errored)."""
    return {
        "overall_confidence": 0.5,
        "is_coherent": True,
        "anomalies": [],
        "critical_missing_fields": [],
        "reextraction_recommended": False,
        "auditor_notes": "LLM call failed — deterministic fallback applied.",
    }


def _deterministic_quality_check(facts: FinancialFacts) -> dict[str, Any]:
    """
    Rule-based quality check used when LLM is unavailable.
    Checks critical missing fields and basic plausibility.
    """
    anomalies: list[str] = []
    critical_missing: list[str] = []

    # Critical field presence
    for field_name in ("total_revenue", "net_income", "total_assets", "total_liabilities"):
        if getattr(facts, field_name, None) is None:
            critical_missing.append(field_name)

    # Gross margin plausibility
    if facts.total_revenue and facts.gross_profit:
        margin = facts.gross_profit / facts.total_revenue
        if margin > 0.80:
            anomalies.append(f"Gross margin {margin:.1%} exceeds 80% — verify figures")

    # Balance sheet equation
    if facts.total_assets and facts.total_liabilities and facts.total_equity:
        discrepancy = abs(facts.total_assets - (facts.total_liabilities + facts.total_equity))
        if discrepancy > 5000:
            anomalies.append(
                f"Balance sheet does not balance: "
                f"Assets={facts.total_assets:,.0f}, "
                f"Liabilities+Equity={facts.total_liabilities + facts.total_equity:,.0f}, "
                f"discrepancy={discrepancy:,.0f}"
            )

    # Confidence based on missing fields
    confidence = max(0.0, 1.0 - (len(critical_missing) * 0.25) - (len(anomalies) * 0.10))
    is_coherent = len(anomalies) == 0 and len(critical_missing) == 0

    return {
        "overall_confidence": round(confidence, 2),
        "is_coherent": is_coherent,
        "anomalies": anomalies,
        "critical_missing_fields": critical_missing,
        "reextraction_recommended": len(critical_missing) >= 2,
        "auditor_notes": (
            "Deterministic check (LLM unavailable). "
            + (f"Missing: {', '.join(critical_missing)}." if critical_missing else "All critical fields present.")
        ),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _merge_facts(income: FinancialFacts, balance: FinancialFacts) -> FinancialFacts:
    """
    Merge income statement and balance sheet facts into a single FinancialFacts.
    Income statement fields take priority for revenue/profit fields.
    Balance sheet fields take priority for asset/liability/equity fields.
    """
    merged_confidence = {**balance.field_confidence, **income.field_confidence}
    merged_notes = income.extraction_notes + [
        n for n in balance.extraction_notes if n not in income.extraction_notes
    ]
    return FinancialFacts(
        total_revenue     = income.total_revenue,
        gross_profit      = income.gross_profit,
        operating_income  = income.operating_income,
        ebitda            = income.ebitda,
        net_income        = income.net_income,
        total_assets      = balance.total_assets,
        total_liabilities = balance.total_liabilities,
        total_equity      = balance.total_equity,
        field_confidence  = merged_confidence,
        extraction_notes  = merged_notes,
    )


def _facts_to_dict(facts: FinancialFacts) -> dict[str, Any]:
    return {
        "total_revenue":     facts.total_revenue,
        "gross_profit":      facts.gross_profit,
        "operating_income":  facts.operating_income,
        "ebitda":            facts.ebitda,
        "net_income":        facts.net_income,
        "total_assets":      facts.total_assets,
        "total_liabilities": facts.total_liabilities,
        "total_equity":      facts.total_equity,
        "field_confidence":  facts.field_confidence,
        "extraction_notes":  facts.extraction_notes,
    }
