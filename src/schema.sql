-- ==========================================================================
-- PostgreSQL Schema for The Ledger — Agentic Event Store
-- ==========================================================================
-- This schema implements an append-only event store with:
--   • Stream-level optimistic concurrency (via event_streams.current_version)
--   • Global ordering (GENERATED ALWAYS AS IDENTITY on global_position)
--   • Outbox pattern for guaranteed downstream delivery
--   • Projection checkpoint tracking for the async daemon
--
-- Column justifications are in DESIGN.md §1.
-- ==========================================================================

-- ---------------------------------------------------
-- events  —  The immutable append-only event log
-- ---------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    event_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Partition key: identifies the stream (e.g. "loan-abc123")
    stream_id        TEXT NOT NULL,
    -- Position within the stream; monotonically increasing per stream
    stream_position  BIGINT NOT NULL,
    -- Cluster-wide monotonic counter for global ordering / catch-up subscriptions
    global_position  BIGINT GENERATED ALWAYS AS IDENTITY,
    -- Fully-qualified event type name (e.g. "CreditAnalysisCompleted")
    event_type       TEXT NOT NULL,
    -- Schema version of this event (enables upcasting chain)
    event_version    SMALLINT NOT NULL DEFAULT 1,
    -- Domain-specific payload; structure varies per event_type+event_version
    payload          JSONB NOT NULL,
    -- Tracing / operational metadata (correlation_id, causation_id, etc.)
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Wall-clock time of write (clock_timestamp avoids transaction-start skew)
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    -- Ensures exactly-once per (stream, position) — the concurrency safety net
    CONSTRAINT uq_stream_position UNIQUE (stream_id, stream_position)
);

-- Fast stream replay
CREATE INDEX IF NOT EXISTS idx_events_stream_id
    ON events (stream_id, stream_position);
-- Catch-up subscription ordering
CREATE INDEX IF NOT EXISTS idx_events_global_pos
    ON events (global_position);
-- Filter by event type (projection routing)
CREATE INDEX IF NOT EXISTS idx_events_type
    ON events (event_type);
-- Time-range queries (regulatory examination windows)
CREATE INDEX IF NOT EXISTS idx_events_recorded
    ON events (recorded_at);

-- ---------------------------------------------------
-- event_streams  —  Stream metadata & version counter
-- ---------------------------------------------------
CREATE TABLE IF NOT EXISTS event_streams (
    stream_id        TEXT PRIMARY KEY,
    -- The aggregate type owning this stream (e.g. "loan", "agent", "compliance")
    aggregate_type   TEXT NOT NULL,
    -- Latest stream_position written; used for optimistic concurrency checks
    current_version  BIGINT NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Soft-delete / archival timestamp (NULL = active)
    archived_at      TIMESTAMPTZ,
    -- Extensible stream-level metadata
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- ---------------------------------------------------
-- projection_checkpoints  —  Async daemon bookkeeping
-- ---------------------------------------------------
CREATE TABLE IF NOT EXISTS projection_checkpoints (
    projection_name  TEXT PRIMARY KEY,
    -- Last global_position successfully processed by this projection
    last_position    BIGINT NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------
-- outbox  —  Transactional outbox for guaranteed delivery
-- ---------------------------------------------------
CREATE TABLE IF NOT EXISTS outbox (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- FK to the originating event
    event_id         UUID NOT NULL REFERENCES events(event_id),
    -- Logical destination (e.g. "kafka-events", "redis-notifications")
    destination      TEXT NOT NULL,
    -- Denormalised payload for the downstream consumer
    payload          JSONB NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- NULL until successfully published; set by the outbox poller
    published_at     TIMESTAMPTZ,
    -- Delivery attempt counter for retry / dead-letter logic
    attempts         SMALLINT NOT NULL DEFAULT 0
);

-- Speed up outbox polling (unpublished events ordered by creation)
CREATE INDEX IF NOT EXISTS idx_outbox_unpublished
    ON outbox (created_at) WHERE published_at IS NULL;
