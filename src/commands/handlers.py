"""
Command Handlers — Load → Validate → Determine → Append pattern.

Every handler:
  1. Reconstructs current aggregate state from event history
  2. Validates business rules BEFORE any state change
  3. Determines new events (pure logic, no I/O)
  4. Appends atomically with optimistic concurrency
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.aggregates.agent_session import AgentSessionAggregate
from src.aggregates.loan_application import LoanApplicationAggregate
from src.event_store import EventStore
from src.models.events import (
    AgentContextLoaded,
    ApplicationApproved,
    ApplicationDeclined,
    ApplicationSubmitted,
    ComplianceReviewStarted,
    ComplianceRuleFailed,
    ComplianceRulePassed,
    CreditAnalysisCompleted,
    CreditAnalysisRequested,
    DecisionGenerated,
    DocumentUploaded,
    DomainError,
    FraudScreeningCompleted,
    HumanReviewCompleted,
)


async def _append_to_agent_stream_with_retry(
    store: EventStore,
    agent_id: str,
    session_id: str,
    events: list,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    max_attempts: int = 50,
) -> None:
    """Append events to the agent session stream with optimistic concurrency retry.

    Multiple concurrent command handlers may process different applications using
    the same agent session.  Each will independently read agent.version=N and then
    race to append.  This helper reloads the current stream version on each retry
    so all concurrent tasks eventually succeed without data loss.

    max_attempts=50 handles up to 50 concurrent tasks sharing one agent session,
    each needing at most N retries where N = number of competing tasks.
    """
    from src.models.events import OptimisticConcurrencyError
    stream_id = f"agent-{agent_id}-{session_id}"
    for attempt in range(max_attempts):
        current_version = await store.stream_version(stream_id)
        try:
            await store.append(
                stream_id=stream_id,
                events=events,
                expected_version=current_version,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return
        except OptimisticConcurrencyError:
            if attempt == max_attempts - 1:
                raise
            # Yield to the event loop so competing tasks can proceed,
            # then retry immediately with the freshly loaded version.
            await asyncio.sleep(0)


def _hash_inputs(data: dict) -> str:
    """Deterministic SHA-256 of input data for audit trail."""
    canonical = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Command data classes
# ---------------------------------------------------------------------------

@dataclass
class SubmitApplicationCommand:
    application_id: str
    applicant_id: str
    requested_amount_usd: float
    loan_purpose: str
    submission_channel: str
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass
class RequestCreditAnalysisCommand:
    application_id: str
    assigned_agent_id: str
    priority: str = "normal"
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass
class StartAgentSessionCommand:
    agent_id: str
    session_id: str
    context_source: str
    model_version: str
    event_replay_from_position: int = 0
    context_token_count: int = 0
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass
class CreditAnalysisCompletedCommand:
    application_id: str
    agent_id: str
    session_id: str
    model_version: str
    confidence_score: float | None
    risk_tier: str
    recommended_limit_usd: float
    duration_ms: int
    input_data: dict = field(default_factory=dict)
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass
class FraudScreeningCompletedCommand:
    application_id: str
    agent_id: str
    session_id: str
    fraud_score: float
    anomaly_flags: list[str] = field(default_factory=list)
    screening_model_version: str = ""
    input_data: dict = field(default_factory=dict)
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass
class StartComplianceReviewCommand:
    application_id: str
    checks_required: list[str] = field(default_factory=list)
    regulation_set_version: str = ""
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass
class RecordComplianceCheckCommand:
    application_id: str
    rule_id: str
    rule_version: str
    passed: bool
    failure_reason: str = ""
    remediation_required: bool = False
    evidence_hash: str = ""
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass
class GenerateDecisionCommand:
    application_id: str
    orchestrator_agent_id: str
    recommendation: str
    confidence_score: float
    contributing_agent_sessions: list[str] = field(default_factory=list)
    decision_basis_summary: str = ""
    model_versions: dict[str, str] = field(default_factory=dict)
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass
class HumanReviewCompletedCommand:
    application_id: str
    reviewer_id: str
    override: bool
    final_decision: str
    override_reason: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass
class ApproveApplicationCommand:
    application_id: str
    approved_amount_usd: float
    interest_rate: float
    conditions: list[str] = field(default_factory=list)
    approved_by: str = ""
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass
class DeclineApplicationCommand:
    application_id: str
    decline_reasons: list[str] = field(default_factory=list)
    declined_by: str = ""
    adverse_action_notice_required: bool = False
    correlation_id: str | None = None
    causation_id: str | None = None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_submit_application(
    cmd: SubmitApplicationCommand,
    store: EventStore,
) -> int:
    """Submit a new loan application — creates the loan stream."""
    new_events = [
        ApplicationSubmitted(
            application_id=cmd.application_id,
            applicant_id=cmd.applicant_id,
            requested_amount_usd=cmd.requested_amount_usd,
            loan_purpose=cmd.loan_purpose,
            submission_channel=cmd.submission_channel,
        )
    ]
    return await store.append(
        stream_id=f"loan-{cmd.application_id}",
        events=new_events,
        expected_version=-1,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )


async def handle_request_credit_analysis(
    cmd: RequestCreditAnalysisCommand,
    store: EventStore,
) -> int:
    """Request credit analysis — transitions application to AwaitingAnalysis."""
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    app.assert_submitted()

    new_events = [
        CreditAnalysisRequested(
            application_id=cmd.application_id,
            assigned_agent_id=cmd.assigned_agent_id,
            priority=cmd.priority,
        )
    ]
    return await store.append(
        stream_id=f"loan-{cmd.application_id}",
        events=new_events,
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )


async def handle_start_agent_session(
    cmd: StartAgentSessionCommand,
    store: EventStore,
) -> int:
    """Start an agent session — Gas Town pattern.  Must be called before any agent decisions."""
    new_events = [
        AgentContextLoaded(
            agent_id=cmd.agent_id,
            session_id=cmd.session_id,
            context_source=cmd.context_source,
            event_replay_from_position=cmd.event_replay_from_position,
            context_token_count=cmd.context_token_count,
            model_version=cmd.model_version,
        )
    ]
    return await store.append(
        stream_id=f"agent-{cmd.agent_id}-{cmd.session_id}",
        events=new_events,
        expected_version=-1,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )


async def handle_credit_analysis_completed(
    cmd: CreditAnalysisCompletedCommand,
    store: EventStore,
) -> int:
    """Record a completed credit analysis in the loan stream.

    Validates:
      - Application is awaiting analysis (Rule 1)
      - Agent session has context loaded (Rule 2 — Gas Town)
      - No duplicate credit analysis (Rule 3)
    """
    # 1 — Reconstruct aggregates
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    agent = await AgentSessionAggregate.load(store, cmd.agent_id, cmd.session_id)

    # 2 — Validate business rules
    app.assert_awaiting_credit_analysis()
    app.assert_no_duplicate_credit_analysis()
    agent.assert_context_loaded()
    agent.assert_model_version_current(cmd.model_version)

    # 3 — Determine events
    new_events = [
        CreditAnalysisCompleted(
            application_id=cmd.application_id,
            agent_id=cmd.agent_id,
            session_id=cmd.session_id,
            model_version=cmd.model_version,
            confidence_score=cmd.confidence_score,
            risk_tier=cmd.risk_tier,
            recommended_limit_usd=cmd.recommended_limit_usd,
            analysis_duration_ms=cmd.duration_ms,
            input_data_hash=_hash_inputs(cmd.input_data),
        )
    ]

    # 4 — Append atomically to loan stream (aggregate source of truth)
    new_version = await store.append(
        stream_id=f"loan-{cmd.application_id}",
        events=new_events,
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )

    # Also record on agent session stream so the agent aggregate tracks which
    # applications it has processed — required for causal chain validation (Rule 6).
    # Uses retry because multiple concurrent handlers may share the same agent session.
    await _append_to_agent_stream_with_retry(
        store, cmd.agent_id, cmd.session_id, new_events,
        cmd.correlation_id, cmd.causation_id,
    )

    return new_version


async def handle_fraud_screening_completed(
    cmd: FraudScreeningCompletedCommand,
    store: EventStore,
) -> int:
    """Record a fraud screening result on the loan stream."""
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    agent = await AgentSessionAggregate.load(store, cmd.agent_id, cmd.session_id)

    if app.state is None:
        raise DomainError("Application does not exist", rule="state_machine")
    agent.assert_context_loaded()

    if not (0.0 <= cmd.fraud_score <= 1.0):
        raise DomainError(
            f"fraud_score must be 0.0–1.0, got {cmd.fraud_score}",
            rule="fraud_score_range",
        )

    new_events = [
        FraudScreeningCompleted(
            application_id=cmd.application_id,
            agent_id=cmd.agent_id,
            fraud_score=cmd.fraud_score,
            anomaly_flags=cmd.anomaly_flags,
            screening_model_version=cmd.screening_model_version,
            input_data_hash=_hash_inputs(cmd.input_data),
        )
    ]
    new_version = await store.append(
        stream_id=f"loan-{cmd.application_id}",
        events=new_events,
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )

    # Record on agent session stream for causal chain tracking.
    await _append_to_agent_stream_with_retry(
        store, cmd.agent_id, cmd.session_id, new_events,
        cmd.correlation_id, cmd.causation_id,
    )

    return new_version


async def handle_start_compliance_review(
    cmd: StartComplianceReviewCommand,
    store: EventStore,
) -> int:
    """Transition application into ComplianceReview state."""
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    app.assert_analysis_complete()

    new_events = [
        ComplianceReviewStarted(
            application_id=cmd.application_id,
            checks_required=cmd.checks_required,
            regulation_set_version=cmd.regulation_set_version,
        )
    ]
    return await store.append(
        stream_id=f"loan-{cmd.application_id}",
        events=new_events,
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )


async def handle_record_compliance_check(
    cmd: RecordComplianceCheckCommand,
    store: EventStore,
) -> int:
    """Record a compliance rule pass/fail on the loan stream."""
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    app.assert_compliance_review()

    if cmd.passed:
        new_events = [
            ComplianceRulePassed(
                application_id=cmd.application_id,
                rule_id=cmd.rule_id,
                rule_version=cmd.rule_version,
                evidence_hash=cmd.evidence_hash,
            )
        ]
    else:
        new_events = [
            ComplianceRuleFailed(
                application_id=cmd.application_id,
                rule_id=cmd.rule_id,
                rule_version=cmd.rule_version,
                failure_reason=cmd.failure_reason,
                remediation_required=cmd.remediation_required,
            )
        ]
    return await store.append(
        stream_id=f"loan-{cmd.application_id}",
        events=new_events,
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )


async def handle_generate_decision(
    cmd: GenerateDecisionCommand,
    store: EventStore,
) -> int:
    """Generate an AI decision — enforces confidence floor and causal chain.

    Validates:
      - Application is in ComplianceReview (Rule 1)
      - Confidence floor (Rule 4)
      - Causal chain — all contributing sessions must have processed this app (Rule 6)
    """
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    app.assert_compliance_review()

    # Rule 4 — Confidence floor
    recommendation = LoanApplicationAggregate.enforce_confidence_floor(
        cmd.recommendation, cmd.confidence_score
    )

    # Rule 6 — Causal chain enforcement
    for session_stream_id in cmd.contributing_agent_sessions:
        parts = session_stream_id.replace("agent-", "", 1).split("-", 1)
        if len(parts) != 2:
            raise DomainError(
                f"Invalid agent session stream ID: {session_stream_id}",
                rule="causal_chain",
            )
        agent_id, session_id = parts
        agent = await AgentSessionAggregate.load(store, agent_id, session_id)
        if not agent.has_decision_for_application(cmd.application_id):
            raise DomainError(
                f"Session {session_stream_id} has no decision for application "
                f"{cmd.application_id} — causal chain violation.",
                rule="causal_chain",
            )

    new_events = [
        DecisionGenerated(
            application_id=cmd.application_id,
            orchestrator_agent_id=cmd.orchestrator_agent_id,
            recommendation=recommendation,
            confidence_score=cmd.confidence_score,
            contributing_agent_sessions=cmd.contributing_agent_sessions,
            decision_basis_summary=cmd.decision_basis_summary,
            model_versions=cmd.model_versions,
        )
    ]
    return await store.append(
        stream_id=f"loan-{cmd.application_id}",
        events=new_events,
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )


async def handle_human_review_completed(
    cmd: HumanReviewCompletedCommand,
    store: EventStore,
) -> int:
    """Record a human loan officer's review decision."""
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    app.assert_pending_decision()

    if cmd.override and not cmd.override_reason:
        raise DomainError(
            "override_reason is required when override=True",
            rule="human_review_override",
        )

    new_events = [
        HumanReviewCompleted(
            application_id=cmd.application_id,
            reviewer_id=cmd.reviewer_id,
            override=cmd.override,
            final_decision=cmd.final_decision,
            override_reason=cmd.override_reason,
        )
    ]
    return await store.append(
        stream_id=f"loan-{cmd.application_id}",
        events=new_events,
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )


