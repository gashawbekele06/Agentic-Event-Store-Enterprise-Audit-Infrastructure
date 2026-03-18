import asyncpg
from typing import List, Optional, AsyncIterator
from .models.events import BaseEvent, StoredEvent, StreamMetadata, OptimisticConcurrencyError
import json
from datetime import datetime

class EventStore:
    def __init__(self, connection_string: str):
        self.connection_string = connection_string

    async def _get_connection(self):
        return await asyncpg.connect(self.connection_string)

    async def append(
        self,
        stream_id: str,
        events: List[BaseEvent],
        expected_version: int,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
    ) -> int:
        async with self._get_connection() as conn:
            async with conn.transaction():
                # Check current version
                current_version = await self.stream_version(stream_id)
                if expected_version != -1 and current_version != expected_version:
                    raise OptimisticConcurrencyError(stream_id, expected_version, current_version)

                new_version = current_version
                for event in events:
                    new_version += 1
                    event_dict = event.model_dump()
                    payload = event_dict.copy()
                    # Remove metadata fields from payload
                    for key in ['event_id', 'event_type', 'event_version', 'recorded_at', 'correlation_id', 'causation_id']:
                        payload.pop(key, None)

                    metadata = {
                        'correlation_id': correlation_id,
                        'causation_id': causation_id,
                    }

                    await conn.execute("""
                        INSERT INTO events (event_id, stream_id, stream_position, event_type, event_version, payload, metadata, recorded_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """, event.event_id, stream_id, new_version, event.event_type, event.event_version, json.dumps(payload), json.dumps(metadata), event.recorded_at)

                    # Insert into outbox
                    await conn.execute("""
                        INSERT INTO outbox (event_id, destination, payload)
                        VALUES ($1, $2, $3)
                    """, event.event_id, 'kafka-events', json.dumps(event_dict))

                # Update stream version
                await conn.execute("""
                    INSERT INTO event_streams (stream_id, aggregate_type, current_version)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (stream_id) DO UPDATE SET current_version = $3
                """, stream_id, stream_id.split('-')[0], new_version)

                return new_version

    async def load_stream(
        self,
        stream_id: str,
        from_position: int = 0,
        to_position: Optional[int] = None,
    ) -> List[StoredEvent]:
        async with self._get_connection() as conn:
            rows = await conn.fetch("""
                SELECT event_id, stream_id, stream_position, global_position, event_type, event_version, payload, metadata, recorded_at
                FROM events
                WHERE stream_id = $1 AND stream_position >= $2
                AND ($3 IS NULL OR stream_position <= $3)
                ORDER BY stream_position
            """, stream_id, from_position, to_position)

            return [StoredEvent(
                event_id=row['event_id'],
                stream_id=row['stream_id'],
                stream_position=row['stream_position'],
                global_position=row['global_position'],
                event_type=row['event_type'],
                event_version=row['event_version'],
                payload=row['payload'],
                metadata=row['metadata'],
                recorded_at=row['recorded_at']
            ) for row in rows]

    async def load_all(
        self,
        from_global_position: int = 0,
        event_types: Optional[List[str]] = None,
        batch_size: int = 500,
    ) -> AsyncIterator[StoredEvent]:
        async with self._get_connection() as conn:
            cursor = await conn.cursor("""
                SELECT event_id, stream_id, stream_position, global_position, event_type, event_version, payload, metadata, recorded_at
                FROM events
                WHERE global_position > $1
                AND ($2 IS NULL OR event_type = ANY($2))
                ORDER BY global_position
                LIMIT $3
            """, from_global_position, event_types, batch_size)

            async for row in cursor:
                yield StoredEvent(
                    event_id=row['event_id'],
                    stream_id=row['stream_id'],
                    stream_position=row['stream_position'],
                    global_position=row['global_position'],
                    event_type=row['event_type'],
                    event_version=row['event_version'],
                    payload=row['payload'],
                    metadata=row['metadata'],
                    recorded_at=row['recorded_at']
                )

    async def stream_version(self, stream_id: str) -> int:
        async with self._get_connection() as conn:
            row = await conn.fetchrow("""
                SELECT current_version FROM event_streams WHERE stream_id = $1
            """, stream_id)
            return row['current_version'] if row else 0

    async def archive_stream(self, stream_id: str) -> None:
        async with self._get_connection() as conn:
            await conn.execute("""
                UPDATE event_streams SET archived_at = NOW() WHERE stream_id = $1
            """, stream_id)

    async def get_stream_metadata(self, stream_id: str) -> StreamMetadata:
        async with self._get_connection() as conn:
            row = await conn.fetchrow("""
                SELECT stream_id, aggregate_type, current_version, created_at, archived_at, metadata
                FROM event_streams WHERE stream_id = $1
            """, stream_id)
            if not row:
                raise ValueError(f"Stream {stream_id} not found")
            return StreamMetadata(
                stream_id=row['stream_id'],
                aggregate_type=row['aggregate_type'],
                current_version=row['current_version'],
                created_at=row['created_at'],
                archived_at=row['archived_at'],
                metadata=row['metadata']
            )