"""
Pydantic models for all event types in the Apex Financial Services Event Catalogue.
Includes BaseEvent, StoredEvent, StreamMetadata, and custom exceptions.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class OptimisticConcurrencyError(Exception):
    """Raised when stream version does not match expected_version on append."""

    def __init__(self, stream_id: str, expected: int, actual: int):
        self.stream_id = stream_id
        self.expected_version = expected
        self.actual_version = actual
        self.suggested_action = "reload_stream_and_retry"
        super().__init__(
            f"Concurrency conflict on stream '{stream_id}': "
            f"expected version {expected}, actual version {actual}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": "OptimisticConcurrencyError",
            "message": str(self),
            "stream_id": self.stream_id,
            "expected_version": self.expected_version,
            "actual_version": self.actual_version,
            "suggested_action": self.suggested_action,
        }


class DomainError(Exception):
    """Raised when a business rule or invariant is violated."""

    def __init__(self, message: str, rule: str | None = None):
        self.rule = rule
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": "DomainError",
            "message": str(self),
            "rule": self.rule,
        }


class StreamNotFoundError(Exception):
    """Raised when a referenced stream does not exist."""

    def __init__(self, stream_id: str):
        self.stream_id = stream_id
        super().__init__(f"Stream '{stream_id}' not found")


# ---------------------------------------------------------------------------
# Core models
# ---------------------------------------------------------------------------

class BaseEvent(BaseModel):
    """Base class for all domain events passed to EventStore.append().

    Subclasses define their payload fields directly.  The EventStore
    serialises everything except *event_type* and *event_version*
    into the JSONB ``payload`` column.
    """

    event_type: str
    event_version: int = 1

    model_config = {"arbitrary_types_allowed": True}


class StoredEvent(BaseModel):
    """An event as loaded from the PostgreSQL events table."""

    event_id: UUID
    stream_id: str
    stream_position: int
    global_position: int
    event_type: str
    event_version: int
    payload: dict[str, Any]
    metadata: dict[str, Any]
    recorded_at: datetime

    def with_payload(self, payload: dict[str, Any], version: int) -> StoredEvent:
        """Return a copy with a replaced payload and version (used by upcasters).

        The original StoredEvent is never mutated.
        """
        return StoredEvent(
            event_id=self.event_id,
            stream_id=self.stream_id,
            stream_position=self.stream_position,
            global_position=self.global_position,
            event_type=self.event_type,
            event_version=version,
            payload=payload,
            metadata=self.metadata,
            recorded_at=self.recorded_at,
        )


class StreamMetadata(BaseModel):
    """Metadata about an event stream."""

    stream_id: str
    aggregate_type: str
    current_version: int
    created_at: datetime
    archived_at: datetime | None
    metadata: dict[str, Any]


# ---------------------------------------------------------------------------
# LoanApplication aggregate events  (stream: loan-{application_id})
# ---------------------------------------------------------------------------

class ApplicationSubmitted(BaseEvent):
    event_type: str = "ApplicationSubmitted"
    application_id: str
    applicant_id: str
    requested_amount_usd: float
    loan_purpose: str
    submission_channel: str
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CreditAnalysisRequested(BaseEvent):
    event_type: str = "CreditAnalysisRequested"
    application_id: str
    assigned_agent_id: str
    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    priority: str = "normal"


class CreditAnalysisCompleted(BaseEvent):
    """Version 2 adds model_version and confidence_score over v1."""
    event_type: str = "CreditAnalysisCompleted"
    event_version: int = 2
    application_id: str
    agent_id: str
    session_id: str
    model_version: str
    confidence_score: float | None
    risk_tier: str
    recommended_limit_usd: float
    analysis_duration_ms: int
    input_data_hash: str


class FraudScreeningCompleted(BaseEvent):
    event_type: str = "FraudScreeningCompleted"
    application_id: str
    agent_id: str
    fraud_score: float
    anomaly_flags: list[str] = Field(default_factory=list)
    screening_model_version: str
    input_data_hash: str


class ComplianceCheckRequested(BaseEvent):
    event_type: str = "ComplianceCheckRequested"
    application_id: str
    regulation_set_version: str
    checks_required: list[str] = Field(default_factory=list)


class ComplianceRulePassed(BaseEvent):
    event_type: str = "ComplianceRulePassed"
    application_id: str
    rule_id: str
    rule_version: str
    evaluation_timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    evidence_hash: str


class ComplianceRuleFailed(BaseEvent):
    event_type: str = "ComplianceRuleFailed"
    application_id: str
    rule_id: str
    rule_version: str
    failure_reason: str
    remediation_required: bool


class DecisionGenerated(BaseEvent):
    """Version 2 adds model_versions dict over v1."""
    event_type: str = "DecisionGenerated"
    event_version: int = 2
    application_id: str
    orchestrator_agent_id: str
    recommendation: str  # APPROVE | DECLINE | REFER
    confidence_score: float
    contributing_agent_sessions: list[str] = Field(default_factory=list)
    decision_basis_summary: str = ""
    model_versions: dict[str, str] = Field(default_factory=dict)


class HumanReviewCompleted(BaseEvent):
    event_type: str = "HumanReviewCompleted"
    application_id: str
    reviewer_id: str
    override: bool
    final_decision: str  # APPROVE | DECLINE
    override_reason: str | None = None


class ApplicationApproved(BaseEvent):
    event_type: str = "ApplicationApproved"
    application_id: str
    approved_amount_usd: float
    interest_rate: float
    conditions: list[str] = Field(default_factory=list)
    approved_by: str
    effective_date: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ApplicationDeclined(BaseEvent):
    event_type: str = "ApplicationDeclined"
    application_id: str
    decline_reasons: list[str] = Field(default_factory=list)
    declined_by: str
    adverse_action_notice_required: bool


# ---------------------------------------------------------------------------
# Identified missing events (Phase 1 domain exercise)
# ---------------------------------------------------------------------------

class FraudScreeningRequested(BaseEvent):
    """Missing from original catalogue. Tracks when fraud screening is initiated."""
    event_type: str = "FraudScreeningRequested"
    application_id: str
    assigned_agent_id: str
    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ComplianceReviewStarted(BaseEvent):
    """Missing from original catalogue. Transitions LoanApplication to ComplianceReview."""
    event_type: str = "ComplianceReviewStarted"
    application_id: str
    checks_required: list[str] = Field(default_factory=list)
    regulation_set_version: str = ""


# ---------------------------------------------------------------------------
# AgentSession aggregate events  (stream: agent-{agent_id}-{session_id})
# ---------------------------------------------------------------------------

class AgentContextLoaded(BaseEvent):
    """MUST be the first event in any AgentSession stream (Gas Town pattern)."""
    event_type: str = "AgentContextLoaded"
    agent_id: str
    session_id: str
    context_source: str
    event_replay_from_position: int = 0
    context_token_count: int = 0
    model_version: str = ""


# ---------------------------------------------------------------------------
# AuditLedger aggregate events  (stream: audit-{entity_type}-{entity_id})
# ---------------------------------------------------------------------------

class AuditIntegrityCheckRun(BaseEvent):
    event_type: str = "AuditIntegrityCheckRun"
    entity_id: str
    check_timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    events_verified_count: int
    integrity_hash: str
    previous_hash: str
