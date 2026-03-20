# DESIGN.md — Architectural Decisions & Tradeoff Analysis

## 1. Aggregate Boundary Justification

### Why ComplianceRecord is a Separate Aggregate from LoanApplication

**Decision:** ComplianceRecord (`compliance-{application_id}`) is a distinct aggregate from LoanApplication (`loan-{application_id}`), even though compliance checks are always _about_ a specific loan.

**What would couple if merged:**

If ComplianceRecord events lived in the `loan-{id}` stream:

1. **Write contention multiplies.** Each compliance rule evaluation (there can be 5–15 per application) would compete with agent analysis events for the same stream lock. Under the Apex load profile (100 concurrent applications × 4 agents × ~8 compliance rules), a merged model would see ~3,200 write operations/minute on loan streams, compared to ~1,600 with separated streams. The extra contention increases `OptimisticConcurrencyError` rates by approximately 2× on the loan stream.

2. **Failure domain cross-contamination.** Compliance checks involve external regulation API calls. If a compliance handler fails (API timeout, invalid regulation data), the rolled-back transaction would also discard any co-resident loan events prepared in the same batch. With separated aggregates, compliance failures are isolated to the compliance stream.

3. **Projection coupling.** The `ComplianceAuditView` projection (which supports temporal queries required by regulators) would need to filter compliance events from loan lifecycle events when processing the mixed stream. This adds branching logic and increases processing time for every event.

**The specific failure mode prevented:** Agent A appends `CreditAnalysisCompleted` to `loan-X` at version 7. Simultaneously, the compliance daemon appends `ComplianceRulePassed` to what would be the same stream. With a merged aggregate, one of these fails with a concurrency error, even though they are modifying completely independent aspects of the application. The compliance check must retry despite having nothing to do with credit analysis — this is spurious coupling. With separated aggregates, both writes succeed without interference.

### Schema Column Justifications

| Column                       | Table                  | Justification                                                                                                                        |
| ---------------------------- | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `event_id` (UUID PK)         | events                 | Globally unique identity for every event; enables deduplication, outbox correlation, and external reference                          |
| `stream_id` (TEXT)           | events                 | Partition key — identifies which aggregate instance owns this event; enables efficient stream-level queries                          |
| `stream_position` (BIGINT)   | events                 | Monotonic position within the stream; combined with `stream_id` forms the concurrency check constraint (`uq_stream_position`)        |
| `global_position` (IDENTITY) | events                 | Cluster-wide monotonic counter; enables catch-up subscriptions and global event ordering for projections                             |
| `event_type` (TEXT)          | events                 | Fully-qualified event type name; enables filtered subscriptions and routing to projection handlers                                   |
| `event_version` (SMALLINT)   | events                 | Schema version of the stored payload; enables the upcasting chain (v1→v2→v3) at read time                                            |
| `payload` (JSONB)            | events                 | Domain-specific data; JSONB enables PostgreSQL-native indexing and querying against event content                                    |
| `metadata` (JSONB)           | events                 | Operational tracing data (correlation_id, causation_id); separated from payload to keep domain data clean                            |
| `recorded_at` (TIMESTAMPTZ)  | events                 | Wall-clock write time using `clock_timestamp()` (not `NOW()`) to avoid transaction-start skew; enables time-range regulatory queries |
| `current_version` (BIGINT)   | event_streams          | The latest `stream_position` written; read via `SELECT … FOR UPDATE` during `append()` to enforce optimistic concurrency             |
| `aggregate_type` (TEXT)      | event_streams          | Enables querying all streams of a given aggregate type (e.g., "list all loan streams") without parsing stream_id                     |
| `archived_at` (TIMESTAMPTZ)  | event_streams          | Soft-delete for stream archival; archived streams can be excluded from active queries but remain available for regulatory replay     |
| `last_position` (BIGINT)     | projection_checkpoints | The last `global_position` this projection processed successfully; enables checkpoint-based catch-up replay                          |
| `published_at` (TIMESTAMPTZ) | outbox                 | NULL until successfully published; the outbox poller queries `WHERE published_at IS NULL` to find pending messages                   |
| `attempts` (SMALLINT)        | outbox                 | Delivery retry counter; enables dead-letter logic (skip after N attempts)                                                            |

---

## 2. Projection Strategy

### ApplicationSummary Projection

- **Type:** Async (via ProjectionDaemon)
- **SLO:** 500ms lag (p99)
- **Justification:** The ApplicationSummary is a convenience view for loan officers — not a decision-making input. Eventual consistency at 500ms is acceptable because no automated system uses this view to make irreversible decisions. Inline projection would add ~2ms to every write, compounding to significant latency under burst load (100+ concurrent applications). Async decouples write latency from read-model maintenance.

### AgentPerformanceLedger Projection

- **Type:** Async
- **SLO:** 2 seconds lag
- **Justification:** Agent performance metrics are analytical (model monitoring, A/B comparison). A 2-second lag is invisible to operators reviewing dashboards. The projection aggregates across many events (confidence score averages, approve/decline rates), making inline updates expensive — each event would require a read-modify-write on the aggregation row.

