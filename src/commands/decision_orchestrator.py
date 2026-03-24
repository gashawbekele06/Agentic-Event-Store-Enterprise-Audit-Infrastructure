"""
DecisionOrchestratorAgent Command Handler

The only agent that reads from other agents' output streams.
Synthesises credit + fraud + compliance into a final recommendation.
Hard constraints are enforced in Python AFTER the LLM produces its summary —
the LLM cannot override them.

Hard constraints (Python, in order):
  1. compliance BLOCKED         → force DECLINE
  2. fraud_score > 0.60         → force REFER
  3. confidence_score < 0.60    → force REFER
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from src.aggregates.loan_application import LoanApplicationAggregate
from src.event_store import EventStore
from src.models.events import (
    AgentContextLoaded,
    ApplicationApproved,
    ApplicationDeclined,
    DecisionGenerated,
    DomainError,
    HumanReviewRequested,
)

_ORCHESTRATOR_SYSTEM_PROMPT = """
You are a loan decision orchestrator. You receive credit analysis, fraud screening,
and compliance results for a commercial loan application.

Produce a JSON object with these fields:
{
  "executive_summary": "<3-5 sentences summarising key risk factors and recommendation rationale>",
  "key_risks": ["<risk 1>", "<risk 2>", ...],
  "recommendation": "<APPROVE|DECLINE|REFER>",
  "confidence_score": <float 0.0-1.0>
}