async def handle_approve_application(
    cmd: ApproveApplicationCommand,
    store: EventStore,
) -> int:
    """Formally approve the application (Rule 5 — compliance checks must pass)."""
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    app.assert_can_approve()

    if cmd.approved_amount_usd > (app.recommended_limit_usd or float("inf")):
        raise DomainError(
            f"Approved amount {cmd.approved_amount_usd} exceeds agent-assessed "
            f"maximum {app.recommended_limit_usd}",
            rule="credit_limit_exceeded",
        )

    new_events = [
        ApplicationApproved(
            application_id=cmd.application_id,
            approved_amount_usd=cmd.approved_amount_usd,
            interest_rate=cmd.interest_rate,
            conditions=cmd.conditions,
            approved_by=cmd.approved_by,
        )
    ]
    return await store.append(
        stream_id=f"loan-{cmd.application_id}",
        events=new_events,
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )


async def handle_decline_application(
    cmd: DeclineApplicationCommand,
    store: EventStore,
) -> int:
    """Formally decline the application."""
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    app.assert_can_decline()

    new_events = [
        ApplicationDeclined(
            application_id=cmd.application_id,
            decline_reasons=cmd.decline_reasons,
            declined_by=cmd.declined_by,
            adverse_action_notice_required=cmd.adverse_action_notice_required,
        )
    ]
    return await store.append(
        stream_id=f"loan-{cmd.application_id}",
        events=new_events,
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )


