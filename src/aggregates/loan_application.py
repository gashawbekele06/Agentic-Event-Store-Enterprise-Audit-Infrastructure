"""
LoanApplicationAggregate — Full lifecycle state machine for commercial loan applications.

Enforces business rules:
  1. Application state machine (valid transitions only)
  3. Model version locking (no duplicate credit analysis unless override)
  4. Confidence floor (DecisionGenerated < 0.6 → must REFER)
  5. Compliance dependency (cannot approve without all compliance checks passed)
"""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

from src.models.events import DomainError, StoredEvent

if TYPE_CHECKING:
    from src.event_store import EventStore


class ApplicationState(str, Enum):
    SUBMITTED = "SUBMITTED"
    AWAITING_ANALYSIS = "AWAITING_ANALYSIS"
    ANALYSIS_COMPLETE = "ANALYSIS_COMPLETE"
    COMPLIANCE_REVIEW = "COMPLIANCE_REVIEW"
    PENDING_DECISION = "PENDING_DECISION"
    APPROVED_PENDING_HUMAN = "APPROVED_PENDING_HUMAN"
    DECLINED_PENDING_HUMAN = "DECLINED_PENDING_HUMAN"
    FINAL_APPROVED = "FINAL_APPROVED"
    FINAL_DECLINED = "FINAL_DECLINED"


# Valid state transitions — key → set of reachable states
VALID_TRANSITIONS: dict[ApplicationState | None, set[ApplicationState]] = {
    None: {ApplicationState.SUBMITTED},
    ApplicationState.SUBMITTED: {ApplicationState.AWAITING_ANALYSIS},
    ApplicationState.AWAITING_ANALYSIS: {ApplicationState.ANALYSIS_COMPLETE},
    ApplicationState.ANALYSIS_COMPLETE: {ApplicationState.COMPLIANCE_REVIEW},
    ApplicationState.COMPLIANCE_REVIEW: {ApplicationState.PENDING_DECISION},
    ApplicationState.PENDING_DECISION: {
        ApplicationState.APPROVED_PENDING_HUMAN,
        ApplicationState.DECLINED_PENDING_HUMAN,
    },
    ApplicationState.APPROVED_PENDING_HUMAN: {ApplicationState.FINAL_APPROVED},
    ApplicationState.DECLINED_PENDING_HUMAN: {ApplicationState.FINAL_DECLINED},
}


