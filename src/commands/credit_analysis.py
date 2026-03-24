"""
CreditAnalysisAgent Command Handler

Evaluates financial risk for a loan application.
  Step 1: Gas Town anchor
  Step 2: Load extracted facts from docpkg stream
  Step 3: Query Applicant Registry (3-year history + compliance flags)
  Step 4: LLM call — credit risk analysis
  Step 5: Python policy constraints (override LLM if rules trigger)
  Step 6: Append CreditAnalysisCompleted to loan stream

Policy constraints (Python, enforced after LLM):
  - Max loan-to-revenue ratio 35%       → cap recommended_limit_usd
  - Prior default                       → force risk_tier = HIGH
  - Active HIGH compliance flag         → cap confidence at 0.50
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.aggregates.loan_application import LoanApplicationAggregate
from src.event_store import EventStore
from src.models.events import AgentContextLoaded, CreditAnalysisCompleted, DomainError
from src.registry.client import ApplicantRegistryClient

_CREDIT_SYSTEM_PROMPT = """
You are a commercial credit analyst. Assess the financial risk of a loan application.

Return a JSON object:
{
  "risk_tier": "<LOW|MEDIUM|HIGH>",
  "confidence_score": <float 0.0-1.0>,
  "recommended_limit_usd": <float>,
  "rationale": "<2-3 sentences explaining the risk assessment>",
  "data_quality_caveats": ["<caveat if any extracted fields were missing or low-confidence>"]
}

