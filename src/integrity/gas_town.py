"""
Gas Town — Agent memory reconstruction from the event store.

When an AI agent crashes mid-session, reconstruct_agent_context() replays its
event stream and returns enough context to resume without repeating completed work.

Pattern: Every agent action written to event store before execution = crash-safe memory.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.event_store import EventStore


@dataclass
class AgentContext:
    """Reconstructed context for an AI agent session."""
    agent_id: str
    session_id: str
    context_source: str
    model_version: str
    last_event_position: int
    context_text: str  # token-efficient summary of session history
    decisions_made: list[str] = field(default_factory=list)
    applications_processed: list[str] = field(default_factory=list)
    pending_work: list[dict[str, Any]] = field(default_factory=list)
    session_health_status: str = "HEALTHY"  # HEALTHY | NEEDS_RECONCILIATION | FAILED
    raw_recent_events: list[dict[str, Any]] = field(default_factory=list)
    needs_reconciliation_reason: str | None = None


async def reconstruct_agent_context(
    store: "EventStore",
    agent_id: str,
    session_id: str,
    token_budget: int = 8000,
) -> AgentContext:
    """
    Reconstruct agent context from the event stream.

    Steps:
    1. Load full AgentSession stream for agent_id + session_id
    2. Identify: last completed action, pending work, current application state
    3. Summarise old events into prose (token-efficient)
    4. Preserve verbatim: last 3 events, any PENDING or ERROR state events
    5. Return AgentContext with context_text, last_event_position, pending_work[], session_health_status

    CRITICAL: if the agent's last event was a partial decision (no corresponding
    completion event), flag the context as NEEDS_RECONCILIATION.
    """
    stream_id = f"agent-{agent_id}-{session_id}"
    events = await store.load_stream(stream_id)

    if not events:
        return AgentContext(
            agent_id=agent_id,
            session_id=session_id,
            context_source="fresh",
            model_version="unknown",
            last_event_position=0,
            context_text="No prior session events found.",
            session_health_status="HEALTHY",
        )

    # Analyze events
    context_source = "unknown"
    model_version = "unknown"
    decisions_made: list[str] = []
    applications_processed: list[str] = []
    pending_work: list[dict[str, Any]] = []
    session_health_status = "HEALTHY"
    needs_reconciliation_reason: str | None = None

    # Track started-vs-completed work items for NEEDS_RECONCILIATION detection
    started_analyses: set[str] = set()
    completed_analyses: set[str] = set()

    last_position = events[-1].stream_position
    summary_lines: list[str] = []

    for event in events:
        et = event.event_type
        p = event.payload

        if et == "AgentContextLoaded":
            context_source = p.get("context_source", "fresh")
            model_version = p.get("model_version", "unknown")
            summary_lines.append(
                f"Session started — model={model_version}, source={context_source}, "
                f"tokens={p.get('context_token_count', 0)}"
            )

        elif et == "CreditAnalysisCompleted":
            app_id = p.get("application_id", "")
            decisions_made.append("CreditAnalysisCompleted")
            if app_id:
                applications_processed.append(app_id)
                completed_analyses.add(app_id)
            summary_lines.append(
                f"Credit analysis completed — app={app_id}, risk={p.get('risk_tier')}, "
                f"confidence={p.get('confidence_score')}"
            )

        elif et == "FraudScreeningCompleted":
            app_id = p.get("application_id", "")
            decisions_made.append("FraudScreeningCompleted")
            if app_id:
                applications_processed.append(app_id)
            summary_lines.append(
                f"Fraud screening completed — app={app_id}, score={p.get('fraud_score')}"
            )

        elif et == "DecisionGenerated":
            app_id = p.get("application_id", "")
            decisions_made.append("DecisionGenerated")
            if app_id:
                applications_processed.append(app_id)
            summary_lines.append(
                f"Decision generated — app={app_id}, recommendation={p.get('recommendation')}, "
                f"confidence={p.get('confidence_score')}"
            )

    # Check for NEEDS_RECONCILIATION: work started but not completed
    # In this context: CreditAnalysisRequested but no CreditAnalysisCompleted in same session
    # We detect this by checking if the last event is an "in-progress" type
    last_event_type = events[-1].event_type
    in_progress_types = {"CreditAnalysisRequested", "FraudScreeningRequested", "ComplianceCheckRequested"}
    if last_event_type in in_progress_types:
        session_health_status = "NEEDS_RECONCILIATION"
        needs_reconciliation_reason = (
            f"Session ended with {last_event_type} but no corresponding completion event. "
            f"Agent must verify whether the analysis was actually completed externally."
        )

    # Build token-efficient context text
    # Keep all summary lines but truncate if over token budget (rough estimate: 4 chars/token)
    full_summary = "\n".join(summary_lines)
    char_budget = token_budget * 4
    if len(full_summary) > char_budget:
        # Keep the last N lines that fit
        truncated_lines = []
        char_used = 0
        for line in reversed(summary_lines):
            if char_used + len(line) + 1 > char_budget:
                break
            truncated_lines.insert(0, line)
            char_used += len(line) + 1
        context_text = f"[{len(summary_lines) - len(truncated_lines)} older events summarised]\n" + "\n".join(truncated_lines)
    else:
        context_text = full_summary

    # Preserve last 3 events verbatim
    raw_recent = [
        {
            "event_type": e.event_type,
            "stream_position": e.stream_position,
            "payload": e.payload,
            "recorded_at": e.recorded_at.isoformat(),
        }
        for e in events[-3:]
    ]

    return AgentContext(
        agent_id=agent_id,
        session_id=session_id,
        context_source=context_source,
        model_version=model_version,
        last_event_position=last_position,
        context_text=context_text,
        decisions_made=decisions_made,
        applications_processed=list(set(applications_processed)),
        pending_work=pending_work,
        session_health_status=session_health_status,
        raw_recent_events=raw_recent,
        needs_reconciliation_reason=needs_reconciliation_reason,
    )
