"""
Regulatory Examination Package — Self-contained audit export for regulators.

Produces a JSON document that a regulator can verify independently against the
database without trusting the live system.  The package contains:

1. Complete event stream in chronological order with full payloads.
2. Projection state as it existed at examination_date (temporal query).
3. Cryptographic audit chain integrity result.
4. Human-readable lifecycle narrative (one sentence per significant event).
5. AI agent model versions, confidence scores, and input data hashes.

The output is intentionally denormalised so the regulator can load a single
file into their own tooling (e.g., a spreadsheet or a Python script) and
re-derive every fact independently.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.event_store import EventStore


# ---------------------------------------------------------------------------
# Narrative generation
# ---------------------------------------------------------------------------

_NARRATIVE_TEMPLATES: dict[str, str] = {
    "ApplicationSubmitted": (
        "Application submitted by {applicant_id} for ${requested_amount_usd:,.0f} "
        "via {submission_channel}."
    ),
    "CreditAnalysisRequested": (
        "Credit analysis requested and assigned to agent {assigned_agent_id}."
    ),
    "CreditAnalysisCompleted": (
        "Credit analysis completed by agent {agent_id} (model {model_version}): "
        "risk tier {risk_tier}, recommended limit ${recommended_limit_usd:,.0f}, "
        "confidence {confidence_score}."
    ),
    "FraudScreeningCompleted": (
        "Fraud screening completed by agent {agent_id}: score {fraud_score:.3f}, "
        "flags={anomaly_flags}."
    ),
    "ComplianceReviewStarted": (
        "Compliance review started — checks required: {checks_required} "
        "(regulation set {regulation_set_version})."
    ),
    "ComplianceCheckRequested": (
        "Compliance check requested for {checks_required} "
        "(regulation set {regulation_set_version})."
    ),
    "ComplianceRulePassed": (
        "Compliance rule {rule_id} (v{rule_version}) passed."
    ),
    "ComplianceRuleFailed": (
        "Compliance rule {rule_id} (v{rule_version}) FAILED: {failure_reason}."
    ),
    "DecisionGenerated": (
        "AI orchestrator generated recommendation: {recommendation} "
        "(confidence {confidence_score})."
    ),
    "HumanReviewCompleted": (
        "Human reviewer {reviewer_id} completed review: {final_decision}"
        "{override_note}."
    ),
    "ApplicationApproved": (
        "Application approved for ${approved_amount_usd:,.0f} at {interest_rate:.2%} "
        "by {approved_by}."
    ),
    "ApplicationDeclined": (
        "Application declined by {declined_by}. Reasons: {decline_reasons}."
    ),
    "AuditIntegrityCheckRun": (
        "Integrity check run: {events_verified_count} events verified, "
        "chain valid={chain_valid}."
    ),
}


def _build_narrative(event_type: str, payload: dict[str, Any]) -> str:
    """Generate a plain-English sentence for a single event."""
    template = _NARRATIVE_TEMPLATES.get(event_type)
    if not template:
        return f"{event_type} recorded."
    try:
        p = dict(payload)
        # Enrich special fields
        if "override" in p and p.get("override"):
            p["override_note"] = f" (OVERRIDE: {p.get('override_reason', 'no reason given')})"
        else:
            p["override_note"] = ""
        if "confidence_score" in p and p["confidence_score"] is None:
            p["confidence_score"] = "unknown"
        if "anomaly_flags" not in p:
            p["anomaly_flags"] = []
        return template.format(**p)
    except (KeyError, ValueError, TypeError):
        return f"{event_type} recorded."


def _hash_package(content: str) -> str:
    """SHA-256 of the complete package content for independent verification."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Package generation
# ---------------------------------------------------------------------------

