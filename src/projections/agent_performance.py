"""
AgentPerformanceLedger Projection — Aggregated metrics per AI agent model version.

Answers: "Has agent v2.3 been making systematically different decisions than v2.2?"
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from src.models.events import StoredEvent
from src.projections import Projection

if TYPE_CHECKING:
    from src.event_store import EventStore

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS projection_agent_performance (
    agent_id                TEXT NOT NULL,
    model_version           TEXT NOT NULL,
    analyses_completed      INTEGER NOT NULL DEFAULT 0,
    decisions_generated     INTEGER NOT NULL DEFAULT 0,
    avg_confidence_score    NUMERIC,
    avg_duration_ms         NUMERIC,
    approve_count           INTEGER NOT NULL DEFAULT 0,
    decline_count           INTEGER NOT NULL DEFAULT 0,
    refer_count             INTEGER NOT NULL DEFAULT 0,
    human_override_count    INTEGER NOT NULL DEFAULT 0,
    first_seen_at           TIMESTAMPTZ,
    last_seen_at            TIMESTAMPTZ,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (agent_id, model_version)
);
"""


class AgentPerformanceLedgerProjection(Projection):
    """Aggregated performance metrics per AI agent model version."""

    SUBSCRIBED_EVENTS = {
        "CreditAnalysisCompleted",
        "FraudScreeningCompleted",
        "DecisionGenerated",
        "HumanReviewCompleted",
        "AgentContextLoaded",
    }

    def __init__(self) -> None:
        super().__init__(name="agent_performance")

    async def ensure_schema(self, store: "EventStore") -> None:
        async with store._pool.acquire() as conn:
            await conn.execute(DDL)

    async def apply_event(self, event: StoredEvent) -> None:
        if event.event_type not in self.SUBSCRIBED_EVENTS:
            return
        store = self._store  # type: ignore[attr-defined]
        async with store._pool.acquire() as conn:
            await self._handle(conn, event)

    async def _handle(self, conn: Any, event: StoredEvent) -> None:
        et = event.event_type
        p = event.payload
        ts = event.recorded_at

        if et == "CreditAnalysisCompleted":
            agent_id = p.get("agent_id", "unknown")
            model_version = p.get("model_version", "unknown")
            confidence = p.get("confidence_score")
            duration_ms = p.get("analysis_duration_ms", 0)

            await conn.execute(
                """
                INSERT INTO projection_agent_performance
                    (agent_id, model_version, analyses_completed, avg_confidence_score,
                     avg_duration_ms, first_seen_at, last_seen_at)
                VALUES ($1, $2, 1, $3, $4, $5, $5)
                ON CONFLICT (agent_id, model_version) DO UPDATE SET
                    analyses_completed = projection_agent_performance.analyses_completed + 1,
                    avg_confidence_score = CASE
                        WHEN $3 IS NOT NULL THEN
                            (projection_agent_performance.avg_confidence_score *
                             projection_agent_performance.analyses_completed + $3) /
                            (projection_agent_performance.analyses_completed + 1)
                        ELSE projection_agent_performance.avg_confidence_score
                    END,
                    avg_duration_ms = (projection_agent_performance.avg_duration_ms *
                                       projection_agent_performance.analyses_completed + $4) /
                                      (projection_agent_performance.analyses_completed + 1),
                    last_seen_at = GREATEST(projection_agent_performance.last_seen_at, $5),
                    updated_at = NOW()
                """,
                agent_id, model_version, confidence, float(duration_ms), ts,
            )

        elif et == "DecisionGenerated":
            agent_id = p.get("orchestrator_agent_id", "unknown")
            recommendation = p.get("recommendation", "")
            model_versions = p.get("model_versions", {})
            model_version = model_versions.get("orchestrator", "unknown")

            col_map = {"APPROVE": "approve_count", "DECLINE": "decline_count", "REFER": "refer_count"}
            col = col_map.get(recommendation, "refer_count")

            await conn.execute(
                f"""
                INSERT INTO projection_agent_performance
                    (agent_id, model_version, decisions_generated, {col}, first_seen_at, last_seen_at)
                VALUES ($1, $2, 1, 1, $3, $3)
                ON CONFLICT (agent_id, model_version) DO UPDATE SET
                    decisions_generated = projection_agent_performance.decisions_generated + 1,
                    {col} = projection_agent_performance.{col} + 1,
                    last_seen_at = GREATEST(projection_agent_performance.last_seen_at, $3),
                    updated_at = NOW()
                """,
                agent_id, model_version, ts,
            )

        elif et == "HumanReviewCompleted":
            override = p.get("override", False)
            if override:
                # We don't have agent_id directly; update all rows for this session's agent
                # Use a best-effort attribution by incrementing the most recent agent
                await conn.execute(
                    """
                    UPDATE projection_agent_performance SET
                        human_override_count = human_override_count + 1,
                        updated_at = NOW()
                    WHERE last_seen_at = (
                        SELECT MAX(last_seen_at) FROM projection_agent_performance
                        WHERE decisions_generated > 0
                    )
                    """
                )

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    async def get_all(self, store: "EventStore") -> list[dict[str, Any]]:
        async with store._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *,
                    CASE WHEN (approve_count + decline_count + refer_count) > 0
                         THEN approve_count::numeric / (approve_count + decline_count + refer_count)
                         ELSE NULL END AS approve_rate,
                    CASE WHEN (approve_count + decline_count + refer_count) > 0
                         THEN decline_count::numeric / (approve_count + decline_count + refer_count)
                         ELSE NULL END AS decline_rate,
                    CASE WHEN (approve_count + decline_count + refer_count) > 0
                         THEN refer_count::numeric / (approve_count + decline_count + refer_count)
                         ELSE NULL END AS refer_rate
                FROM projection_agent_performance
                ORDER BY agent_id, model_version
                """
            )
        return [dict(r) for r in rows]

    async def get_for_agent(self, store: "EventStore", agent_id: str) -> list[dict[str, Any]]:
        async with store._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM projection_agent_performance WHERE agent_id=$1 ORDER BY last_seen_at DESC",
                agent_id,
            )
        return [dict(r) for r in rows]

    async def rebuild_from_scratch(self, store: "EventStore") -> None:
        async with store._pool.acquire() as conn:
            await conn.execute("TRUNCATE projection_agent_performance")
        self._store = store  # type: ignore[attr-defined]
        async for event in store.load_all(from_global_position=0):
            await self.apply_event(event)
