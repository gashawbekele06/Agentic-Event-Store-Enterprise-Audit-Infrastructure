from enum import Enum
from typing import List, Optional
from .event_store import EventStore
from .models.events import *

class ApplicationState(Enum):
    SUBMITTED = "SUBMITTED"
    AWAITING_ANALYSIS = "AWAITING_ANALYSIS"
    ANALYSIS_COMPLETE = "ANALYSIS_COMPLETE"
    COMPLIANCE_REVIEW = "COMPLIANCE_REVIEW"
    PENDING_DECISION = "PENDING_DECISION"
    APPROVED_PENDING_HUMAN = "APPROVED_PENDING_HUMAN"
    DECLINED_PENDING_HUMAN = "DECLINED_PENDING_HUMAN"
    FINAL_APPROVED = "FINAL_APPROVED"
    FINAL_DECLINED = "FINAL_DECLINED"

class LoanApplicationAggregate:
    def __init__(self, application_id: str):
        self.application_id = application_id
        self.version = 0
        self.state = ApplicationState.SUBMITTED
        self.applicant_id: Optional[str] = None
        self.requested_amount: Optional[float] = None
        self.approved_amount: Optional[float] = None
        self.risk_tier: Optional[str] = None
        self.compliance_status: Optional[str] = None
        self.decision: Optional[str] = None
        self.human_reviewer_id: Optional[str] = None

    @classmethod
    async def load(cls, store: EventStore, application_id: str) -> "LoanApplicationAggregate":
        events = await store.load_stream(f"loan-{application_id}")
        agg = cls(application_id=application_id)
        for event in events:
            agg._apply(event)
        return agg

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
        if self.state == ApplicationState.SUBMITTED:
            self.state = ApplicationState.AWAITING_ANALYSIS

    def _on_CreditAnalysisCompleted(self, event: StoredEvent) -> None:
        if self.state == ApplicationState.AWAITING_ANALYSIS:
            self.state = ApplicationState.ANALYSIS_COMPLETE
            self.risk_tier = event.payload.get("risk_tier")

    def _on_ComplianceCheckRequested(self, event: StoredEvent) -> None:
        if self.state == ApplicationState.ANALYSIS_COMPLETE:
            self.state = ApplicationState.COMPLIANCE_REVIEW

    def _on_ComplianceRulePassed(self, event: StoredEvent) -> None:
        self.compliance_status = "PASSED"

    def _on_ComplianceRuleFailed(self, event: StoredEvent) -> None:
        self.compliance_status = "FAILED"

    def _on_DecisionGenerated(self, event: StoredEvent) -> None:
        if self.state == ApplicationState.COMPLIANCE_REVIEW:
            self.state = ApplicationState.PENDING_DECISION
            self.decision = event.payload.get("recommendation")

    def _on_HumanReviewCompleted(self, event: StoredEvent) -> None:
        if event.payload.get("override"):
            self.state = ApplicationState.FINAL_APPROVED if event.payload.get("final_decision") == "APPROVE" else ApplicationState.FINAL_DECLINED
        else:
            if self.decision == "APPROVE":
                self.state = ApplicationState.APPROVED_PENDING_HUMAN
            else:
                self.state = ApplicationState.DECLINED_PENDING_HUMAN
        self.human_reviewer_id = event.payload.get("reviewer_id")

    def _on_ApplicationApproved(self, event: StoredEvent) -> None:
        self.state = ApplicationState.FINAL_APPROVED
        self.approved_amount = event.payload.get("approved_amount_usd")

    def _on_ApplicationDeclined(self, event: StoredEvent) -> None:
        self.state = ApplicationState.FINAL_DECLINED

    def assert_awaiting_credit_analysis(self) -> None:
        if self.state != ApplicationState.AWAITING_ANALYSIS:
            raise DomainError(f"Application {self.application_id} not awaiting credit analysis")

    def assert_valid_orchestrator_decision(self, decision: DecisionGenerated) -> None:
        if decision.confidence_score < 0.6:
            raise DomainError("Decision confidence too low")
        if self.compliance_status == "FAILED":
            raise DomainError("Cannot approve with failed compliance")