async def generate_regulatory_package(
    store: "EventStore",
    application_id: str,
    examination_date: datetime | None = None,
) -> dict[str, Any]:
    """
    Generate a self-contained regulatory examination package.

    Parameters
    ----------
    store:
        The EventStore to read from.
    application_id:
        The loan application to examine.
    examination_date:
        The point-in-time for the temporal projection query.
        Defaults to now (current state).

    Returns
    -------
    dict
        A fully self-contained JSON-serialisable package.  Save it with
        ``json.dumps(package, default=str)`` for file export.
    """
    if examination_date is None:
        examination_date = datetime.now(timezone.utc)

    loan_stream_id = f"loan-{application_id}"

    # ------------------------------------------------------------------ #
    # 1. Load complete event stream                                        #
    # ------------------------------------------------------------------ #
    events = await store.load_stream(loan_stream_id)

    serialised_events: list[dict[str, Any]] = []
    for ev in events:
        serialised_events.append({
            "event_id": str(ev.event_id),
            "stream_id": ev.stream_id,
            "stream_position": ev.stream_position,
            "global_position": ev.global_position,
            "event_type": ev.event_type,
            "event_version": ev.event_version,
            "payload": ev.payload,
            "metadata": ev.metadata,
            "recorded_at": ev.recorded_at.isoformat(),
            "narrative": _build_narrative(ev.event_type, ev.payload),
        })

    # ------------------------------------------------------------------ #
    # 2. Temporal projection state at examination_date                     #
    # ------------------------------------------------------------------ #
    # Replay events up to examination_date to derive point-in-time state.
    state_at_examination: dict[str, Any] = {
        "state": "UNKNOWN",
        "applicant_id": None,
        "requested_amount_usd": None,
        "approved_amount_usd": None,
        "risk_tier": None,
        "fraud_score": None,
        "recommendation": None,
        "final_decision": None,
        "compliance_checks": {},
        "agent_sessions": [],
    }

    STATE_MAP = {
        "ApplicationSubmitted": "SUBMITTED",
        "CreditAnalysisRequested": "AWAITING_ANALYSIS",
        "CreditAnalysisCompleted": "ANALYSIS_COMPLETE",
        "ComplianceReviewStarted": "COMPLIANCE_REVIEW",
        "ComplianceCheckRequested": "COMPLIANCE_REVIEW",
        "DecisionGenerated": "PENDING_DECISION",
        "ApplicationApproved": "FINAL_APPROVED",
        "ApplicationDeclined": "FINAL_DECLINED",
    }

    events_within_date = [
        e for e in events
        if e.recorded_at <= examination_date
    ]

    for ev in events_within_date:
        p = ev.payload
        et = ev.event_type
        if et in STATE_MAP:
            state_at_examination["state"] = STATE_MAP[et]
        if et == "ApplicationSubmitted":
            state_at_examination["applicant_id"] = p.get("applicant_id")
            state_at_examination["requested_amount_usd"] = p.get("requested_amount_usd")
        elif et == "CreditAnalysisCompleted":
            state_at_examination["risk_tier"] = p.get("risk_tier")
        elif et == "FraudScreeningCompleted":
            state_at_examination["fraud_score"] = p.get("fraud_score")
        elif et == "ComplianceRulePassed":
            state_at_examination["compliance_checks"][p.get("rule_id", "?")] = "PASSED"
        elif et == "ComplianceRuleFailed":
            state_at_examination["compliance_checks"][p.get("rule_id", "?")] = "FAILED"
        elif et == "DecisionGenerated":
            state_at_examination["recommendation"] = p.get("recommendation")
        elif et == "HumanReviewCompleted":
            state_at_examination["final_decision"] = p.get("final_decision")
            new_state = "APPROVED_PENDING_HUMAN" if p.get("final_decision") == "APPROVE" else "DECLINED_PENDING_HUMAN"
            state_at_examination["state"] = new_state
        elif et == "ApplicationApproved":
            state_at_examination["approved_amount_usd"] = p.get("approved_amount_usd")

    # ------------------------------------------------------------------ #
    # 3. Cryptographic integrity verification                              #
    # ------------------------------------------------------------------ #
    from src.integrity.audit_chain import run_integrity_check
    integrity = await run_integrity_check(store, "loan", application_id)
    integrity_section = {
        "chain_valid": integrity.chain_valid,
        "tamper_detected": integrity.tamper_detected,
        "events_verified_count": integrity.events_verified_count,
        "integrity_hash": integrity.integrity_hash,
        "previous_hash": integrity.previous_hash,
        "checked_at": integrity.checked_at.isoformat(),
        "error": integrity.error,
    }

    # ------------------------------------------------------------------ #
    # 4. Lifecycle narrative                                               #
    # ------------------------------------------------------------------ #
    narrative_lines = [
        f"[{ev['recorded_at']}] {ev['narrative']}"
        for ev in serialised_events
    ]
    lifecycle_narrative = "\n".join(narrative_lines)

    # ------------------------------------------------------------------ #
    # 5. AI agent participation summary                                    #
    # ------------------------------------------------------------------ #
    agent_participation: list[dict[str, Any]] = []
    for ev in events:
        et = ev.event_type
        p = ev.payload
        if et == "CreditAnalysisCompleted":
            agent_participation.append({
                "event_type": et,
                "agent_id": p.get("agent_id"),
                "session_id": p.get("session_id"),
                "model_version": p.get("model_version"),
                "confidence_score": p.get("confidence_score"),
                "input_data_hash": p.get("input_data_hash"),
                "risk_tier": p.get("risk_tier"),
                "recorded_at": ev.recorded_at.isoformat(),
            })
        elif et == "FraudScreeningCompleted":
            agent_participation.append({
                "event_type": et,
                "agent_id": p.get("agent_id"),
                "screening_model_version": p.get("screening_model_version"),
                "fraud_score": p.get("fraud_score"),
                "anomaly_flags": p.get("anomaly_flags", []),
                "input_data_hash": p.get("input_data_hash"),
                "recorded_at": ev.recorded_at.isoformat(),
            })
        elif et == "DecisionGenerated":
            agent_participation.append({
                "event_type": et,
                "orchestrator_agent_id": p.get("orchestrator_agent_id"),
                "recommendation": p.get("recommendation"),
                "confidence_score": p.get("confidence_score"),
                "model_versions": p.get("model_versions", {}),
                "contributing_agent_sessions": p.get("contributing_agent_sessions", []),
                "recorded_at": ev.recorded_at.isoformat(),
            })

    # ------------------------------------------------------------------ #
    # Assemble package                                                     #
    # ------------------------------------------------------------------ #
    package: dict[str, Any] = {
        "package_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "examination_date": examination_date.isoformat(),
        "application_id": application_id,
        "stream_id": loan_stream_id,
        "total_events": len(serialised_events),
        "section_1_event_stream": serialised_events,
        "section_2_state_at_examination": state_at_examination,
        "section_3_integrity": integrity_section,
        "section_4_lifecycle_narrative": lifecycle_narrative,
        "section_5_ai_agent_participation": agent_participation,
        "verification_instructions": (
            "To verify this package independently:\n"
            "1. Connect to the PostgreSQL database.\n"
            "2. Run: SELECT * FROM events WHERE stream_id = '{stream}' ORDER BY stream_position;\n"
            "3. Compare event payloads against section_1_event_stream.\n"
            "4. Re-compute the integrity hash by SHA-256 of concatenated event payloads.\n"
            "5. Compare against section_3_integrity.integrity_hash.\n"
            "Any difference indicates tampering."
        ).format(stream=loan_stream_id),
    }

    # Compute package self-hash (excludes this field itself)
    package_json = json.dumps(package, default=str, sort_keys=True)
    package["package_hash"] = _hash_package(package_json)

    return package
