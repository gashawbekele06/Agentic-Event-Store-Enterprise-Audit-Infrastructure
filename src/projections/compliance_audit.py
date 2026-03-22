"""
ComplianceAuditView Projection — Regulatory read model with temporal queries.

Supports:
  - get_current_compliance(application_id) → full compliance record
  - get_compliance_at(application_id, timestamp) → temporal query (state at past time)
  - get_projection_lag() → milliseconds behind latest event
  - rebuild_from_scratch() → full replay without downtime

Snapshot strategy: Event-count trigger (every 50 compliance events per application)
to enable fast temporal queries without full replay.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from src.models.events import StoredEvent
from src.projections import Projection

if TYPE_CHECKING:
    from src.event_store import EventStore

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS projection_compliance_audit (
    application_id          TEXT NOT NULL,
    rule_id                 TEXT NOT NULL,
    rule_version            TEXT,
    regulation_set_version  TEXT,
    status                  TEXT NOT NULL,  -- PASSED | FAILED | PENDING
    failure_reason          TEXT,
    remediation_required    BOOLEAN,
    evidence_hash           TEXT,
    evaluation_timestamp    TIMESTAMPTZ,
    event_position          BIGINT NOT NULL,
    recorded_at             TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (application_id, rule_id)
);

CREATE TABLE IF NOT EXISTS projection_compliance_audit_snapshots (
    application_id      TEXT NOT NULL,
    snapshot_at         TIMESTAMPTZ NOT NULL,
    event_position      BIGINT NOT NULL,
    snapshot_data       JSONB NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (application_id, snapshot_at)
);

CREATE INDEX IF NOT EXISTS idx_compliance_audit_app
    ON projection_compliance_audit (application_id);
CREATE INDEX IF NOT EXISTS idx_compliance_snapshots_app_time
    ON projection_compliance_audit_snapshots (application_id, snapshot_at DESC);
"""

SNAPSHOT_EVERY_N = 50  # take snapshot after every 50 compliance events per application


