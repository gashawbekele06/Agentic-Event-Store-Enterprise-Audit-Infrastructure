"""
ApplicationSummary Projection — One row per loan application, current state.

Updated by the ProjectionDaemon as events arrive.
Provides: loan state, risk tier, fraud score, compliance status, decision.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from src.models.events import StoredEvent
from src.projections import Projection

if TYPE_CHECKING:
    from src.event_store import EventStore

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS projection_application_summary (
    application_id          TEXT PRIMARY KEY,
    state                   TEXT NOT NULL DEFAULT 'SUBMITTED',
    applicant_id            TEXT,
    requested_amount_usd    NUMERIC,
    approved_amount_usd     NUMERIC,
    risk_tier               TEXT,
    fraud_score             NUMERIC,
    compliance_status       TEXT DEFAULT 'PENDING',
    decision                TEXT,
    agent_sessions_completed TEXT[] DEFAULT '{}',
    last_event_type         TEXT,
    last_event_at           TIMESTAMPTZ,
    human_reviewer_id       TEXT,
    final_decision_at       TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


class ApplicationSummaryProjection(Projection):
    """Read-optimised view of every loan application's current state."""

    SUBSCRIBED_EVENTS = {
        "ApplicationSubmitted",
        "CreditAnalysisRequested",
        "CreditAnalysisCompleted",
        "FraudScreeningCompleted",
        "ComplianceReviewStarted",
        "ComplianceRulePassed",
        "ComplianceRuleFailed",
        "DecisionGenerated",
        "HumanReviewCompleted",
        "ApplicationApproved",
        "ApplicationDeclined",
    }

    def __init__(self) -> None:
        super().__init__(name="application_summary")

    async def ensure_schema(self, store: "EventStore") -> None:
        async with store._pool.acquire() as conn:
            await conn.execute(DDL)

    async def apply_event(self, event: StoredEvent) -> None:
        if event.stream_id.startswith("loan-") and event.event_type in self.SUBSCRIBED_EVENTS:
            application_id = event.payload.get("application_id") or event.stream_id[5:]
            await self._handle(event, application_id)

    async def _handle(self, event: StoredEvent, application_id: str) -> None:
        # We need a pool reference — acquired via the store at ProjectionDaemon level
        # The daemon calls apply_event with the store set
        store = self._store  # type: ignore[attr-defined]
        async with store._pool.acquire() as conn:
            await self._upsert_event(conn, event, application_id)

    async def _upsert_event(self, conn: Any, event: StoredEvent, application_id: str) -> None:
        et = event.event_type
        p = event.payload
        timestamp = event.recorded_at

        # Ensure row exists
        await conn.execute(
            """
            INSERT INTO projection_application_summary (application_id, last_event_type, last_event_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (application_id) DO NOTHING
            """,
            application_id, et, timestamp,
        )

        if et == "ApplicationSubmitted":
            await conn.execute(
                """
                UPDATE projection_application_summary SET
                    state='SUBMITTED', applicant_id=$2, requested_amount_usd=$3,
                    last_event_type=$4, last_event_at=$5, updated_at=NOW()
                WHERE application_id=$1
                """,
                application_id, p.get("applicant_id"), p.get("requested_amount_usd"),
                et, timestamp,
            )
        elif et == "CreditAnalysisRequested":
            await conn.execute(
                """
                UPDATE projection_application_summary SET
                    state='AWAITING_ANALYSIS', last_event_type=$2, last_event_at=$3, updated_at=NOW()
                WHERE application_id=$1
                """,
                application_id, et, timestamp,
            )
        elif et == "CreditAnalysisCompleted":
            await conn.execute(
                """
                UPDATE projection_application_summary SET
                    state='ANALYSIS_COMPLETE', risk_tier=$2,
                    last_event_type=$3, last_event_at=$4, updated_at=NOW()
                WHERE application_id=$1
                """,
                application_id, p.get("risk_tier"), et, timestamp,
            )
        elif et == "FraudScreeningCompleted":
            await conn.execute(
                """
                UPDATE projection_application_summary SET
                    fraud_score=$2, last_event_type=$3, last_event_at=$4, updated_at=NOW()
                WHERE application_id=$1
                """,
                application_id, p.get("fraud_score"), et, timestamp,
            )
        elif et in ("ComplianceReviewStarted", "ComplianceCheckRequested"):
            await conn.execute(
                """
                UPDATE projection_application_summary SET
                    state='COMPLIANCE_REVIEW', compliance_status='PENDING',
                    last_event_type=$2, last_event_at=$3, updated_at=NOW()
                WHERE application_id=$1
                """,
                application_id, et, timestamp,
            )
        elif et == "ComplianceRulePassed":
            await conn.execute(
                """
                UPDATE projection_application_summary SET
                    last_event_type=$2, last_event_at=$3, updated_at=NOW()
                WHERE application_id=$1
                """,
                application_id, et, timestamp,
            )
        elif et == "ComplianceRuleFailed":
            await conn.execute(
                """
                UPDATE projection_application_summary SET
                    compliance_status='BLOCKED', last_event_type=$2, last_event_at=$3, updated_at=NOW()
                WHERE application_id=$1
                """,
                application_id, et, timestamp,
            )
        elif et == "DecisionGenerated":
            await conn.execute(
                """
                UPDATE projection_application_summary SET
                    state='PENDING_DECISION', decision=$2,
                    last_event_type=$3, last_event_at=$4, updated_at=NOW()
                WHERE application_id=$1
                """,
                application_id, p.get("recommendation"), et, timestamp,
            )
        elif et == "HumanReviewCompleted":
            final = p.get("final_decision", "")
            new_state = "APPROVED_PENDING_HUMAN" if final == "APPROVE" else "DECLINED_PENDING_HUMAN"
            await conn.execute(
                """
                UPDATE projection_application_summary SET
                    state=$2, human_reviewer_id=$3, decision=$4,
                    last_event_type=$5, last_event_at=$6, updated_at=NOW()
                WHERE application_id=$1
                """,
                application_id, new_state, p.get("reviewer_id"), final, et, timestamp,
            )
        elif et == "ApplicationApproved":
            await conn.execute(
                """
                UPDATE projection_application_summary SET
                    state='FINAL_APPROVED', approved_amount_usd=$2, decision='APPROVE',
                    compliance_status='CLEAR', final_decision_at=$3,
                    last_event_type=$4, last_event_at=$5, updated_at=NOW()
                WHERE application_id=$1
                """,
                application_id, p.get("approved_amount_usd"), timestamp, et, timestamp,
            )
        elif et == "ApplicationDeclined":
            await conn.execute(
                """
                UPDATE projection_application_summary SET
                    state='FINAL_DECLINED', decision='DECLINE', final_decision_at=$2,
                    last_event_type=$3, last_event_at=$4, updated_at=NOW()
                WHERE application_id=$1
                """,
                application_id, timestamp, et, timestamp,
            )
        else:
            await conn.execute(
                """
                UPDATE projection_application_summary SET
                    last_event_type=$2, last_event_at=$3, updated_at=NOW()
                WHERE application_id=$1
                """,
                application_id, et, timestamp,
            )

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    async def get(self, store: "EventStore", application_id: str) -> dict[str, Any] | None:
        async with store._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM projection_application_summary WHERE application_id=$1",
                application_id,
            )
        return dict(row) if row else None

    async def get_all(self, store: "EventStore") -> list[dict[str, Any]]:
        async with store._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM projection_application_summary ORDER BY created_at"
            )
        return [dict(r) for r in rows]

    async def get_by_state(self, store: "EventStore", state: str) -> list[dict[str, Any]]:
        async with store._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM projection_application_summary WHERE state=$1 ORDER BY created_at",
                state,
            )
        return [dict(r) for r in rows]

    async def rebuild_from_scratch(self, store: "EventStore") -> None:
        """Truncate and replay all events from position 0."""
        async with store._pool.acquire() as conn:
            await conn.execute("TRUNCATE projection_application_summary")
        self._store = store  # type: ignore[attr-defined]
        async for event in store.load_all(from_global_position=0):
            await self.apply_event(event)
        await self.update_checkpoint(store, 0)
