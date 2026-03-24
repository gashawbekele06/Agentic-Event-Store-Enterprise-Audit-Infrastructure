"""
FraudDetectionAgent Command Handler

Detects inconsistencies between submitted documents and registry history.
  Step 1: Gas Town anchor
  Step 2: Load extracted facts from docpkg stream
  Step 3: Query Applicant Registry for 3-year financial history
  Step 4: LLM identifies pattern anomalies
  Step 5: Python computes weighted fraud_score
  Step 6: Append FraudAnomalyDetected (0-N) + FraudScreeningCompleted to fraud stream
          Append ComplianceCheckRequested to loan stream
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from src.aggregates.loan_application import ApplicationState, LoanApplicationAggregate
from src.event_store import EventStore
from src.models.events import (
    AgentContextLoaded,
    ComplianceCheckRequested,
    DomainError,
    FraudAnomalyDetected,
    FraudScreeningCompleted,
)
from src.registry.client import ApplicantRegistryClient

# Anomaly severity weights → fraud_score
_SEVERITY_WEIGHTS = {
    "revenue_discrepancy":   0.30,
    "margin_collapse":       0.25,
    "asset_inflation":       0.35,
    "income_reversal":       0.20,
    "growth_inconsistency":  0.15,
    "other":                 0.10,
}

_FRAUD_SYSTEM_PROMPT = """
You are a financial fraud analyst. You receive current-year extracted financials
and 3-year historical registry data for the same company.

Identify anomalies ONLY — do not make lending decisions.

For each anomaly found, return a JSON object:
{
  "anomaly_type": "<one of: revenue_discrepancy|margin_collapse|asset_inflation|income_reversal|growth_inconsistency|other>",
  "description": "<one sentence>",
  "severity": <float 0.0-1.0>,
  "evidence": "<specific numbers that support this finding>"
}