### ComplianceAuditView Projection (Temporal)

- **Type:** Async
- **SLO:** 2 seconds lag
- **Snapshot strategy:** Event-count trigger

**Temporal query support:**

The `get_compliance_at(application_id, timestamp)` method must return the compliance state as it existed at a specific past moment. This requires either:

1. **Full replay from position 0** — accurate but O(n) in event count per query
2. **Periodic snapshots** — reduced replay cost at the expense of storage

**Chosen: Event-count snapshots (every 50 events per application).**

The projection stores a snapshot of the compliance state after every 50th compliance event for each application. A temporal query:

1. Finds the latest snapshot before the requested timestamp
2. Replays events from the snapshot position to the target timestamp
3. Returns the reconstructed state

**Why event-count over time-based triggers:** Compliance events arrive in bursts (all checks for one application run within minutes). A time-based trigger (e.g., every 5 minutes) would miss the burst and create a snapshot after the burst, providing no benefit. An event-count trigger captures state at regular intervals _within_ the burst.

**Snapshot invalidation:** Snapshots are never invalidated — they are immutable, like the events they derive from. A `rebuild_from_scratch()` operation truncates all snapshots and the projection table, then replays from `global_position = 0`. During rebuild, live reads are served from the stream directly (degraded performance, not outage).

---

## 3. Concurrency Analysis

### Expected OptimisticConcurrencyError Rate

**Load profile:** 100 concurrent applications, 4 agents each, processing duration ~500ms per agent.

**Per-stream contention model:**

Each `loan-{id}` stream receives writes from:

- 1 credit analysis agent
- 1 fraud screening agent
- 1 orchestrator (decision generation)
- 1 compliance review trigger
- 1 human review

Total: ~5 write operations per stream per application lifecycle.

Under peak load, the 4 agent writes for a single application may overlap. The probability of two agents writing to the same stream within the same 500ms window:

- Each agent write takes ~5ms (transaction duration including `FOR UPDATE`).
- With 4 agents writing within a 500ms window, the probability of overlap: P ≈ 1 - (1 - 5/500)^(4-1) ≈ 3%.

**Cluster-wide estimate:** At 100 applications, 5 writes each = 500 writes/minute. At 3% collision rate → **~15 `OptimisticConcurrencyError` per minute.**

### Retry Strategy

```
Retry 1:  50ms  backoff + jitter (0–25ms)
Retry 2: 100ms  backoff + jitter (0–50ms)
Retry 3: 200ms  backoff + jitter (0–100ms)
```

**Maximum retry budget: 3 attempts.** After 3 failures, the handler returns a `ConcurrencyRetryExhausted` error to the caller. At a 3% per-attempt failure rate, the probability of 3 consecutive failures is 0.03³ = 0.0027% — effectively zero under normal load.

**When the budget is exhausted:** This indicates either a systematic contention problem (too many agents writing to the same stream simultaneously) or a bug (incorrect expected_version logic). The error is logged at WARN level, and the calling system is expected to backpressure or re-queue the operation.

---

## 4. Upcasting Inference Decisions

### CreditAnalysisCompleted v1 → v2

