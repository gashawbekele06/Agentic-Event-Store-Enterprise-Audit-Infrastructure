"""
EventStore — Async Python class backed by PostgreSQL (asyncpg).

Provides: append (with optimistic concurrency), load_stream, load_all,
stream_version, archive_stream, get_stream_metadata.

Optimistic concurrency is enforced via SELECT … FOR UPDATE row-level
locking on the event_streams table inside the append transaction.
Outbox writes happen in the same transaction for guaranteed delivery.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import asyncpg

from src.models.events import (
    BaseEvent,
    OptimisticConcurrencyError,
    StoredEvent,
    StreamMetadata,
    StreamNotFoundError,
)
from src.upcasting.registry import registry as _upcaster_registry


class EventStore:
    """PostgreSQL-backed event store with optimistic concurrency control."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------
    # Factory / lifecycle
    # ------------------------------------------------------------------

    @classmethod
    async def create(cls, dsn: str, **pool_kwargs: Any) -> EventStore:
        """Create an EventStore backed by a connection-pool to *dsn*."""
        pool = await asyncpg.create_pool(dsn, **pool_kwargs)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def append(
        self,
        stream_id: str,
        events: list[BaseEvent],
        expected_version: int,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> int:
        """Atomically append *events* to *stream_id*.

        Parameters
        ----------
        stream_id:
            Target stream (e.g. ``loan-abc123``).
        events:
            One or more domain events to append.
        expected_version:
            ``-1`` = stream must be **new** (will be created);
            ``N``  = stream's ``current_version`` must equal *N*.
        correlation_id / causation_id:
            Optional tracing metadata stored alongside every event.

        Returns
        -------
        int
            The new stream version after append.

        Raises
        ------
        OptimisticConcurrencyError
            When the stream's actual version differs from *expected_version*.
        StreamNotFoundError
            When *expected_version* >= 0 but the stream does not exist.
        ValueError
            When *events* is empty.
        """
        if not events:
            raise ValueError("Cannot append an empty event list")

        aggregate_type = stream_id.split("-")[0]

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # --- Ensure / lock the stream row -------------------------
                if expected_version == -1:
                    # New stream: try INSERT, detect concurrent creation.
                    row = await conn.fetchrow(
                        """
                        INSERT INTO event_streams
                            (stream_id, aggregate_type, current_version)
                        VALUES ($1, $2, 0)
                        ON CONFLICT (stream_id) DO NOTHING
                        RETURNING current_version
                        """,
                        stream_id,
                        aggregate_type,
                    )
                    if row is None:
                        actual = await conn.fetchval(
                            "SELECT current_version FROM event_streams "
                            "WHERE stream_id = $1",
                            stream_id,
                        )
                        raise OptimisticConcurrencyError(
                            stream_id, -1, actual if actual is not None else 0
                        )
                    current_version = 0
                else:
                    # Existing stream: lock the row and validate version.
                    row = await conn.fetchrow(
                        "SELECT current_version FROM event_streams "
                        "WHERE stream_id = $1 FOR UPDATE",
                        stream_id,
                    )
                    if row is None:
                        raise StreamNotFoundError(stream_id)
                    current_version: int = row["current_version"]
                    if current_version != expected_version:
                        raise OptimisticConcurrencyError(
                            stream_id, expected_version, current_version
                        )

                # --- Build metadata JSON ----------------------------------
                metadata: dict[str, Any] = {}
                if correlation_id:
                    metadata["correlation_id"] = correlation_id
                if causation_id:
                    metadata["causation_id"] = causation_id
                meta_json = json.dumps(metadata)

                # --- Append events ----------------------------------------
                new_version = current_version
                for event in events:
                    new_version += 1
                    event_dict = event.model_dump(mode="json")
                    # Payload = all fields minus the envelope fields
                    payload = {
                        k: v
                        for k, v in event_dict.items()
                        if k not in ("event_type", "event_version")
                    }
                    payload_json = json.dumps(payload, default=str)
                    event_id = uuid4()

                    await conn.execute(
                        """
                        INSERT INTO events
                            (event_id, stream_id, stream_position,
                             event_type, event_version, payload, metadata)
                        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                        """,
                        event_id,
                        stream_id,
                        new_version,
                        event.event_type,
                        event.event_version,
                        payload_json,
                        meta_json,
                    )

                    # Outbox write in the same transaction
                    outbox_payload = json.dumps(
                        {
                            **payload,
                            "event_type": event.event_type,
                            "stream_id": stream_id,
                        },
                        default=str,
                    )
                    await conn.execute(
                        """
                        INSERT INTO outbox (event_id, destination, payload)
                        VALUES ($1, $2, $3::jsonb)
                        """,
                        event_id,
                        "default",
                        outbox_payload,
                    )

                # --- Advance stream version --------------------------------
                await conn.execute(
                    "UPDATE event_streams SET current_version = $1 "
                    "WHERE stream_id = $2",
                    new_version,
                    stream_id,
                )

                return new_version

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def load_stream(
        self,
        stream_id: str,
        from_position: int = 0,
        to_position: int | None = None,
    ) -> list[StoredEvent]:
        """Load events from *stream_id*, upcasted transparently."""
        async with self._pool.acquire() as conn:
            if to_position is not None:
                rows = await conn.fetch(
                    """
                    SELECT event_id, stream_id, stream_position, global_position,
                           event_type, event_version, payload, metadata, recorded_at
                    FROM events
                    WHERE stream_id = $1
                      AND stream_position >= $2
                      AND stream_position <= $3
                    ORDER BY stream_position
                    """,
                    stream_id,
                    from_position,
                    to_position,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT event_id, stream_id, stream_position, global_position,
                           event_type, event_version, payload, metadata, recorded_at
                    FROM events
                    WHERE stream_id = $1
                      AND stream_position >= $2
                    ORDER BY stream_position
                    """,
                    stream_id,
                    from_position,
                )

        return [self._row_to_stored_event(r) for r in rows]

    async def load_all(
        self,
        from_global_position: int = 0,
        event_types: list[str] | None = None,
        batch_size: int = 500,
    ) -> AsyncIterator[StoredEvent]:
        """Async generator over events ordered by global_position.

        Reads in batches of *batch_size*, releasing the connection between
        batches to avoid holding pool resources during slow consumers.
        """
        position = from_global_position
        while True:
            async with self._pool.acquire() as conn:
                if event_types:
                    rows = await conn.fetch(
                        """
                        SELECT event_id, stream_id, stream_position, global_position,
                               event_type, event_version, payload, metadata, recorded_at
                        FROM events
                        WHERE global_position > $1
                          AND event_type = ANY($2::text[])
                        ORDER BY global_position
                        LIMIT $3
                        """,
                        position,
                        event_types,
                        batch_size,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT event_id, stream_id, stream_position, global_position,
                               event_type, event_version, payload, metadata, recorded_at
                        FROM events
                        WHERE global_position > $1
                        ORDER BY global_position
                        LIMIT $2
                        """,
                        position,
                        batch_size,
                    )

            if not rows:
                break

            for r in rows:
                stored = self._row_to_stored_event(r)
                yield stored
                position = r["global_position"]

            if len(rows) < batch_size:
                break

    async def stream_version(self, stream_id: str) -> int:
        """Return the current version of *stream_id* (0 if stream does not exist)."""
        async with self._pool.acquire() as conn:
            v = await conn.fetchval(
                "SELECT current_version FROM event_streams WHERE stream_id = $1",
                stream_id,
            )
            return v if v is not None else 0

    async def archive_stream(self, stream_id: str) -> None:
        """Mark *stream_id* as archived (soft-delete)."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE event_streams SET archived_at = NOW() "
                "WHERE stream_id = $1 AND archived_at IS NULL",
                stream_id,
            )
            if result == "UPDATE 0":
                raise StreamNotFoundError(stream_id)

    async def get_stream_metadata(self, stream_id: str) -> StreamMetadata:
        """Return metadata for *stream_id*."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT stream_id, aggregate_type, current_version,
                       created_at, archived_at, metadata
                FROM event_streams
                WHERE stream_id = $1
                """,
                stream_id,
            )
        if row is None:
            raise StreamNotFoundError(stream_id)
        meta = (
            row["metadata"]
            if isinstance(row["metadata"], dict)
            else json.loads(row["metadata"])
        )
        return StreamMetadata(
            stream_id=row["stream_id"],
            aggregate_type=row["aggregate_type"],
            current_version=row["current_version"],
            created_at=row["created_at"],
            archived_at=row["archived_at"],
            metadata=meta,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _row_to_stored_event(self, row: asyncpg.Record) -> StoredEvent:
        """Convert an asyncpg Record from the events table into a StoredEvent,
        applying upcasting transparently."""
        payload = (
            row["payload"]
            if isinstance(row["payload"], dict)
            else json.loads(row["payload"])
        )
        metadata = (
            row["metadata"]
            if isinstance(row["metadata"], dict)
            else json.loads(row["metadata"])
        )
        stored = StoredEvent(
            event_id=row["event_id"],
            stream_id=row["stream_id"],
            stream_position=row["stream_position"],
            global_position=row["global_position"],
            event_type=row["event_type"],
            event_version=row["event_version"],
            payload=payload,
            metadata=metadata,
            recorded_at=row["recorded_at"],
        )
        return _upcaster_registry.upcast(stored)
