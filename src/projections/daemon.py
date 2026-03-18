from .event_store import EventStore
from .models.events import StoredEvent
from typing import List, AsyncIterator
import asyncio
from datetime import datetime

class Projection:
    def __init__(self, name: str):
        self.name = name

    async def apply_event(self, event: StoredEvent) -> None:
        raise NotImplementedError

    async def get_checkpoint(self, store: EventStore) -> int:
        async with store._get_connection() as conn:
            row = await conn.fetchrow("SELECT last_position FROM projection_checkpoints WHERE projection_name = $1", self.name)
            return row['last_position'] if row else 0

    async def update_checkpoint(self, store: EventStore, position: int) -> None:
        async with store._get_connection() as conn:
            await conn.execute("""
                INSERT INTO projection_checkpoints (projection_name, last_position, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (projection_name) DO UPDATE SET last_position = $2, updated_at = NOW()
            """, self.name, position)

class ProjectionDaemon:
    def __init__(self, store: EventStore, projections: List[Projection]):
        self._store = store
        self._projections = {p.name: p for p in projections}
        self._running = False

    async def run_forever(self, poll_interval_ms: int = 100) -> None:
        self._running = True
        while self._running:
            await self._process_batch()
            await asyncio.sleep(poll_interval_ms / 1000)

    async def _process_batch(self) -> None:
        # Get lowest checkpoint across all projections
        checkpoints = {}
        for name, proj in self._projections.items():
            checkpoints[name] = await proj.get_checkpoint(self._store)
        min_checkpoint = min(checkpoints.values()) if checkpoints else 0

        # Load events from that position
        events = []
        async for event in self._store.load_all(from_global_position=min_checkpoint, batch_size=100):
            events.append(event)
            if len(events) >= 100:
                break

        if not events:
            return

        # Apply to each projection
        for event in events:
            for proj in self._projections.values():
                await proj.apply_event(event)

        # Update checkpoints
        max_position = max(e.global_position for e in events)
        for proj in self._projections.values():
            await proj.update_checkpoint(self._store, max_position)

    async def get_all_lags(self) -> dict:
        async with self._store._get_connection() as conn:
            row = await conn.fetchrow("SELECT global_position FROM events ORDER BY global_position DESC LIMIT 1")
            latest_global = row['global_position'] if row else 0

        lags = {}
        for name, proj in self._projections.items():
            checkpoint = await proj.get_checkpoint(self._store)
            lags[name] = latest_global - checkpoint
        return lags