class LoanApplicationAggregate:
    """Consistency boundary for a single commercial loan application.

    State is reconstructed exclusively by replaying events from the
    ``loan-{application_id}`` stream.
    """

    def __init__(self, application_id: str) -> None:
        self.application_id = application_id
        self.version: int = 0
        self.state: ApplicationState | None = None

        # Domain state fields
        self.applicant_id: str | None = None
        self.requested_amount: float | None = None
        self.approved_amount: float | None = None
        self.risk_tier: str | None = None
        self.fraud_score: float | None = None
        self.decision: str | None = None
        self.confidence_score: float | None = None
        self.human_reviewer_id: str | None = None
        self.credit_analysis_done: bool = False
        self.credit_analysis_overridden: bool = False
        self.fraud_screening_done: bool = False
        self.recommended_limit_usd: float | None = None

        # Compliance tracking (cross-aggregate reference)
        self.compliance_checks_required: list[str] = []
        self.compliance_checks_passed: list[str] = []
        self.compliance_checks_failed: list[str] = []

        # Agent session tracking (for causal chain validation)
        self.contributing_agent_sessions: list[str] = []

    # ------------------------------------------------------------------
    # Load from stream (event replay)
    # ------------------------------------------------------------------

    @classmethod
    async def load(cls, store: EventStore, application_id: str) -> LoanApplicationAggregate:
        """Reconstruct aggregate state by replaying the event stream."""
        events = await store.load_stream(f"loan-{application_id}")
        agg = cls(application_id=application_id)
        for event in events:
            agg._apply(event)
        return agg

    # ------------------------------------------------------------------
    # Apply handlers — one per event type
    # ------------------------------------------------------------------

    def _apply(self, event: StoredEvent) -> None:
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler:
            handler(event)
        self.version = event.stream_position

    def _on_ApplicationSubmitted(self, event: StoredEvent) -> None:
        self.state = ApplicationState.SUBMITTED
        self.applicant_id = event.payload.get("applicant_id")
        self.requested_amount = event.payload.get("requested_amount_usd")

    def _on_CreditAnalysisRequested(self, event: StoredEvent) -> None:
        self.state = ApplicationState.AWAITING_ANALYSIS

    def _on_CreditAnalysisCompleted(self, event: StoredEvent) -> None:
        self.state = ApplicationState.ANALYSIS_COMPLETE
        self.credit_analysis_done = True
        self.risk_tier = event.payload.get("risk_tier")
        self.confidence_score = event.payload.get("confidence_score")
        self.recommended_limit_usd = event.payload.get("recommended_limit_usd")

    def _on_FraudScreeningCompleted(self, event: StoredEvent) -> None:
        self.fraud_screening_done = True
        self.fraud_score = event.payload.get("fraud_score")

    def _on_ComplianceReviewStarted(self, event: StoredEvent) -> None:
        self.state = ApplicationState.COMPLIANCE_REVIEW
        self.compliance_checks_required = event.payload.get("checks_required", [])

    def _on_ComplianceRulePassed(self, event: StoredEvent) -> None:
        rule_id = event.payload.get("rule_id", "")
        if rule_id and rule_id not in self.compliance_checks_passed:
            self.compliance_checks_passed.append(rule_id)

    def _on_ComplianceRuleFailed(self, event: StoredEvent) -> None:
        rule_id = event.payload.get("rule_id", "")
        if rule_id and rule_id not in self.compliance_checks_failed:
            self.compliance_checks_failed.append(rule_id)

    def _on_DecisionGenerated(self, event: StoredEvent) -> None:
        self.state = ApplicationState.PENDING_DECISION
        self.decision = event.payload.get("recommendation")
        self.confidence_score = event.payload.get("confidence_score")
        self.contributing_agent_sessions = event.payload.get(
            "contributing_agent_sessions", []
        )

    def _on_HumanReviewCompleted(self, event: StoredEvent) -> None:
        self.human_reviewer_id = event.payload.get("reviewer_id")
        final = event.payload.get("final_decision", "")
        if final == "APPROVE":
            self.state = ApplicationState.APPROVED_PENDING_HUMAN
        else:
            self.state = ApplicationState.DECLINED_PENDING_HUMAN

    def _on_ApplicationApproved(self, event: StoredEvent) -> None:
        self.state = ApplicationState.FINAL_APPROVED
        self.approved_amount = event.payload.get("approved_amount_usd")

    def _on_ApplicationDeclined(self, event: StoredEvent) -> None:
        self.state = ApplicationState.FINAL_DECLINED

    # ------------------------------------------------------------------
    # Assertion helpers (business rules)
    # ------------------------------------------------------------------

    def _assert_transition(self, target: ApplicationState) -> None:
        """Rule 1 — Validate that *target* is a legal transition from current state."""
        allowed = VALID_TRANSITIONS.get(self.state, set())
        if target not in allowed:
            raise DomainError(
                f"Invalid state transition: {self.state} → {target} "
                f"for application {self.application_id}",
                rule="state_machine",
            )

    def assert_submitted(self) -> None:
        if self.state != ApplicationState.SUBMITTED:
            raise DomainError(
                f"Application {self.application_id} must be in SUBMITTED state "
                f"(currently {self.state})",
                rule="state_machine",
            )

    def assert_awaiting_credit_analysis(self) -> None:
        """Application must be in AWAITING_ANALYSIS state."""
        if self.state != ApplicationState.AWAITING_ANALYSIS:
            raise DomainError(
                f"Application {self.application_id} not awaiting credit analysis "
                f"(currently {self.state})",
                rule="state_machine",
            )

    def assert_analysis_complete(self) -> None:
        if self.state != ApplicationState.ANALYSIS_COMPLETE:
            raise DomainError(
                f"Application {self.application_id} analysis not complete "
                f"(currently {self.state})",
                rule="state_machine",
            )

    def assert_compliance_review(self) -> None:
        if self.state != ApplicationState.COMPLIANCE_REVIEW:
            raise DomainError(
                f"Application {self.application_id} not in compliance review "
                f"(currently {self.state})",
                rule="state_machine",
            )

    def assert_pending_decision(self) -> None:
        if self.state != ApplicationState.PENDING_DECISION:
            raise DomainError(
                f"Application {self.application_id} not pending decision "
                f"(currently {self.state})",
                rule="state_machine",
            )

    def assert_no_duplicate_credit_analysis(self) -> None:
        """Rule 3 — No duplicate CreditAnalysisCompleted unless overridden."""
        if self.credit_analysis_done and not self.credit_analysis_overridden:
            raise DomainError(
                f"Credit analysis already completed for application "
                f"{self.application_id}. Must be overridden by HumanReview first.",
                rule="model_version_locking",
            )

    @staticmethod
    def enforce_confidence_floor(recommendation: str, confidence_score: float) -> str:
        """Rule 4 — Confidence < 0.6 forces REFER regardless of analysis.

        Returns the (possibly overridden) recommendation.
        """
        if confidence_score < 0.6:
            return "REFER"
        return recommendation

    def assert_compliance_passed(self) -> None:
        """Rule 5 — All required compliance checks must have passed."""
        if self.compliance_checks_failed:
            raise DomainError(
                f"Cannot approve: compliance rules failed — "
                f"{self.compliance_checks_failed}",
                rule="compliance_dependency",
            )
        missing = set(self.compliance_checks_required) - set(
            self.compliance_checks_passed
        )
        if missing:
            raise DomainError(
                f"Cannot approve: compliance checks incomplete — "
                f"missing {missing}",
                rule="compliance_dependency",
            )

    def assert_can_approve(self) -> None:
        """Composite check for approval readiness."""
        if self.state != ApplicationState.APPROVED_PENDING_HUMAN:
            raise DomainError(
                f"Application {self.application_id} not in APPROVED_PENDING_HUMAN "
                f"state (currently {self.state})",
                rule="state_machine",
            )
        self.assert_compliance_passed()

    def assert_can_decline(self) -> None:
        if self.state != ApplicationState.DECLINED_PENDING_HUMAN:
            raise DomainError(
                f"Application {self.application_id} not in DECLINED_PENDING_HUMAN "
                f"state (currently {self.state})",
                rule="state_machine",
            )

    def assert_approved_amount_within_limit(self, approved_amount_usd: float) -> None:
        """Rule 7 — Approved amount must not exceed agent-assessed recommended limit."""
        limit = self.recommended_limit_usd
        if limit is not None and approved_amount_usd > limit:
            raise DomainError(
                f"Approved amount {approved_amount_usd} exceeds agent-assessed "
                f"maximum {limit}",
                rule="credit_limit_exceeded",
            )

    def assert_human_review_override_reason(self, override: bool, override_reason: str | None) -> None:
        """Rule 8 — An override flag without an explanation reason is not permitted."""
        if override and not override_reason:
            raise DomainError(
                "override_reason is required when override=True",
                rule="human_review_override",
            )

    def assert_causal_chain_valid(
        self,
        contributing_session_stream_ids: list[str],
        agent_aggregates: list,  # list[AgentSessionAggregate] — avoid circular import
    ) -> None:
        """Rule 6 — Every contributing agent session must have produced a decision
        for this application.  All cross-event invariants are encapsulated here so
        handlers only need to load the aggregates and delegate.
        """
        if len(contributing_session_stream_ids) != len(agent_aggregates):
            raise DomainError(
                f"Mismatch between {len(contributing_session_stream_ids)} contributing "
                f"session IDs and {len(agent_aggregates)} loaded agent aggregates",
                rule="causal_chain",
            )
        for stream_id, agent in zip(contributing_session_stream_ids, agent_aggregates):
            if not agent.has_decision_for_application(self.application_id):
                raise DomainError(
                    f"Session {stream_id} has no decision for application "
                    f"{self.application_id} — causal chain violation.",
                    rule="causal_chain",
                )