| New Field          | Inference Strategy                                          | Error Rate                                                  | Downstream Consequence of Error                                                                                                                                                                                                                             |
| ------------------ | ----------------------------------------------------------- | ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `model_version`    | `"legacy-pre-2026"` — deterministic label for all v1 events | **0%** — All v1 events predate model tracking by definition | Performance dashboards show "legacy-pre-2026" as a bucket. Incorrect inference would mix model eras, skewing A/B comparisons                                                                                                                                |
| `confidence_score` | `None` — null                                               | **0%** (no inference = no error)                            | Consumers must handle `null`. The confidence floor rule (≥0.6) would reject `null` scores — so historical decisions cannot be retroactively re-evaluated against the 0.6 threshold, which is correct (the rule didn't exist when those decisions were made) |

**Why null for confidence_score, not an estimated value:**

Fabricating a confidence score (e.g., `0.75` based on the decision outcome) introduces a subtle but dangerous failure mode: downstream systems that filter on confidence ranges would include fabricated scores alongside measured ones, with no way to distinguish them. A regulator auditing "all decisions with confidence < 0.7" would see fabricated data in the results set — this is a compliance violation. Null is honest and forces explicit handling.

### DecisionGenerated v1 → v2

| New Field               | Inference Strategy                                                                                                                                  | Error Rate                                                                                     | Downstream Consequence of Error                                                                                                       |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `model_versions` (dict) | Reconstruct by loading each session referenced in `contributing_agent_sessions` and reading the `model_version` from its `AgentContextLoaded` event | **~2%** — some legacy sessions may not have `AgentContextLoaded` events (pre-Gas Town pattern) | Incomplete `model_versions` dict means some agent model versions are unknown. Performance reporting shows "unknown" for those agents. |

**Performance implication:** This upcaster requires N store lookups (one per contributing agent session). For a decision with 4 contributing sessions, this adds 4 `load_stream()` calls per event load. This is acceptable for individual event loads but becomes expensive during full stream replay (e.g., projection rebuild). Mitigation: cache `AgentContextLoaded` model versions in a lookup table populated during the first rebuild pass.

---

## 5. EventStoreDB Comparison

| This Implementation (PostgreSQL)  | EventStoreDB Equivalent                         | Gap Analysis                                                                                                                 |
| --------------------------------- | ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `stream_id` column                | Stream name (first-class concept)               | EventStoreDB streams are native — no separate metadata table needed                                                          |
| `global_position` identity        | `$all` stream                                   | EventStoreDB's `$all` is a built-in global stream; our `global_position` achieves the same ordering                          |
| `load_all()` async generator      | `$all` catch-up subscription                    | EventStoreDB provides push-based subscriptions with automatic backpressure; our implementation is poll-based                 |
| `ProjectionDaemon`                | Persistent subscriptions + projections system   | EventStoreDB projections are built-in JavaScript projections running inside the database; ours are external Python processes |
| `projection_checkpoints` table    | Persistent subscription checkpoint              | EventStoreDB handles checkpointing automatically for persistent subscriptions                                                |
| `outbox` table                    | Competing consumers on persistent subscriptions | EventStoreDB's persistent subscriptions with consumer groups replace the outbox pattern entirely                             |
| `SELECT … FOR UPDATE` concurrency | `ExpectedVersion` on stream append              | EventStoreDB enforces expected version natively with no SQL; our implementation relies on PostgreSQL row locks               |
| `archived_at` soft-delete         | Stream metadata with `$deleted`                 | EventStoreDB supports stream soft-delete and hard-delete as first-class operations                                           |
| Index on `event_type`             | System projections (`$et-{type}`)               | EventStoreDB automatically creates per-event-type system streams; we must query by index                                     |

**What EventStoreDB gives that our implementation must work harder to achieve:**

1. **Push-based subscriptions.** EventStoreDB pushes events to subscribers as they arrive. Our daemon polls on an interval (100ms default). This adds latency equal to the poll interval — acceptable for our SLOs but inferior to native push.

2. **Built-in projection engine.** EventStoreDB runs JavaScript projection functions inside the database. Our projections run as external Python processes, requiring a separate deployment unit, health monitoring, and restart orchestration.

3. **Native competing consumers.** EventStoreDB's persistent subscriptions with consumer groups distribute event processing across nodes out of the box. Our equivalent requires PostgreSQL advisory locks and external coordination (Redis).

4. **Scavenge (compaction).** EventStoreDB can scavenge (compact) deleted streams. Our implementation retains all data indefinitely; archival is a soft flag with no physical reclamation.

---

## 6. What I Would Do Differently

**The single most significant decision I would reconsider:** The choice to record `CreditAnalysisCompleted` events directly on the `loan-{application_id}` stream rather than only on the `agent-{agent_id}-{session_id}` stream.

**What I built:** The command handler for credit analysis completed appends the `CreditAnalysisCompleted` event to the loan stream, which drives the LoanApplication aggregate's state machine. This is pragmatic — it keeps the state machine transitions in a single stream.

**The problem:** This conflates two concerns. The loan stream should record _what happened to the loan_ (e.g., "CreditAnalysisResultReceived"), while the agent stream should record _what the agent did_ (i.e., the full `CreditAnalysisCompleted` with all its payload). By putting the full analysis event on the loan stream, we have:

1. **Duplicate data.** The credit analysis payload exists in both the loan stream (for the state machine) and ideally the agent stream (for agent accountability). In the current implementation, the agent stream doesn't get a copy unless the handler writes to both streams in separate transactions.

2. **Aggregate boundary leakage.** The LoanApplication aggregate now carries agent-session-level detail (model_version, analysis_duration_ms, input_data_hash) that properly belongs to the AgentSession domain.

3. **Tighter coupling in upcasting.** When `CreditAnalysisCompleted` changes schema (v1→v2), the upcaster affects both the loan stream and the agent stream. With separate event types, each aggregate's events evolve independently.

**What the better version would look like:**

```
AgentSession stream: CreditAnalysisCompleted (full agent output)
LoanApplication stream: CreditAnalysisResultRecorded (lean summary: risk_tier, recommended_limit, agent_session_ref)
```

The command handler writes to both streams: the agent event to the agent stream, and a summary event to the loan stream. This requires either two `append()` calls (with the risk of partial failure) or the outbox pattern to guarantee both writes eventually succeed.

I chose the simpler path for the interim because:

- It follows the challenge's example command handler directly
- It avoids the partial-failure problem of cross-aggregate writes
- The tradeoff (duplicate data, boundary leakage) is tolerable at this project's scale

With another full day, I would implement the dual-write pattern using the outbox: the command handler appends to the agent stream and writes a "pending loan update" to the outbox, which a background process picks up and appends to the loan stream as a `CreditAnalysisResultRecorded` event. This is more complex but architecturally correct.