Return a JSON array of anomaly objects (empty array [] if none found).
""".strip()


@dataclass
class RunFraudDetectionCommand:
    application_id: str
    applicant_id: str
    agent_id: str
    regulation_set_version: str = "2026-Q1"
    session_id: str = field(default_factory=lambda: str(uuid4()))
    model_version: str = "fraud-detector-v1"
    correlation_id: str | None = None


async def handle_fraud_detection(
    cmd: RunFraudDetectionCommand,
    store: EventStore,
    registry: ApplicantRegistryClient,
) -> dict[str, Any]:
    """Full fraud detection pipeline."""

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
    extracted_facts: dict = {}
    for ev in docpkg_events:
        if ev.event_type == "ExtractionCompleted":
            extracted_facts = ev.payload.get("merged_facts", {})
            break

    if not extracted_facts:
        raise DomainError(
            f"No ExtractionCompleted event found for {cmd.application_id}. "
            "Run DocumentProcessingAgent first.",
            rule="fraud_requires_extraction",
        )

    # ── Step 3: Load 3-year financial history from registry ───────────────────
    history = await registry.get_financial_history(cmd.applicant_id, years=3)
    prior_default = await registry.had_prior_default(cmd.applicant_id)

    # ── Step 4: LLM anomaly detection ─────────────────────────────────────────
    anomalies = await _detect_anomalies_with_llm(extracted_facts, history)

    # ── Step 5: Python computes weighted fraud_score ───────────────────────────
    # Prior default is a hard signal — adds 0.40 regardless of LLM output
    fraud_score = sum(
        _SEVERITY_WEIGHTS.get(a["anomaly_type"], 0.10) * a["severity"]
        for a in anomalies
    )
    if prior_default:
        fraud_score = min(1.0, fraud_score + 0.40)
        anomalies.append({
            "anomaly_type": "other",
            "description": "Company has a prior loan default on record.",
            "severity": 0.80,
            "evidence": "loan_relationships.default_occurred = True",
        })

    fraud_score = round(min(1.0, fraud_score), 4)

    # fraud_score > 0.60 → DECLINE signal; 0.30–0.60 → FLAG_FOR_REVIEW
    risk_level = (
        "HIGH" if fraud_score > 0.60
        else "MEDIUM" if fraud_score >= 0.30
        else "LOW"
    )

    # ── Step 6: Append events ─────────────────────────────────────────────────
    fraud_stream = f"fraud-{cmd.application_id}"
    fraud_events = []

    for a in anomalies:
        fraud_events.append(FraudAnomalyDetected(
            application_id=cmd.application_id,
            anomaly_type=a["anomaly_type"],
            description=a["description"],
            severity=float(a["severity"]),
            evidence=a.get("evidence", ""),
        ))

    fraud_events.append(FraudScreeningCompleted(
        application_id=cmd.application_id,
        agent_id=cmd.agent_id,
        fraud_score=fraud_score,
        anomaly_flags=[a["anomaly_type"] for a in anomalies],
        screening_model_version=cmd.model_version,
        input_data_hash=_hash(extracted_facts),
    ))

    await store.append(
        stream_id=fraud_stream,
        events=fraud_events,
        expected_version=-1,
        correlation_id=cmd.correlation_id,
    )

    # Trigger compliance review on loan stream
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    await store.append(
        stream_id=f"loan-{cmd.application_id}",
        events=[ComplianceCheckRequested(
            application_id=cmd.application_id,
            regulation_set_version=cmd.regulation_set_version,
            checks_required=["REG-001", "REG-002", "REG-003",
                             "REG-004", "REG-005", "REG-006"],
        )],
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
    )

    return {
        "fraud_stream": fraud_stream,
        "fraud_score": fraud_score,
        "risk_level": risk_level,
        "anomalies_found": len(anomalies),
        "anomalies": anomalies,
        "prior_default": prior_default,
        "recommendation": "DECLINE" if fraud_score > 0.60 else
                          "FLAG_FOR_REVIEW" if fraud_score >= 0.30 else "PASS",
    }


async def _detect_anomalies_with_llm(
    current: dict, history: list[dict]
) -> list[dict]:
    user_msg = json.dumps({
        "current_year_extracted": current,
        "historical_registry": history,
    }, indent=2, default=str)

    openai_key   = os.getenv("OPENAI_API_KEY")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")

    try:
        if openai_key:
            return await _llm_openai(user_msg, openai_key)
        if anthropic_key:
            return await _llm_anthropic(user_msg, anthropic_key)
    except Exception:
        pass

    return _deterministic_anomaly_check(current, history)


async def _llm_openai(user_msg: str, api_key: str) -> list[dict]:
    import openai
    client = openai.AsyncOpenAI(api_key=api_key)
    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=1024,
        messages=[
            {"role": "system", "content": _FRAUD_SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content.strip()
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, list) else parsed.get("anomalies", [])


async def _llm_anthropic(user_msg: str, api_key: str) -> list[dict]:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=_FRAUD_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].lstrip("json").strip()
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, list) else parsed.get("anomalies", [])


def _deterministic_anomaly_check(
    current: dict, history: list[dict]
) -> list[dict]:
    """Rule-based fallback when no LLM is available."""
    anomalies = []
    if not history:
        return anomalies

    prior = history[0]  # Most recent historical year

    cur_rev  = current.get("total_revenue")
    hist_rev = prior.get("total_revenue")
    if cur_rev and hist_rev and hist_rev > 0:
        delta = (cur_rev - float(hist_rev)) / float(hist_rev)
        if abs(delta) > 0.40:
            anomalies.append({
                "anomaly_type": "revenue_discrepancy",
                "description": f"Revenue changed {delta:+.0%} vs prior year.",
                "severity": min(1.0, abs(delta)),
                "evidence": f"current={cur_rev:,.0f}, prior={hist_rev:,.0f}",
            })

    return anomalies


def _hash(data: dict) -> str:
    import hashlib, json
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()