Base your assessment on: revenue trends, profitability, leverage, and data quality.
DO NOT make a final approve/decline decision — only assess risk tier and recommended limit.
""".strip()


@dataclass
class RunCreditAnalysisCommand:
    application_id: str
    applicant_id: str
    agent_id: str
    requested_amount_usd: float
    session_id: str = field(default_factory=lambda: str(uuid4()))
    model_version: str = "credit-analyst-v1"
    correlation_id: str | None = None


async def handle_credit_analysis(
    cmd: RunCreditAnalysisCommand,
    store: EventStore,
    registry: ApplicantRegistryClient,
) -> dict[str, Any]:

    t0 = time.time()

    # ── Step 1: Gas Town anchor ───────────────────────────────────────────────
    await store.append(
        stream_id=f"agent-{cmd.agent_id}-{cmd.session_id}",
        events=[AgentContextLoaded(
            agent_id=cmd.agent_id,
            session_id=cmd.session_id,
            context_source="fresh",
            model_version=cmd.model_version,
        )],
        expected_version=-1,
        correlation_id=cmd.correlation_id,
    )

    # ── Step 2: Load extracted facts from docpkg stream ───────────────────────
    docpkg_events = await store.load_stream(f"docpkg-{cmd.application_id}")
    merged_facts: dict = {}
    quality: dict = {}
    for ev in docpkg_events:
        if ev.event_type == "ExtractionCompleted":
            merged_facts = ev.payload.get("merged_facts", {})
        if ev.event_type == "QualityAssessmentCompleted":
            quality = ev.payload

    if not merged_facts:
        raise DomainError(
            f"No ExtractionCompleted found for {cmd.application_id}. "
            "Run DocumentProcessingAgent first.",
            rule="credit_requires_extraction",
        )

    # ── Step 3: Query registry ────────────────────────────────────────────────
    history  = await registry.get_financial_history(cmd.applicant_id, years=3)
    flags    = await registry.get_compliance_flags(cmd.applicant_id)
    prior_default = await registry.had_prior_default(cmd.applicant_id)

    active_flag_types = {f["flag_type"] for f in flags if f["is_active"]}
    has_high_risk_flag = bool(active_flag_types)

    # ── Step 4: LLM credit risk analysis ─────────────────────────────────────
    llm_result = await _analyse_with_llm(
        merged_facts, history, quality,
        cmd.requested_amount_usd,
    )

    risk_tier        = llm_result.get("risk_tier", "MEDIUM")
    confidence_score = float(llm_result.get("confidence_score", 0.7))
    recommended_limit = float(llm_result.get("recommended_limit_usd", cmd.requested_amount_usd))
    rationale        = llm_result.get("rationale", "")
    caveats          = llm_result.get("data_quality_caveats", [])

    # ── Step 5: Policy constraints ────────────────────────────────────────────
    # Rule 1: Prior default → force HIGH
    if prior_default:
        risk_tier = "HIGH"
        caveats.append("Prior default on record — risk tier forced to HIGH.")

    # Rule 2: Active compliance flag → cap confidence at 0.50
    if has_high_risk_flag:
        confidence_score = min(confidence_score, 0.50)
        caveats.append(
            f"Active compliance flag(s) {active_flag_types} — confidence capped at 0.50."
        )

    # Rule 3: Max loan-to-revenue ratio 35%
    total_revenue = merged_facts.get("total_revenue") or (
        float(history[0]["total_revenue"]) if history and history[0].get("total_revenue") else None
    )
    if total_revenue and total_revenue > 0:
        max_limit = total_revenue * 0.35
        if recommended_limit > max_limit:
            caveats.append(
                f"Recommended limit capped at 35% of revenue "
                f"(${max_limit:,.0f} from ${recommended_limit:,.0f})."
            )
            recommended_limit = max_limit

    # Cap at requested amount
    recommended_limit = min(recommended_limit, cmd.requested_amount_usd)

    duration_ms = int((time.time() - t0) * 1000)
    input_hash  = _hash({**merged_facts, "history_years": len(history)})

    # ── Step 6: Append CreditAnalysisCompleted to loan stream ────────────────
    app = await LoanApplicationAggregate.load(store, cmd.application_id)

    await store.append(
        stream_id=f"loan-{cmd.application_id}",
        events=[CreditAnalysisCompleted(
            application_id=cmd.application_id,
            agent_id=cmd.agent_id,
            session_id=cmd.session_id,
            model_version=cmd.model_version,
            confidence_score=confidence_score,
            risk_tier=risk_tier,
            recommended_limit_usd=recommended_limit,
            analysis_duration_ms=duration_ms,
            input_data_hash=input_hash,
        )],
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
    )

    return {
        "application_id":      cmd.application_id,
        "risk_tier":           risk_tier,
        "confidence_score":    confidence_score,
        "recommended_limit_usd": recommended_limit,
        "rationale":           rationale,
        "data_quality_caveats": caveats,
        "duration_ms":         duration_ms,
    }


async def _analyse_with_llm(
    facts: dict,
    history: list[dict],
    quality: dict,
    requested_amount: float,
) -> dict[str, Any]:
    user_msg = json.dumps({
        "requested_amount_usd": requested_amount,
        "extracted_current_year": facts,
        "historical_registry": history,
        "data_quality": {
            "overall_confidence": quality.get("overall_confidence"),
            "critical_missing":   quality.get("critical_missing_fields", []),
            "anomalies":          quality.get("anomalies", []),
        },
    }, indent=2, default=str)

    openai_key    = os.getenv("OPENAI_API_KEY")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")

    try:
        if openai_key:
            import openai
            client = openai.AsyncOpenAI(api_key=openai_key)
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=512,
                messages=[
                    {"role": "system", "content": _CREDIT_SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                response_format={"type": "json_object"},
            )
            return json.loads(resp.choices[0].message.content.strip())

        if anthropic_key:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=anthropic_key)
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                system=_CREDIT_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip()
            return json.loads(raw)
    except Exception:
        pass

    # Deterministic fallback
    revenue = facts.get("total_revenue")
    net_income = facts.get("net_income")
    risk = "HIGH" if (not revenue or not net_income or net_income < 0) else "MEDIUM"
    limit = min(requested_amount, (revenue * 0.35) if revenue else requested_amount)
    return {
        "risk_tier": risk,
        "confidence_score": 0.6,
        "recommended_limit_usd": limit,
        "rationale": "Deterministic fallback — LLM unavailable.",
        "data_quality_caveats": [],
    }


def _hash(data: dict) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()