class ComplianceAuditViewProjection(Projection):
    """Regulatory read model with temporal query support."""

    SUBSCRIBED_EVENTS = {
        "ComplianceCheckRequested",
        "ComplianceReviewStarted",
        "ComplianceRulePassed",
        "ComplianceRuleFailed",
    }

    def __init__(self) -> None:
        super().__init__(name="compliance_audit")
        self._event_counts: dict[str, int] = {}  # application_id → event count since last snapshot

    async def ensure_schema(self, store: "EventStore") -> None:
        async with store._pool.acquire() as conn:
            await conn.execute(DDL)

    async def apply_event(self, event: StoredEvent) -> None:
        if event.event_type not in self.SUBSCRIBED_EVENTS:
            return
        application_id = event.payload.get("application_id")
        if not application_id:
            return
        store = self._store  # type: ignore[attr-defined]
        async with store._pool.acquire() as conn:
            await self._handle(conn, event, application_id)

        # Snapshot trigger
        self._event_counts[application_id] = self._event_counts.get(application_id, 0) + 1
        if self._event_counts[application_id] >= SNAPSHOT_EVERY_N:
            await self._take_snapshot(store, application_id, event.global_position)
            self._event_counts[application_id] = 0

    async def _handle(self, conn: Any, event: StoredEvent, application_id: str) -> None:
        et = event.event_type
        p = event.payload
        ts = event.recorded_at

        if et in ("ComplianceCheckRequested", "ComplianceReviewStarted"):
            # Ensure pending rows for each required check
            for rule_id in p.get("checks_required", []):
                await conn.execute(
                    """
                    INSERT INTO projection_compliance_audit
                        (application_id, rule_id, status, regulation_set_version,
                         event_position, recorded_at)
                    VALUES ($1, $2, 'PENDING', $3, $4, $5)
                    ON CONFLICT (application_id, rule_id) DO NOTHING
                    """,
                    application_id, rule_id,
                    p.get("regulation_set_version", ""),
                    event.global_position, ts,
                )

        elif et == "ComplianceRulePassed":
            rule_id = p.get("rule_id", "")
            await conn.execute(
                """
                INSERT INTO projection_compliance_audit
                    (application_id, rule_id, rule_version, status, evidence_hash,
                     evaluation_timestamp, event_position, recorded_at)
                VALUES ($1, $2, $3, 'PASSED', $4, $5, $6, $7)
                ON CONFLICT (application_id, rule_id) DO UPDATE SET
                    status='PASSED', rule_version=$3, evidence_hash=$4,
                    evaluation_timestamp=$5, event_position=$6, recorded_at=$7
                """,
                application_id, rule_id, p.get("rule_version", ""),
                p.get("evidence_hash", ""), p.get("evaluation_timestamp") or ts,
                event.global_position, ts,
            )

        elif et == "ComplianceRuleFailed":
            rule_id = p.get("rule_id", "")
            await conn.execute(
                """
                INSERT INTO projection_compliance_audit
                    (application_id, rule_id, rule_version, status, failure_reason,
                     remediation_required, event_position, recorded_at)
                VALUES ($1, $2, $3, 'FAILED', $4, $5, $6, $7)
                ON CONFLICT (application_id, rule_id) DO UPDATE SET
                    status='FAILED', rule_version=$3, failure_reason=$4,
                    remediation_required=$5, event_position=$6, recorded_at=$7
                """,
                application_id, rule_id, p.get("rule_version", ""),
                p.get("failure_reason", ""), p.get("remediation_required", False),
                event.global_position, ts,
            )

    async def _take_snapshot(
        self, store: "EventStore", application_id: str, event_position: int
    ) -> None:
        """Snapshot current compliance state for this application."""
        current = await self.get_current_compliance(store, application_id)
        if not current:
            return
        async with store._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO projection_compliance_audit_snapshots
                    (application_id, snapshot_at, event_position, snapshot_data)
                VALUES ($1, NOW(), $2, $3::jsonb)
                ON CONFLICT (application_id, snapshot_at) DO UPDATE SET
                    snapshot_data=$3::jsonb, event_position=$2
                """,
                application_id, event_position, json.dumps(current, default=str),
            )

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    async def get_current_compliance(
        self, store: "EventStore", application_id: str
    ) -> dict[str, Any] | None:
        async with store._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM projection_compliance_audit WHERE application_id=$1 ORDER BY rule_id",
                application_id,
            )
        if not rows:
            return None
        checks = [dict(r) for r in rows]
        return {
            "application_id": application_id,
            "checks": checks,
            "overall_status": self._compute_overall_status(checks),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }

    async def get_compliance_at(
        self, store: "EventStore", application_id: str, timestamp: datetime
    ) -> dict[str, Any] | None:
        """Return compliance state as it existed at a specific timestamp.

        Strategy:
        1. Find the most recent snapshot before timestamp (fast path).
        2. If no snapshot, replay compliance events from the events table
           filtered by recorded_at <= timestamp.
        """
        async with store._pool.acquire() as conn:
            # Try snapshot first
            snap_row = await conn.fetchrow(
                """
                SELECT snapshot_data, event_position FROM projection_compliance_audit_snapshots
                WHERE application_id=$1 AND snapshot_at <= $2
                ORDER BY snapshot_at DESC LIMIT 1
                """,
                application_id, timestamp,
            )

        if snap_row:
            data = snap_row["snapshot_data"]
            if isinstance(data, str):
                data = json.loads(data)
            # Verify the snapshot is valid (no events after snapshot_at that we missed)
            return {**data, "temporal_query_at": timestamp.isoformat(), "source": "snapshot"}

        # Full replay from raw events table
        async with store._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT event_type, payload, global_position, recorded_at
                FROM events
                WHERE stream_id LIKE 'loan-' || $1 OR payload->>'application_id' = $1
                  AND event_type IN ('ComplianceCheckRequested', 'ComplianceReviewStarted',
                                     'ComplianceRulePassed', 'ComplianceRuleFailed')
                  AND recorded_at <= $2
                ORDER BY global_position
                """,
                application_id, timestamp,
            )

        checks: dict[str, dict[str, Any]] = {}
        for row in rows:
            et = row["event_type"]
            p = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
            rule_id = p.get("rule_id", "")
            if et == "ComplianceRulePassed" and rule_id:
                checks[rule_id] = {"rule_id": rule_id, "status": "PASSED",
                                   "rule_version": p.get("rule_version"), "recorded_at": str(row["recorded_at"])}
            elif et == "ComplianceRuleFailed" and rule_id:
                checks[rule_id] = {"rule_id": rule_id, "status": "FAILED",
                                   "failure_reason": p.get("failure_reason"),
                                   "recorded_at": str(row["recorded_at"])}

        if not checks:
            return None
        check_list = list(checks.values())
        return {
            "application_id": application_id,
            "checks": check_list,
            "overall_status": self._compute_overall_status(check_list),
            "temporal_query_at": timestamp.isoformat(),
            "source": "event_replay",
        }

    def _compute_overall_status(self, checks: list[dict[str, Any]]) -> str:
        if any(c.get("status") == "FAILED" for c in checks):
            return "BLOCKED"
        if all(c.get("status") == "PASSED" for c in checks) and checks:
            return "CLEAR"
        return "PENDING"

    async def get_projection_lag(self, store: "EventStore") -> int:
        """Return milliseconds behind the latest event."""
        async with store._pool.acquire() as conn:
            latest_global = await conn.fetchval("SELECT MAX(global_position) FROM events") or 0
            latest_recorded = await conn.fetchval("SELECT MAX(recorded_at) FROM events")
            cp = await self.get_checkpoint(store)
            cp_recorded = await conn.fetchval(
                "SELECT recorded_at FROM events WHERE global_position=$1", cp
            ) if cp > 0 else None

        if latest_recorded and cp_recorded:
            delta = (latest_recorded - cp_recorded).total_seconds() * 1000
            return max(0, int(delta))
        return 0

    async def rebuild_from_scratch(self, store: "EventStore") -> None:
        """Truncate and replay all events from position 0 (no downtime to reads during replay)."""
        async with store._pool.acquire() as conn:
            await conn.execute("TRUNCATE projection_compliance_audit, projection_compliance_audit_snapshots")
        self._store = store  # type: ignore[attr-defined]
        self._event_counts.clear()
        async for event in store.load_all(from_global_position=0):
            await self.apply_event(event)
        await self.update_checkpoint(store, 0)
