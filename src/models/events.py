from pydantic import BaseModel
from typing import Optional, Dict, Any, List
from datetime import datetime
import uuid

class BaseEvent(BaseModel):
    event_id: str = str(uuid.uuid4())
    event_type: str
    event_version: int = 1
    recorded_at: datetime = datetime.utcnow()

class StoredEvent(BaseEvent):
    stream_id: str
    stream_position: int
    global_position: int
    payload: Dict[str, Any]
    metadata: Dict[str, Any]

class StreamMetadata(BaseModel):
    stream_id: str
    aggregate_type: str
    current_version: int
    created_at: datetime
    archived_at: Optional[datetime]
    metadata: Dict[str, Any]

class OptimisticConcurrencyError(Exception):
    def __init__(self, stream_id: str, expected: int, actual: int):
        self.stream_id = stream_id
        self.expected_version = expected
        self.actual_version = actual
        super().__init__(f"Concurrency error on {stream_id}: expected {expected}, got {actual}")

class DomainError(Exception):
    pass

# Event definitions
class ApplicationSubmitted(BaseEvent):
    event_type: str = "ApplicationSubmitted"
    application_id: str
    applicant_id: str
    requested_amount_usd: float
    loan_purpose: str
    submission_channel: str

class CreditAnalysisCompleted(BaseEvent):
    event_type: str = "CreditAnalysisCompleted"
    application_id: str
    agent_id: str
    session_id: str
    model_version: str
    confidence_score: Optional[float]
    risk_tier: str
    recommended_limit_usd: float
    analysis_duration_ms: int
    input_data_hash: str

class DecisionGenerated(BaseEvent):
    event_type: str = "DecisionGenerated"
    application_id: str
    orchestrator_agent_id: str
    recommendation: str
    confidence_score: float
    contributing_agent_sessions: List[str]
    decision_basis_summary: str
    model_versions: Dict[str, str]

class HumanReviewCompleted(BaseEvent):
    event_type: str = "HumanReviewCompleted"
    application_id: str
    reviewer_id: str
    override: bool
    final_decision: str
    override_reason: Optional[str]

class ApplicationApproved(BaseEvent):
    event_type: str = "ApplicationApproved"
    application_id: str
    approved_amount_usd: float
    interest_rate: float
    conditions: List[str]
    approved_by: str
    effective_date: datetime

class ApplicationDeclined(BaseEvent):
    event_type: str = "ApplicationDeclined"
    application_id: str
    decline_reasons: List[str]
    declined_by: str
    adverse_action_notice_required: bool

class AgentSessionStarted(BaseEvent):
    event_type: str = "AgentSessionStarted"
    agent_id: str
    session_id: str
    agent_type: str
    model_version: str
    context_source: str
    context_token_count: int

class AgentNodeExecuted(BaseEvent):
    event_type: str = "AgentNodeExecuted"
    agent_id: str
    session_id: str
    node_name: str
    node_sequence: int
    input_keys: List[str]
    output_keys: List[str]
    llm_called: bool
    llm_tokens_input: Optional[int]
    llm_tokens_output: Optional[int]
    llm_cost_usd: Optional[float]
    duration_ms: int

class AgentOutputWritten(BaseEvent):
    event_type: str = "AgentOutputWritten"
    agent_id: str
    session_id: str
    events_written: List[Dict[str, Any]]
    output_summary: str

class AgentSessionCompleted(BaseEvent):
    event_type: str = "AgentSessionCompleted"
    agent_id: str
    session_id: str
    total_nodes_executed: int
    total_llm_calls: int
    total_tokens_used: int
    total_cost_usd: float
    next_agent_triggered: Optional[str]

class FraudScreeningCompleted(BaseEvent):
    event_type: str = "FraudScreeningCompleted"
    application_id: str
    agent_id: str
    fraud_score: float
    anomaly_flags: List[str]
    screening_model_version: str
    input_data_hash: str

class ComplianceRulePassed(BaseEvent):
    event_type: str = "ComplianceRulePassed"
    application_id: str
    rule_id: str
    rule_version: str
    evaluation_timestamp: datetime
    evidence_hash: str

class ComplianceRuleFailed(BaseEvent):
    event_type: str = "ComplianceRuleFailed"
    application_id: str
    rule_id: str
    rule_version: str
    failure_reason: str
    remediation_required: bool

class AuditIntegrityCheckRun(BaseEvent):
    event_type: str = "AuditIntegrityCheckRun"
    entity_id: str
    check_timestamp: datetime
    events_verified_count: int
    integrity_hash: str
    previous_hash: str