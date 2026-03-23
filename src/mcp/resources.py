"""
MCP Resources — The Query Side of The Ledger MCP Server.

6 resources exposing projection read models. Resources NEVER load
aggregate streams except for justified exceptions (audit-trail, agent sessions).
All reads come from pre-built projections to meet SLO targets.

Resource URIs:
  ledger://applications/{id}                → ApplicationSummary
  ledger://applications/{id}/compliance     → ComplianceAuditView (supports ?as_of=)
  ledger://applications/{id}/audit-trail    → AuditLedger direct stream (justified)
  ledger://agents/{id}/performance          → AgentPerformanceLedger
  ledger://agents/{id}/sessions/{session_id} → AgentSession direct stream (justified)
  ledger://ledger/health                    → ProjectionDaemon lag metrics watchdog

SLOs:
  ledger://applications/{id}         p99 < 50ms
  ledger://applications/{id}/compliance  p99 < 200ms
  ledger://applications/{id}/audit-trail p99 < 500ms
  ledger://agents/{id}/performance   p99 < 50ms
  ledger://agents/{id}/sessions/...  p99 < 300ms
  ledger://ledger/health             p99 < 10ms
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.event_store import EventStore
from src.projections import ProjectionDaemon
from src.projections.agent_performance import AgentPerformanceLedgerProjection
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.compliance_audit import ComplianceAuditViewProjection


async def resource_application(
    store: EventStore,
    application_id: str,
    projections: dict[str, Any],
) -> dict[str, Any]:
    """
    ledger://applications/{id}
    Current state of a loan application from the ApplicationSummary projection.
    SLO: p99 < 50ms — reads a single row by primary key.
    """
    proj: ApplicationSummaryProjection = projections.get("application_summary")
    if proj is None:
        return {"error_type": "ConfigurationError", "message": "ApplicationSummary projection not registered"}

    row = await proj.get(store, application_id)
    if row is None:
        return {
            "error_type": "NotFound",
            "message": f"No application found with id={application_id}",
            "suggested_action": "Call submit_application first",
        }

    # Convert non-serialisable types
    for k, v in row.items():
        if isinstance(v, datetime):
            row[k] = v.isoformat()

    return {"application": row}


async def resource_compliance(
    store: EventStore,
    application_id: str,
    projections: dict[str, Any],
    as_of: str | None = None,
) -> dict[str, Any]:
    """
    ledger://applications/{id}/compliance[?as_of=ISO8601]
    Compliance state from the ComplianceAuditView projection.
    Supports temporal query via as_of — returns state as it existed at that timestamp.
    SLO: p99 < 200ms.
    """
    proj: ComplianceAuditViewProjection = projections.get("compliance_audit")
    if proj is None:
        return {"error_type": "ConfigurationError", "message": "ComplianceAuditView projection not registered"}

    if as_of:
        try:
            ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        except ValueError:
            return {
                "error_type": "ValidationError",
                "message": f"as_of must be ISO 8601 timestamp, got: {as_of}",
                "suggested_action": "Use format: 2026-01-15T10:30:00Z",
            }
        result = await proj.get_compliance_at(store, application_id, ts)
    else:
        result = await proj.get_current_compliance(store, application_id)

    if result is None:
        return {
            "error_type": "NotFound",
            "message": f"No compliance records found for application_id={application_id}",
        }

    # Serialise any datetime values
    def _serialize(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {k: _serialize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_serialize(i) for i in obj]
        return obj

    return _serialize(result)


async def resource_audit_trail(
    store: EventStore,
    application_id: str,
    from_ts: str | None = None,
    to_ts: str | None = None,
) -> dict[str, Any]:
    """
    ledger://applications/{id}/audit-trail[?from=ISO8601&to=ISO8601]

    JUSTIFIED EXCEPTION: This resource loads the audit stream directly rather than
    from a projection. Rationale: the AuditLedger is the append-only cross-cutting
    trail — it is small per entity and regulatory consumers always need the full
    event log, not a summarised view. Caching it in a projection table would
    duplicate it without meaningful query benefit.

    SLO: p99 < 500ms.
    """
    audit_stream_id = f"audit-loan-{application_id}"
    loan_stream_id = f"loan-{application_id}"

    # Load both streams
    audit_events = await store.load_stream(audit_stream_id)
    loan_events = await store.load_stream(loan_stream_id)

    all_events = sorted(loan_events + audit_events, key=lambda e: e.global_position)

    # Apply time range filter if provided
    def _parse_ts(s: str) -> datetime | None:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None

    if from_ts:
        from_dt = _parse_ts(from_ts)
        if from_dt:
            all_events = [e for e in all_events if e.recorded_at >= from_dt]

    if to_ts:
        to_dt = _parse_ts(to_ts)
        if to_dt:
            all_events = [e for e in all_events if e.recorded_at <= to_dt]

    return {
        "application_id": application_id,
        "event_count": len(all_events),
        "events": [
            {
                "event_id": str(e.event_id),
                "stream_id": e.stream_id,
                "stream_position": e.stream_position,
                "global_position": e.global_position,
                "event_type": e.event_type,
                "event_version": e.event_version,
                "payload": e.payload,
                "metadata": e.metadata,
                "recorded_at": e.recorded_at.isoformat(),
            }
            for e in all_events
        ],
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


async def resource_agent_performance(
    store: EventStore,
    agent_id: str,
    projections: dict[str, Any],
) -> dict[str, Any]:
    """
    ledger://agents/{id}/performance
    Aggregated performance metrics for this agent from AgentPerformanceLedger.
    SLO: p99 < 50ms.
    """
    proj: AgentPerformanceLedgerProjection = projections.get("agent_performance")
    if proj is None:
        return {"error_type": "ConfigurationError", "message": "AgentPerformanceLedger projection not registered"}

    rows = await proj.get_for_agent(store, agent_id)
    if not rows:
        return {
            "error_type": "NotFound",
            "message": f"No performance data found for agent_id={agent_id}",
        }

    def _serialize(row: dict) -> dict:
        return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}

    return {
        "agent_id": agent_id,
        "model_versions": [_serialize(r) for r in rows],
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


async def resource_agent_session(
    store: EventStore,
    agent_id: str,
    session_id: str,
) -> dict[str, Any]:
    """
    ledger://agents/{id}/sessions/{session_id}

    JUSTIFIED EXCEPTION: Loads the AgentSession stream directly.
    Rationale: session replays are debug/audit operations that always need
    the full immutable event sequence; a projection would lose causal detail.

    SLO: p99 < 300ms.
    """
    stream_id = f"agent-{agent_id}-{session_id}"
    events = await store.load_stream(stream_id)

    if not events:
        return {
            "error_type": "NotFound",
            "message": f"No session found for agent_id={agent_id} session_id={session_id}",
            "suggested_action": "Call start_agent_session first",
        }

    return {
        "agent_id": agent_id,
        "session_id": session_id,
        "stream_id": stream_id,
        "event_count": len(events),
        "events": [
            {
                "event_id": str(e.event_id),
                "stream_position": e.stream_position,
                "event_type": e.event_type,
                "event_version": e.event_version,
                "payload": e.payload,
                "recorded_at": e.recorded_at.isoformat(),
            }
            for e in events
        ],
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


async def resource_ledger_health(
    store: EventStore,
    daemon: ProjectionDaemon | None,
) -> dict[str, Any]:
    """
    ledger://ledger/health
    Watchdog endpoint — returns per-projection lag in events behind latest.
    SLO: p99 < 10ms. Never fails — returns degraded status on error.
    """
    if daemon is None:
        return {
            "status": "degraded",
            "message": "ProjectionDaemon not running",
            "lags": {},
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    try:
        lags = await daemon.get_all_lags()
        # Determine health: any lag > threshold is warn/critical
        max_lag = max(lags.values()) if lags else 0
        status = "healthy" if max_lag < 100 else ("warn" if max_lag < 500 else "critical")
        return {
            "status": status,
            "lags": lags,
            "max_lag_events": max_lag,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return {
            "status": "degraded",
            "error": str(exc),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