Base your recommendation on the evidence provided.
DO NOT override hard compliance blocks or fraud scores — those are enforced separately.
""".strip()


@dataclass
class RunOrchestratorCommand:
    application_id: str
    agent_id: str
    requested_amount_usd: float
    session_id: str = field(default_factory=lambda: str(uuid4()))
    model_version: str = "orchestrator-v1"
    correlation_id: str | None = None


async def handle_orchestrator_decision(
    cmd: RunOrchestratorCommand,
    store: EventStore,
) -> dict[str, Any]:
    """Full orchestration pipeline."""

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

    # ── Step 2: Load credit result from loan stream ───────────────────────────
    credit = _find_event(
        await store.load_stream(f"loan-{cmd.application_id}"),
        "CreditAnalysisCompleted",
    )
    if not credit:
        raise DomainError(
            f"No CreditAnalysisCompleted found for {cmd.application_id}.",
            rule="orchestrator_requires_credit",
        )

    # ── Step 3: Load fraud result from fraud stream ───────────────────────────
    fraud = _find_event(
        await store.load_stream(f"fraud-{cmd.application_id}"),
        "FraudScreeningCompleted",
    )
    if not fraud:
        raise DomainError(
            f"No FraudScreeningCompleted found for {cmd.application_id}.",
            rule="orchestrator_requires_fraud",
        )

    # ── Step 4: Load compliance result from compliance stream ─────────────────
    compliance = _find_event(
        await store.load_stream(f"compliance-{cmd.application_id}"),
        "ComplianceCheckCompleted",
    )
    if not compliance:
        raise DomainError(
            f"No ComplianceCheckCompleted found for {cmd.application_id}.",
            rule="orchestrator_requires_compliance",
        )

    credit_payload     = credit.payload
    fraud_payload      = fraud.payload
    compliance_payload = compliance.payload

    # ── Step 5: LLM synthesis ─────────────────────────────────────────────────
    llm_decision = await _synthesise_with_llm(
        credit_payload, fraud_payload, compliance_payload,
        cmd.requested_amount_usd,
    )

    # ── Step 6: Hard constraints (Python overrides LLM) ───────────────────────
    recommendation   = llm_decision.get("recommendation", "REFER")
    confidence_score = float(llm_decision.get("confidence_score", 0.5))
    executive_summary = llm_decision.get("executive_summary", "")
    key_risks        = llm_decision.get("key_risks", [])

    override_reason: str | None = None

    if compliance_payload.get("overall_verdict") == "BLOCKED":
        recommendation  = "DECLINE"
        override_reason = "Compliance hard block — application cannot proceed."
        key_risks.insert(0, override_reason)

    elif float(fraud_payload.get("fraud_score", 0)) > 0.60:
        recommendation  = "REFER"
        override_reason = (
            f"Fraud score {fraud_payload['fraud_score']:.2f} exceeds 0.60 threshold."
        )
        key_risks.insert(0, override_reason)

    elif confidence_score < 0.60:
        recommendation  = "REFER"
        override_reason = (
            f"Confidence score {confidence_score:.2f} below 0.60 minimum — "
            "human review required."
        )
        key_risks.insert(0, override_reason)

    if override_reason:
        executive_summary = f"[Override: {override_reason}] " + executive_summary

    # ── Step 7: Append DecisionGenerated to loan stream ───────────────────────
    app = await LoanApplicationAggregate.load(store, cmd.application_id)

    contributing_sessions = [
        s for s in [
            credit_payload.get("session_id"),
            fraud_payload.get("agent_id"),
        ] if s
    ]

    decision_event = DecisionGenerated(
        application_id=cmd.application_id,
        orchestrator_agent_id=cmd.agent_id,
        recommendation=recommendation,
        confidence_score=confidence_score,
        contributing_agent_sessions=contributing_sessions,
        decision_basis_summary=executive_summary,
        model_versions={
            "credit":      credit_payload.get("model_version", "unknown"),
            "fraud":       fraud_payload.get("screening_model_version", "unknown"),
            "orchestrator": cmd.model_version,
        },
    )

    await store.append(
        stream_id=f"loan-{cmd.application_id}",
        events=[decision_event],
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
    )

    # Reload after DecisionGenerated
    app = await LoanApplicationAggregate.load(store, cmd.application_id)

    # ── Step 8: Auto-decision or human review ─────────────────────────────────
    if recommendation == "APPROVE":
        approved_amount = min(
            cmd.requested_amount_usd,
            float(credit_payload.get("recommended_limit_usd", cmd.requested_amount_usd)),
        )
        await store.append(
            stream_id=f"loan-{cmd.application_id}",
            events=[ApplicationApproved(
                application_id=cmd.application_id,
                approved_amount_usd=approved_amount,
                interest_rate=_calculate_rate(credit_payload.get("risk_tier", "MEDIUM")),
                conditions=[],
                approved_by=cmd.agent_id,
            )],
            expected_version=app.version,
            correlation_id=cmd.correlation_id,
        )

    elif recommendation == "DECLINE":
        decline_reasons = key_risks[:3] if key_risks else ["Risk criteria not met."]
        await store.append(
            stream_id=f"loan-{cmd.application_id}",
            events=[ApplicationDeclined(
                application_id=cmd.application_id,
                decline_reasons=decline_reasons,
                declined_by=cmd.agent_id,
                adverse_action_notice_required=True,
            )],
            expected_version=app.version,
            correlation_id=cmd.correlation_id,
        )

    else:  # REFER → human review
        await store.append(
            stream_id=f"loan-{cmd.application_id}",
            events=[HumanReviewRequested(
                application_id=cmd.application_id,
                reason=override_reason or "Low confidence — referred for human review.",
                orchestrator_agent_id=cmd.agent_id,
            )],
            expected_version=app.version,
            correlation_id=cmd.correlation_id,
        )

    return {
        "application_id":   cmd.application_id,
        "recommendation":   recommendation,
        "confidence_score": confidence_score,
        "override_applied": override_reason is not None,
        "override_reason":  override_reason,
        "executive_summary": executive_summary,
        "key_risks":        key_risks,
        "final_state": (
            "APPROVED" if recommendation == "APPROVE"
            else "DECLINED" if recommendation == "DECLINE"
            else "PENDING_HUMAN_REVIEW"
        ),
    }


async def _synthesise_with_llm(
    credit: dict,
    fraud: dict,
    compliance: dict,
    requested_amount: float,
) -> dict[str, Any]:
    user_msg = json.dumps({
        "requested_amount_usd": requested_amount,
        "credit_analysis": {
            "risk_tier":            credit.get("risk_tier"),
            "confidence_score":     credit.get("confidence_score"),
            "recommended_limit_usd": credit.get("recommended_limit_usd"),
        },
        "fraud_screening": {
            "fraud_score":   fraud.get("fraud_score"),
            "anomaly_flags": fraud.get("anomaly_flags", []),
        },
        "compliance": {
            "overall_verdict": compliance.get("overall_verdict"),
            "has_hard_block":  compliance.get("has_hard_block"),
            "rules_failed":    compliance.get("rules_failed"),
            "summary":         compliance.get("summary"),
        },
    }, indent=2)

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
                    {"role": "system", "content": _ORCHESTRATOR_SYSTEM_PROMPT},
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
                system=_ORCHESTRATOR_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip()
            return json.loads(raw)

    except Exception:
        pass

    # Deterministic fallback
    fraud_score = float(fraud.get("fraud_score", 0))
    confidence  = float(credit.get("confidence_score") or 0.5)
    risk_tier   = credit.get("risk_tier", "MEDIUM")
    blocked     = compliance.get("overall_verdict") == "BLOCKED"

    if blocked or fraud_score > 0.60:
        rec = "DECLINE"
    elif confidence < 0.60 or risk_tier == "HIGH":
        rec = "REFER"
    else:
        rec = "APPROVE"

    return {
        "executive_summary": f"Deterministic fallback: {risk_tier} risk, fraud={fraud_score:.2f}, compliance={compliance.get('overall_verdict')}.",
        "key_risks": [],
        "recommendation": rec,
        "confidence_score": confidence,
    }


def _find_event(events, event_type: str):
    """Return the last event of a given type from a stream."""
    for ev in reversed(events):
        if ev.event_type == event_type:
            return ev
    return None


def _calculate_rate(risk_tier: str) -> float:
    return {"LOW": 4.5, "MEDIUM": 6.75, "HIGH": 9.0}.get(risk_tier, 6.75)
