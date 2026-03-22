"""
MCP Tools — The Command Side of The Ledger MCP Server.

8 tools exposing event store command handlers to LLM consumers.
All tools return structured errors with suggested_action for autonomous recovery.

Key design principles:
1. Preconditions documented in tool description so LLMs know what to call first
2. Structured error types (not free-form messages) enable autonomous error recovery
3. All writes go through command handlers (aggregate rules enforced)
4. Optimistic concurrency errors returned with exact retry instructions
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from src.commands.handlers import (
    ApproveApplicationCommand,
    CreditAnalysisCompletedCommand,
    DeclineApplicationCommand,
    FraudScreeningCompletedCommand,
    GenerateDecisionCommand,
    HumanReviewCompletedCommand,
    RecordComplianceCheckCommand,
    StartAgentSessionCommand,
    StartComplianceReviewCommand,
    SubmitApplicationCommand,
    handle_approve_application,
    handle_credit_analysis_completed,
    handle_decline_application,
    handle_fraud_screening_completed,
    handle_generate_decision,
    handle_human_review_completed,
    handle_record_compliance_check,
    handle_request_credit_analysis,
    handle_start_agent_session,
    handle_start_compliance_review,
    handle_submit_application,
    RequestCreditAnalysisCommand,
)
from src.event_store import EventStore
from src.integrity.audit_chain import run_integrity_check
from src.models.events import DomainError, OptimisticConcurrencyError, StreamNotFoundError


def _occ_error(e: OptimisticConcurrencyError) -> dict[str, Any]:
    return {
        "error_type": "OptimisticConcurrencyError",
        "message": str(e),
        "stream_id": e.stream_id,
        "expected_version": e.expected_version,
        "actual_version": e.actual_version,
        "suggested_action": "reload_stream_and_retry",
    }


def _domain_error(e: DomainError) -> dict[str, Any]:
    return {
        "error_type": "DomainError",
        "message": str(e),
        "rule": e.rule,
        "suggested_action": "check_application_state_and_preconditions",
    }


def _not_found_error(e: StreamNotFoundError) -> dict[str, Any]:
    return {
        "error_type": "StreamNotFoundError",
        "message": str(e),
        "stream_id": e.stream_id,
        "suggested_action": "verify_application_id_and_submit_first",
    }


def _validation_error(msg: str, field: str) -> dict[str, Any]:
    return {
        "error_type": "ValidationError",
        "message": msg,
        "field": field,
        "suggested_action": "correct_field_and_retry",
    }


async def tool_submit_application(store: EventStore, params: dict[str, Any]) -> dict[str, Any]:
    """
    Submit a new loan application to The Ledger.

    Prerequisites: None — this is the first tool to call for any new application.
    Creates the loan-{application_id} stream.

    Returns: stream_id and initial_version on success.
    """
    required = ["application_id", "applicant_id", "requested_amount_usd", "loan_purpose", "submission_channel"]
    for f in required:
        if f not in params:
            return _validation_error(f"Missing required field: {f}", f)

    try:
        new_version = await handle_submit_application(
            SubmitApplicationCommand(
                application_id=params["application_id"],
                applicant_id=params["applicant_id"],
                requested_amount_usd=float(params["requested_amount_usd"]),
                loan_purpose=params["loan_purpose"],
                submission_channel=params["submission_channel"],
                correlation_id=params.get("correlation_id"),
                causation_id=params.get("causation_id"),
            ),
            store,
        )
        return {
            "success": True,
            "stream_id": f"loan-{params['application_id']}",
            "initial_version": new_version,
        }
    except OptimisticConcurrencyError as e:
        return _occ_error(e)
    except DomainError as e:
        return _domain_error(e)


async def tool_start_agent_session(store: EventStore, params: dict[str, Any]) -> dict[str, Any]:
    """
    Start an AI agent session — MUST be called before any agent decision tools.

    This is the Gas Town anchor: writes AgentContextLoaded as the first event
    in the agent session stream. All subsequent agent tools validate that this
    event exists before accepting decisions.

    Prerequisites: None. Call this before record_credit_analysis, record_fraud_screening, etc.
    Returns: session_id and context_position.
    """
    required = ["agent_id", "session_id", "context_source", "model_version"]
    for f in required:
        if f not in params:
            return _validation_error(f"Missing required field: {f}", f)

    try:
        new_version = await handle_start_agent_session(
            StartAgentSessionCommand(
                agent_id=params["agent_id"],
                session_id=params["session_id"],
                context_source=params["context_source"],
                model_version=params["model_version"],
                event_replay_from_position=params.get("event_replay_from_position", 0),
                context_token_count=params.get("context_token_count", 0),
                correlation_id=params.get("correlation_id"),
                causation_id=params.get("causation_id"),
            ),
            store,
        )
        return {
            "success": True,
            "session_id": params["session_id"],
            "context_position": new_version,
            "stream_id": f"agent-{params['agent_id']}-{params['session_id']}",
        }
    except OptimisticConcurrencyError as e:
        return _occ_error(e)
    except DomainError as e:
        return _domain_error(e)


async def tool_request_credit_analysis(store: EventStore, params: dict[str, Any]) -> dict[str, Any]:
    """
    Request credit analysis for a loan application.
    Prerequisites: Application must be in SUBMITTED state (call submit_application first).
    """
    required = ["application_id", "assigned_agent_id"]
    for f in required:
        if f not in params:
            return _validation_error(f"Missing required field: {f}", f)
    try:
        new_version = await handle_request_credit_analysis(
            RequestCreditAnalysisCommand(
                application_id=params["application_id"],
                assigned_agent_id=params["assigned_agent_id"],
                priority=params.get("priority", "normal"),
            ),
            store,
        )
        return {"success": True, "new_stream_version": new_version}
    except OptimisticConcurrencyError as e:
        return _occ_error(e)
    except DomainError as e:
        return _domain_error(e)
    except StreamNotFoundError as e:
        return _not_found_error(e)


async def tool_record_credit_analysis(store: EventStore, params: dict[str, Any]) -> dict[str, Any]:
    """
    Record a completed credit analysis in the loan stream.

    Prerequisites:
    - An active agent session must exist (call start_agent_session first).
      Calling without an active session returns PreconditionFailed.
    - Application must be in AWAITING_ANALYSIS state.
    - risk_tier must be one of: LOW, MEDIUM, HIGH.

    Returns: event_id and new_stream_version on success.
    """
    required = ["application_id", "agent_id", "session_id", "model_version", "risk_tier", "recommended_limit_usd"]
    for f in required:
        if f not in params:
            return _validation_error(f"Missing required field: {f}", f)

    valid_tiers = {"LOW", "MEDIUM", "HIGH"}
    if params.get("risk_tier") not in valid_tiers:
        return _validation_error(f"risk_tier must be one of {valid_tiers}", "risk_tier")

    try:
        new_version = await handle_credit_analysis_completed(
            CreditAnalysisCompletedCommand(
                application_id=params["application_id"],
                agent_id=params["agent_id"],
                session_id=params["session_id"],
                model_version=params["model_version"],
                confidence_score=params.get("confidence_score"),
                risk_tier=params["risk_tier"],
                recommended_limit_usd=float(params["recommended_limit_usd"]),
                duration_ms=int(params.get("duration_ms", 0)),
                input_data=params.get("input_data", {}),
                correlation_id=params.get("correlation_id"),
                causation_id=params.get("causation_id"),
            ),
            store,
        )
        return {"success": True, "new_stream_version": new_version}
    except OptimisticConcurrencyError as e:
        return _occ_error(e)
    except DomainError as e:
        return _domain_error(e)
    except StreamNotFoundError as e:
        return _not_found_error(e)


async def tool_record_fraud_screening(store: EventStore, params: dict[str, Any]) -> dict[str, Any]:
    """
    Record a fraud screening result on the loan stream.

    Prerequisites:
    - An active agent session must exist (call start_agent_session first).
    - fraud_score must be a float between 0.0 and 1.0.

    Returns: event_id and new_stream_version.
    """
    required = ["application_id", "agent_id", "session_id", "fraud_score"]
    for f in required:
        if f not in params:
            return _validation_error(f"Missing required field: {f}", f)

    fraud_score = float(params["fraud_score"])
    if not (0.0 <= fraud_score <= 1.0):
        return _validation_error("fraud_score must be between 0.0 and 1.0", "fraud_score")

    try:
        new_version = await handle_fraud_screening_completed(
            FraudScreeningCompletedCommand(
                application_id=params["application_id"],
                agent_id=params["agent_id"],
                session_id=params["session_id"],
                fraud_score=fraud_score,
                anomaly_flags=params.get("anomaly_flags", []),
                screening_model_version=params.get("screening_model_version", ""),
                input_data=params.get("input_data", {}),
                correlation_id=params.get("correlation_id"),
                causation_id=params.get("causation_id"),
            ),
            store,
        )
        return {"success": True, "new_stream_version": new_version}
    except OptimisticConcurrencyError as e:
        return _occ_error(e)
    except DomainError as e:
        return _domain_error(e)
    except StreamNotFoundError as e:
        return _not_found_error(e)


async def tool_start_compliance_review(store: EventStore, params: dict[str, Any]) -> dict[str, Any]:
    """
    Start compliance review for a loan application, transitioning it to ComplianceReview state.

    Prerequisites: Application must be in ANALYSIS_COMPLETE state (credit analysis done).
    Returns: new_stream_version.
    """
    required = ["application_id"]
    for f in required:
        if f not in params:
            return _validation_error(f"Missing required field: {f}", f)
    try:
        new_version = await handle_start_compliance_review(
            StartComplianceReviewCommand(
                application_id=params["application_id"],
                checks_required=params.get("checks_required", []),
                regulation_set_version=params.get("regulation_set_version", ""),
            ),
            store,
        )
        return {"success": True, "new_stream_version": new_version}
    except OptimisticConcurrencyError as e:
        return _occ_error(e)
    except DomainError as e:
        return _domain_error(e)
    except StreamNotFoundError as e:
        return _not_found_error(e)


async def tool_record_compliance_check(store: EventStore, params: dict[str, Any]) -> dict[str, Any]:
    """
    Record a compliance rule pass or fail on the loan stream.

    Prerequisites:
    - Application must be in COMPLIANCE_REVIEW state (call start_compliance_review first).
    - rule_id must exist in the active regulation_set_version.

    Returns: check_id and compliance_status.
    """
    required = ["application_id", "rule_id", "rule_version", "passed"]
    for f in required:
        if f not in params:
            return _validation_error(f"Missing required field: {f}", f)

    try:
        new_version = await handle_record_compliance_check(
            RecordComplianceCheckCommand(
                application_id=params["application_id"],
                rule_id=params["rule_id"],
                rule_version=params["rule_version"],
                passed=bool(params["passed"]),
                failure_reason=params.get("failure_reason", ""),
                remediation_required=params.get("remediation_required", False),
                evidence_hash=params.get("evidence_hash", ""),
                correlation_id=params.get("correlation_id"),
                causation_id=params.get("causation_id"),
            ),
            store,
        )
        return {
            "success": True,
            "new_stream_version": new_version,
            "compliance_status": "PASSED" if params["passed"] else "FAILED",
        }
    except OptimisticConcurrencyError as e:
        return _occ_error(e)
    except DomainError as e:
        return _domain_error(e)
    except StreamNotFoundError as e:
        return _not_found_error(e)


async def tool_generate_decision(store: EventStore, params: dict[str, Any]) -> dict[str, Any]:
    """
    Generate an AI decision for a loan application.

    Prerequisites:
    - Application must be in COMPLIANCE_REVIEW state.
    - All contributing_agent_sessions must have processed this application.
    - confidence_score < 0.6 will automatically set recommendation to REFER
      (regulatory confidence floor — cannot be overridden by this tool).
    - recommendation must be: APPROVE, DECLINE, or REFER.

    Returns: decision_id, recommendation (may differ from input due to confidence floor).
    """
    required = ["application_id", "orchestrator_agent_id", "recommendation", "confidence_score"]
    for f in required:
        if f not in params:
            return _validation_error(f"Missing required field: {f}", f)

    valid_recs = {"APPROVE", "DECLINE", "REFER"}
    if params.get("recommendation") not in valid_recs:
        return _validation_error(f"recommendation must be one of {valid_recs}", "recommendation")

    try:
        new_version = await handle_generate_decision(
            GenerateDecisionCommand(
                application_id=params["application_id"],
                orchestrator_agent_id=params["orchestrator_agent_id"],
                recommendation=params["recommendation"],
                confidence_score=float(params["confidence_score"]),
                contributing_agent_sessions=params.get("contributing_agent_sessions", []),
                decision_basis_summary=params.get("decision_basis_summary", ""),
                model_versions=params.get("model_versions", {}),
                correlation_id=params.get("correlation_id"),
                causation_id=params.get("causation_id"),
            ),
            store,
        )
        return {"success": True, "new_stream_version": new_version}
    except OptimisticConcurrencyError as e:
        return _occ_error(e)
    except DomainError as e:
        return _domain_error(e)
    except StreamNotFoundError as e:
        return _not_found_error(e)


async def tool_record_human_review(store: EventStore, params: dict[str, Any]) -> dict[str, Any]:
    """
    Record a human loan officer's review decision.

    Prerequisites:
    - Application must be in PENDING_DECISION state (DecisionGenerated event must exist).
    - If override=True, override_reason is required (validation enforced by domain).

    After this tool:
    - If final_decision=APPROVE → call approve_application.
    - If final_decision=DECLINE → call decline_application.

    Returns: final_decision and application_state.
    """
    required = ["application_id", "reviewer_id", "override", "final_decision"]
    for f in required:
        if f not in params:
            return _validation_error(f"Missing required field: {f}", f)

    try:
        new_version = await handle_human_review_completed(
            HumanReviewCompletedCommand(
                application_id=params["application_id"],
                reviewer_id=params["reviewer_id"],
                override=bool(params["override"]),
                final_decision=params["final_decision"],
                override_reason=params.get("override_reason"),
                correlation_id=params.get("correlation_id"),
                causation_id=params.get("causation_id"),
            ),
            store,
        )
        next_state = "APPROVED_PENDING_HUMAN" if params["final_decision"] == "APPROVE" else "DECLINED_PENDING_HUMAN"
        return {
            "success": True,
            "final_decision": params["final_decision"],
            "application_state": next_state,
            "new_stream_version": new_version,
            "next_step": "Call approve_application or decline_application to finalize.",
        }
    except OptimisticConcurrencyError as e:
        return _occ_error(e)
    except DomainError as e:
        return _domain_error(e)
    except StreamNotFoundError as e:
        return _not_found_error(e)


async def tool_approve_application(store: EventStore, params: dict[str, Any]) -> dict[str, Any]:
    """
    Formally approve a loan application.
    Prerequisites: Human review must be completed with final_decision=APPROVE.
    All compliance checks must have passed.
    """
    required = ["application_id", "approved_amount_usd", "interest_rate"]
    for f in required:
        if f not in params:
            return _validation_error(f"Missing required field: {f}", f)
    try:
        new_version = await handle_approve_application(
            ApproveApplicationCommand(
                application_id=params["application_id"],
                approved_amount_usd=float(params["approved_amount_usd"]),
                interest_rate=float(params["interest_rate"]),
                conditions=params.get("conditions", []),
                approved_by=params.get("approved_by", "auto"),
            ),
            store,
        )
        return {"success": True, "new_stream_version": new_version, "final_state": "FINAL_APPROVED"}
    except OptimisticConcurrencyError as e:
        return _occ_error(e)
    except DomainError as e:
        return _domain_error(e)
    except StreamNotFoundError as e:
        return _not_found_error(e)


async def tool_decline_application(store: EventStore, params: dict[str, Any]) -> dict[str, Any]:
    """
    Formally decline a loan application.
    Prerequisites: Human review completed with final_decision=DECLINE.
    """
    required = ["application_id"]
    for f in required:
        if f not in params:
            return _validation_error(f"Missing required field: {f}", f)
    try:
        new_version = await handle_decline_application(
            DeclineApplicationCommand(
                application_id=params["application_id"],
                decline_reasons=params.get("decline_reasons", []),
                declined_by=params.get("declined_by", "auto"),
                adverse_action_notice_required=bool(params.get("adverse_action_notice_required", False)),
            ),
            store,
        )
        return {"success": True, "new_stream_version": new_version, "final_state": "FINAL_DECLINED"}
    except OptimisticConcurrencyError as e:
        return _occ_error(e)
    except DomainError as e:
        return _domain_error(e)
    except StreamNotFoundError as e:
        return _not_found_error(e)


async def tool_run_integrity_check(store: EventStore, params: dict[str, Any]) -> dict[str, Any]:
    """
    Run a cryptographic integrity check on an entity's event stream.

    Can only be called by compliance role (enforced by caller).
    Rate-limited to 1 check per minute per entity (not enforced here — caller responsibility).

    Prerequisites: entity_type and entity_id must identify an existing stream.
    Returns: check_result with chain_valid and integrity_hash.
    """
    required = ["entity_type", "entity_id"]
    for f in required:
        if f not in params:
            return _validation_error(f"Missing required field: {f}", f)

    result = await run_integrity_check(store, params["entity_type"], params["entity_id"])
    return {
        "success": True,
        "entity_type": result.entity_type,
        "entity_id": result.entity_id,
        "events_verified_count": result.events_verified_count,
        "chain_valid": result.chain_valid,
        "tamper_detected": result.tamper_detected,
        "integrity_hash": result.integrity_hash,
        "checked_at": result.checked_at.isoformat(),
        "error": result.error,
    }
