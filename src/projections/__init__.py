"""
Projection base class and async daemon stub.

Full implementation with fault-tolerant batch processing, per-projection
checkpoint management, and lag metrics is planned for Phase 3.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from src.models.events import StoredEvent

if TYPE_CHECKING:
    from src.event_store import EventStore

logger = logging.getLogger(__name__)


class Projection:
    """Base class for a read-model projection."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._subscribed_event_types: set[str] | None = None  # None = all

    async def apply_event(self, event: StoredEvent) -> None:
        raise NotImplementedError

    async def get_checkpoint(self, store: EventStore) -> int:
        async with store._pool.acquire() as conn:
            row = await conn.fetchval(
                "SELECT last_position FROM projection_checkpoints "
                "WHERE projection_name = $1",
                self.name,
            )
            return row if row is not None else 0

    async def update_checkpoint(self, store: EventStore, position: int) -> None:
        async with store._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO projection_checkpoints
                    (projection_name, last_position, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (projection_name)
                DO UPDATE SET last_position = $2, updated_at = NOW()
                """,
                self.name,
                position,
            )


class ProjectionDaemon:
    """Background async task that keeps projections up-to-date.

    Continuously polls the events table from the last processed
    global_position, routes events to registered projections, and
    updates checkpoints.
    """

    def __init__(self, store: EventStore, projections: list[Projection]) -> None:
        self._store = store
        self._projections = {p.name: p for p in projections}
        self._running = False

    async def run_forever(self, poll_interval_ms: int = 100) -> None:
        self._running = True
        while self._running:
            try:
                await self._process_batch()
            except Exception:
                logger.exception("ProjectionDaemon batch error")
            await asyncio.sleep(poll_interval_ms / 1000)

    def stop(self) -> None:
        self._running = False

    async def _process_batch(self) -> None:
        # Find lowest checkpoint across all projections
        checkpoints: dict[str, int] = {}
        for name, proj in self._projections.items():
            checkpoints[name] = await proj.get_checkpoint(self._store)
        if not checkpoints:
            return
        min_checkpoint = min(checkpoints.values())

        # Load events from that position
        events: list[StoredEvent] = []
        async for event in self._store.load_all(
            from_global_position=min_checkpoint, batch_size=100
        ):
            events.append(event)
            if len(events) >= 100:
                break

        if not events:
            return

        # Route to projections (inject store so projections can acquire connections)
        for event in events:
            for proj in self._projections.values():
                try:
                    proj._store = self._store  # type: ignore[attr-defined]
                    await proj.apply_event(event)
                except Exception:
                    logger.exception(
                        "Projection %s failed on event %s",
                        proj.name,
                        event.event_id,
                    )

        # Update checkpoints
        max_pos = max(e.global_position for e in events)
        for proj in self._projections.values():
            await proj.update_checkpoint(self._store, max_pos)

    async def get_all_lags(self) -> dict[str, int]:
        async with self._store._pool.acquire() as conn:
            row = await conn.fetchval(
                "SELECT MAX(global_position) FROM events"
            )
            latest_global = row if row is not None else 0

        lags: dict[str, int] = {}
        for name, proj in self._projections.items():
            cp = await proj.get_checkpoint(self._store)
            lags[name] = latest_global - cp
        return lags
