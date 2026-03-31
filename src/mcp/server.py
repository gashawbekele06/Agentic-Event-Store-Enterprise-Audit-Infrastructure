"""
MCP Server — The Ledger entry point.

Exposes The Ledger as a Model Context Protocol server.
Tools (Commands) write events through command handlers.
Resources (Queries) read from pre-built projections.

Startup sequence:
  1. Connect to PostgreSQL
  2. Apply schema + projection tables
  3. Start ProjectionDaemon as background task
  4. Register tools and resources
  5. Serve MCP over stdio (uvx mcp run) or SSE

Usage:
  python -m src.mcp.server
  uvx mcp run src/mcp/server.py
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from mcp.server.fastmcp import FastMCP

from src.event_store import EventStore
from src.mcp.resources import (
    resource_agent_performance,
    resource_agent_session,
    resource_application,
    resource_audit_trail,
    resource_compliance,
    resource_ledger_health,
)
from src.mcp.tools import (
    tool_approve_application,
    tool_decline_application,
    tool_generate_decision,
    tool_record_compliance_check,
    tool_record_credit_analysis,
    tool_record_fraud_screening,
    tool_record_human_review,
    tool_request_credit_analysis,
    tool_run_integrity_check,
    tool_start_agent_session,
    tool_start_compliance_review,
    tool_submit_application,
)
from src.projections import ProjectionDaemon
from src.projections.agent_performance import AgentPerformanceLedgerProjection
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.compliance_audit import ComplianceAuditViewProjection

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/apex_ledger"
)


# ---------------------------------------------------------------------------
# Global state (set during lifespan)
# ---------------------------------------------------------------------------
_store: EventStore | None = None
_daemon: ProjectionDaemon | None = None
_projections: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(server: Any):
    """Start database pool, projections, and daemon on startup."""
    global _store, _daemon, _projections

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=20)
    _store = EventStore(pool)

    # Apply main schema
    import pathlib
    schema_path = pathlib.Path(__file__).resolve().parents[2] / "schema.sql"
    if schema_path.exists():
        schema_sql = schema_path.read_text()
        async with pool.acquire() as conn:
            await conn.execute(schema_sql)

    # Build projection instances and ensure their tables exist
    app_summary = ApplicationSummaryProjection()
    agent_perf = AgentPerformanceLedgerProjection()
    compliance_audit = ComplianceAuditViewProjection()

    await app_summary.ensure_schema(_store)
    await agent_perf.ensure_schema(_store)
    await compliance_audit.ensure_schema(_store)

    _projections = {
        "application_summary": app_summary,
        "agent_performance": agent_perf,
        "compliance_audit": compliance_audit,
    }

    # Start daemon
    _daemon = ProjectionDaemon(_store, list(_projections.values()))
    daemon_task = asyncio.create_task(
        _daemon.run_forever(poll_interval_ms=100)
    )
    logger.info("ProjectionDaemon started")

    yield

    # Shutdown
    _daemon.stop()
    daemon_task.cancel()
    try:
        await daemon_task
    except asyncio.CancelledError:
        pass
    await pool.close()
    logger.info("Ledger MCP server shutdown complete")


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------
mcp = FastMCP("The Ledger", lifespan=lifespan)


def _get_store() -> EventStore:
    if _store is None:
        raise RuntimeError("EventStore not initialised — lifespan not started")
    return _store


# ---------------------------------------------------------------------------
# Tools (Command side)
# ---------------------------------------------------------------------------

@mcp.tool()
async def submit_application(
    application_id: str,
    applicant_id: str,
    requested_amount_usd: float,
    loan_purpose: str,
    submission_channel: str,
    correlation_id: str = "",
    causation_id: str = "",
) -> dict:
    """
    Submit a new loan application to The Ledger.

    Prerequisites: None — this is the first tool to call for any new application.
    Creates the loan-{application_id} event stream. Duplicate application_id will
    return OptimisticConcurrencyError.

    Returns: stream_id and initial_version on success.
    """
    params = dict(locals())
    return await tool_submit_application(_get_store(), params)


@mcp.tool()
async def start_agent_session(
    agent_id: str,
    session_id: str,
    context_source: str,
    model_version: str,
    event_replay_from_position: int = 0,
    context_token_count: int = 0,
) -> dict:
    """
    Start an AI agent session — MUST be called before any agent decision tools.

    This is the Gas Town anchor: writes AgentContextLoaded as the first event
    in the agent session stream. All subsequent agent decision tools validate
    that this event exists before accepting decisions.

    Prerequisites: None. Always call this BEFORE record_credit_analysis,
    record_fraud_screening, or any other agent action tool.
    Failure to call first returns PreconditionFailed from decision tools.

    Returns: session_id and context_position.
    """
    params = dict(locals())
    return await tool_start_agent_session(_get_store(), params)


@mcp.tool()
async def request_credit_analysis(
    application_id: str,
    assigned_agent_id: str,
    priority: str = "normal",
) -> dict:
    """
    Request credit analysis for a loan application.

    Prerequisites: Application must be in SUBMITTED state.
    Call submit_application first. Transitions to AWAITING_ANALYSIS.

    Returns: new_stream_version.
    """
    params = dict(locals())
    return await tool_request_credit_analysis(_get_store(), params)


@mcp.tool()
async def record_credit_analysis(
    application_id: str,
    agent_id: str,
    session_id: str,
    model_version: str,
    risk_tier: str,
    recommended_limit_usd: float,
    confidence_score: float = 0.0,
    duration_ms: int = 0,
) -> dict:
    """
    Record a completed credit analysis result.

    Prerequisites:
    - Call start_agent_session first (Gas Town requirement).
      Calling without an active session returns PreconditionFailed.
    - Application must be in AWAITING_ANALYSIS state.
    - risk_tier must be one of: LOW, MEDIUM, HIGH.

    Returns: event_id and new_stream_version on success.
    """
    params = dict(locals())
    return await tool_record_credit_analysis(_get_store(), params)


@mcp.tool()
async def record_fraud_screening(
    application_id: str,
    agent_id: str,
    session_id: str,
    fraud_score: float,
    anomaly_flags: list = [],
    screening_model_version: str = "",
) -> dict:
    """
    Record a fraud screening result on the loan stream.

    Prerequisites:
    - Call start_agent_session first (Gas Town requirement).
    - fraud_score must be a float between 0.0 and 1.0 (inclusive).

    Returns: event_id and new_stream_version.
    """
    params = dict(locals())
    return await tool_record_fraud_screening(_get_store(), params)


@mcp.tool()
async def start_compliance_review(
    application_id: str,
    checks_required: list = [],
    regulation_set_version: str = "",
) -> dict:
    """
    Transition a loan application into ComplianceReview state.

    Prerequisites: Credit analysis must be complete (ANALYSIS_COMPLETE state).
    Optionally supply checks_required — list of rule IDs that must pass.

    Returns: new_stream_version.
    """
    params = dict(locals())
    return await tool_start_compliance_review(_get_store(), params)


@mcp.tool()
async def record_compliance_check(
    application_id: str,
    rule_id: str,
    rule_version: str,
    passed: bool,
    failure_reason: str = "",
    remediation_required: bool = False,
    evidence_hash: str = "",
) -> dict:
    """
    Record a compliance rule pass or fail on the compliance stream.

    Prerequisites:
    - Application must be in COMPLIANCE_REVIEW state.
    - Call start_compliance_review first.
    - rule_id should correspond to an active regulation in regulation_set_version.

    Returns: check_id and compliance_status (PASSED | FAILED).
    """
    params = dict(locals())
    return await tool_record_compliance_check(_get_store(), params)


@mcp.tool()
async def generate_decision(
    application_id: str,
    orchestrator_agent_id: str,
    recommendation: str,
    confidence_score: float,
    contributing_agent_sessions: list = [],
    decision_basis_summary: str = "",
    model_versions: dict = {},
) -> dict:
    """
    Generate an AI decision for a loan application.

    Prerequisites:
    - Application must be in COMPLIANCE_REVIEW state.
    - All contributing_agent_sessions must have processed this application.
    - confidence_score < 0.6 will automatically override recommendation to REFER
      (regulatory confidence floor — cannot be bypassed by this tool).
    - recommendation must be: APPROVE, DECLINE, or REFER.

    Returns: decision_id and recommendation (may reflect confidence floor override).
    """
    params = dict(locals())
    return await tool_generate_decision(_get_store(), params)


@mcp.tool()
async def record_human_review(
    application_id: str,
    reviewer_id: str,
    override: bool,
    final_decision: str,
    override_reason: str = "",
) -> dict:
    """
    Record a human loan officer's review decision.

    Prerequisites:
    - Application must be in PENDING_DECISION state (DecisionGenerated must exist).
    - If override=True, override_reason is REQUIRED — domain error if absent.

    After this call:
    - If final_decision=APPROVE → call approve_application to finalise.
    - If final_decision=DECLINE → call decline_application to finalise.

    Returns: final_decision and application_state.
    """
    params = dict(locals())
    return await tool_record_human_review(_get_store(), params)


@mcp.tool()
async def approve_application(
    application_id: str,
    approved_amount_usd: float,
    interest_rate: float,
    conditions: list = [],
    approved_by: str = "human",
) -> dict:
    """
    Formally approve a loan application (terminal state).

    Prerequisites:
    - Human review completed with final_decision=APPROVE.
    - All compliance checks must have passed (domain rule enforced).
    - approved_amount_usd must not exceed the agent-assessed maximum.

    Returns: new_stream_version and final_state=FINAL_APPROVED.
    """
    params = dict(locals())
    return await tool_approve_application(_get_store(), params)


@mcp.tool()
async def decline_application(
    application_id: str,
    decline_reasons: list = [],
    declined_by: str = "human",
    adverse_action_notice_required: bool = False,
) -> dict:
    """
    Formally decline a loan application (terminal state).

    Prerequisites: Human review completed with final_decision=DECLINE.

    Returns: new_stream_version and final_state=FINAL_DECLINED.
    """
    params = dict(locals())
    return await tool_decline_application(_get_store(), params)


@mcp.tool()
async def run_integrity_check(
    entity_type: str,
    entity_id: str,
) -> dict:
    """
    Run a cryptographic SHA-256 hash chain integrity check on an entity's event stream.

    Prerequisites:
    - Only callable by compliance role (enforce at caller level).
    - Should be rate-limited to 1 call per minute per entity.
    - entity_type is typically 'loan', entity_id is the application_id.

    Returns: check_result with chain_valid (bool), tamper_detected (bool),
             events_verified_count, and integrity_hash.
    """
    params = dict(locals())
    return await tool_run_integrity_check(_get_store(), params)


# ---------------------------------------------------------------------------
# Resources (Query side — reads from projections)
# ---------------------------------------------------------------------------

@mcp.resource("ledger://applications/{application_id}")
async def get_application(application_id: str) -> dict:
    """
    Current state of loan application from ApplicationSummary projection.
    SLO: p99 < 50ms. Never replays event streams — single projection row lookup.
    """
    return await resource_application(_get_store(), application_id, _projections)


@mcp.resource("ledger://applications/{application_id}/compliance")
async def get_compliance(application_id: str) -> dict:
    """
    Compliance state from ComplianceAuditView projection.
    Supports temporal query: append ?as_of=2026-01-15T10:30:00Z to get state at a past time.
    SLO: p99 < 200ms.
    """
    return await resource_compliance(_get_store(), application_id, _projections)


@mcp.resource("ledger://applications/{application_id}/audit-trail")
async def get_audit_trail(application_id: str) -> dict:
    """
    Full event audit trail for the application (loan + audit streams).
    JUSTIFIED EXCEPTION: Loads streams directly — regulatory consumers need the
    complete immutable event log, not a summarised projection.
    SLO: p99 < 500ms.
    """
    return await resource_audit_trail(_get_store(), application_id)


@mcp.resource("ledger://agents/{agent_id}/performance")
async def get_agent_performance(agent_id: str) -> dict:
    """
    Aggregated performance metrics per model version for this agent.
    Source: AgentPerformanceLedger projection.
    SLO: p99 < 50ms.
    """
    return await resource_agent_performance(_get_store(), agent_id, _projections)


@mcp.resource("ledger://agents/{agent_id}/sessions/{session_id}")
async def get_agent_session(agent_id: str, session_id: str) -> dict:
    """
    Full event log for a specific agent session.
    JUSTIFIED EXCEPTION: Loads stream directly — debug/audit path needs full causal sequence.
    SLO: p99 < 300ms.
    """
    return await resource_agent_session(_get_store(), agent_id, session_id)


@mcp.resource("ledger://ledger/health")
async def get_health() -> dict:
    """
    Projection lag health check — watchdog endpoint.
    Returns per-projection event lag. Never fails — returns degraded on error.
    SLO: p99 < 10ms.
    """
    return await resource_ledger_health(_get_store(), _daemon)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run()