# ---------------------------------------------------------------------------
# Document Upload
# ---------------------------------------------------------------------------

_DOCUMENT_TYPE_MAP = {
    "income_statement":     "INCOME_STATEMENT",
    "balance_sheet":        "BALANCE_SHEET",
    "financial_statements": "FINANCIAL_EXCEL",
    "financial_summary":    "FINANCIAL_CSV",
    "application_proposal": "APPLICATION_PROPOSAL",
}


@dataclass
class UploadDocumentsCommand:
    application_id: str
    company_id: str
    documents_base_dir: str = "documents"
    uploaded_by: str = "system"
    correlation_id: str | None = None


async def handle_upload_documents(
    cmd: UploadDocumentsCommand,
    store: EventStore,
) -> list[str]:
    """
    Scan documents/{company_id}/ and append a DocumentUploaded event
    for each recognised file to the loan stream.
    Returns list of registered file paths.
    """
    from pathlib import Path

    docs_dir = Path(cmd.documents_base_dir) / cmd.company_id
    if not docs_dir.exists():
        raise DomainError(
            f"Documents directory not found: {docs_dir}",
            rule="upload_documents_dir_check",
        )

    app = await LoanApplicationAggregate.load(store, cmd.application_id)

    upload_events = []
    registered: list[str] = []

    for file_path in sorted(docs_dir.iterdir()):
        if not file_path.is_file():
            continue
        stem = file_path.stem.lower()
        doc_type = next(
            (v for k, v in _DOCUMENT_TYPE_MAP.items() if k in stem), None
        )
        if not doc_type:
            continue

        upload_events.append(DocumentUploaded(
            application_id=cmd.application_id,
            document_type=doc_type,
            file_path=str(file_path),
            file_name=file_path.name,
            uploaded_by=cmd.uploaded_by,
        ))
        registered.append(str(file_path))

    if not upload_events:
        raise DomainError(
            f"No recognised documents found in {docs_dir}",
            rule="upload_documents_files_check",
        )

    await store.append(
        stream_id=f"loan-{cmd.application_id}",
        events=upload_events,
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
    )
    return registered
