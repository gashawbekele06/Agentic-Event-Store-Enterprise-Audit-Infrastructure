# DOMAIN_NOTES.md

## 1. EDA vs. ES Distinction

**Question:** A component uses callbacks (like LangChain traces) to capture event-like data. Is this Event-Driven Architecture (EDA) or Event Sourcing (ES)? If you redesigned it using The Ledger, what exactly would change?

**Answer:**

This is **Event-Driven Architecture (EDA)**, not Event Sourcing.

The callback model is fire-and-forget: the component emits a signal that something happened, interested subscribers process it, and the event itself has no durable identity. If the subscriber is offline, the event is lost. If the process restarts, all accumulated state vanishes. LangChain traces are a diagnostic overlay — they capture what happened _for observation_, but they are not the source of truth for _what the system's state is_.

Event Sourcing is fundamentally different. In ES, the events **are** the database. Current state is derived exclusively by replaying the event stream. The distinction is not about syntax (both emit "event" objects); it is about **authority**:

| Dimension    | EDA (Callbacks/Traces)                                   | Event Sourcing (The Ledger)                                   |
| ------------ | -------------------------------------------------------- | ------------------------------------------------------------- |
| Authority    | State lives in a mutable store; events are notifications | Events ARE the store; state is computed from events           |
| Durability   | Events may be dropped or lost                            | Events are append-only, ACID-persisted, never deleted         |
| Replay       | Not possible — no ordered, complete event history        | Core feature — replay from any point to reconstruct state     |
| Ordering     | No global order guarantee                                | Global ordering via `global_position` identity column         |
| Immutability | Events can be silently dropped or modified               | Stored events are never mutated (upcasting is read-time only) |

**If redesigned using The Ledger:**

1. **Agent actions become events in named streams.** Instead of a callback `on_agent_complete(trace)`, the agent calls `store.append(stream_id="agent-{id}-{session}", events=[AgentContextLoaded(...), CreditAnalysisCompleted(...)], expected_version=...)`. The event is persisted in PostgreSQL with ACID guarantees before the action continues.

2. **State reconstruction replaces mutable state.** The Automaton Auditor's current "verdict" would no longer be a variable in memory — it would be the result of replaying the `audit-automaton-{id}` stream. Process crash → restart → replay stream → full state recovery. This is the Gas Town pattern.

3. **Cross-system integration via shared streams.** Week 1's governance hooks currently produce a log that "no other system reads." With The Ledger, governance decisions are events in the `audit-governance-{entity}` stream. The Auditor subscribes via `load_all(event_types=["GovernanceDecision"])`. The Cartographer reads the same stream to build its lineage graph. One write, many readers — this is CQRS.

4. **Temporal queries become possible.** "What was the compliance state of application X at 2:15 PM yesterday?" is answerable by replaying the stream up to that timestamp. With EDA callbacks, this question is unanswerable — the data was never stored with sufficient structure.

**What you gain:** Auditability, reproducibility, temporal queries, crash recovery, and a shared memory layer across all agents. **What you lose:** Simplicity. Event sourcing requires aggregate design, version management, projection maintenance, and upcasting infrastructure. It is not free — but for regulated, multi-agent systems, it is unavoidable.

---

## 2. The Aggregate Question

**Question:** You will build four aggregates. Identify one alternative boundary you considered and rejected. What coupling problem does your chosen boundary prevent?

**Answer:**

**The four aggregates and their boundaries:**

| Aggregate          | Stream Pattern                    | Boundary Rationale                                                       |
| ------------------ | --------------------------------- | ------------------------------------------------------------------------ |
| `LoanApplication`  | `loan-{application_id}`           | Full loan lifecycle — single application is the consistency unit         |
| `AgentSession`     | `agent-{agent_id}-{session_id}`   | One agent's work in one session — isolates agent state from loan state   |
| `ComplianceRecord` | `compliance-{application_id}`     | Regulatory checks per application — separate compliance lifecycle        |
| `AuditLedger`      | `audit-{entity_type}-{entity_id}` | Cross-cutting integrity chain — must be independent of domain aggregates |

**Alternative considered and rejected: Merging ComplianceRecord into LoanApplication.**

The argument for merging: compliance checks are always about a specific loan application, so why not make them events in the `loan-{id}` stream? The aggregate would have a richer state model with compliance fields directly embedded.

**Why rejected — the coupling problem:**

1. **Write contention under concurrent operations.** In the Apex scenario, compliance checks can run in parallel with credit analysis updates. If both write to the same `loan-{id}` stream, every compliance rule evaluation competes for the same stream lock with every credit analysis update. With 4 agents and multiple compliance rules per application, this creates a contention bottleneck. At 100 concurrent applications × 4 agents × 5 compliance rules, the merged aggregate would see roughly 20× more optimistic concurrency conflicts than the separated model. The separated model allows the compliance agent to write `ComplianceRulePassed` events to `compliance-{id}` with zero contention against the credit agent writing `CreditAnalysisCompleted` to `loan-{id}`.

2. **Failure domain expansion.** If a compliance check handler throws an exception (bad regulation data, network timeout to the regulation API), it would roll back the entire loan aggregate's transaction — including any co-resident events. With separated aggregates, a compliance failure affects only the compliance stream. The loan continues its lifecycle independently.

3. **Independent replay and projection.** Compliance has its own temporal query requirements (regulators need to see compliance state at a specific time). With a separate stream, the `ComplianceAuditView` projection subscribes only to compliance events. If merged, the projection would need to filter loan lifecycle events from compliance events, adding complexity and increasing the replay surface.

The coupling prevented: **Write-path mutual interference between independent lifecycle domains**. Credit analysis and compliance evaluation have different rates of change, different failure modes, and different temporal query requirements. Merging them would force the slower, more failure-prone domain (external regulation lookups) to block the faster domain (agent analysis), violating the principle that aggregates should have independent write throughput.

---

## 3. Concurrency in Practice

**Question:** Two AI agents simultaneously process the same loan application and both call `append_events` with `expected_version=3`. Trace the exact sequence of operations. What does the losing agent receive, and what must it do next?

**Answer:**

**Exact sequence of operations (PostgreSQL level):**

```
TIME    AGENT A                                 AGENT B
────    ───────                                 ───────
t0      BEGIN TRANSACTION                       BEGIN TRANSACTION
t1      SELECT current_version                  SELECT current_version
        FROM event_streams                      FROM event_streams
        WHERE stream_id = 'loan-X'              WHERE stream_id = 'loan-X'
        FOR UPDATE                              FOR UPDATE
        → Acquires row lock, reads 3            → BLOCKED (row locked by A)

t2      current_version = 3 ✓
        INSERT INTO events (... stream_position=4 ...)
        INSERT INTO outbox (...)
        UPDATE event_streams SET current_version = 4

t3      COMMIT                                  → Lock released, B proceeds
                                                → reads current_version = 4

t4                                              expected_version (3) ≠ actual (4)
                                                → RAISE OptimisticConcurrencyError
                                                ROLLBACK
```

**Key mechanism:** The `SELECT ... FOR UPDATE` on the `event_streams` row serialises concurrent writers to the same stream. Agent B's transaction is held (not rejected) at t1 until Agent A commits at t3. Once B reads the now-updated version, the mismatch is detected and the error is raised.

**What the losing agent receives:**

```python
OptimisticConcurrencyError(
    stream_id="loan-X",
    expected_version=3,
    actual_version=4,
    suggested_action="reload_stream_and_retry"
)
```

**What it must do next:**

1. **Reload the aggregate:** `app = await LoanApplicationAggregate.load(store, application_id)` — this replays the stream including Agent A's newly appended event.

2. **Re-evaluate its decision:** Agent B's analysis was based on state at version 3. Version 4 may have changed the application's risk_tier, state, or available credit limit. The agent must determine whether its analysis is still relevant given the new state.

3. **Retry or abort:**
   - If the analysis is still valid → call `store.append(..., expected_version=4)`.
   - If Agent A's event invalidates Agent B's conclusion (e.g., A already moved the application to a terminal state) → abort and log the conflict.
   - If max retries exceeded (default: 3 with exponential backoff at 100ms, 200ms, 400ms) → return `ConcurrencyRetryExhausted` error to the caller.

**Why not locks instead of optimistic concurrency?** Pessimistic locks (e.g., `SELECT ... FOR UPDATE` at the application level) would serialize all agent operations on the same application. With 4 agents per application, this would force sequential processing. Optimistic concurrency allows parallel reads and only serialises at the point of write — the "optimistic" bet is that conflicts are rare compared to successful appends, which is true at typical load levels.

---

## 4. Projection Lag and Its Consequences

**Question:** Your LoanApplication projection is eventually consistent with a typical lag of 200ms. A loan officer queries "available credit limit" immediately after an agent commits a disbursement event. They see the old limit. What does your system do?

**Answer:**

**The scenario in detail:**

```
t=0ms    Agent appends DisbursementApproved to loan-{id} stream (write succeeds)
t=0ms    Loan officer queries ApplicationSummary projection (reads stale data)
t=200ms  ProjectionDaemon processes DisbursementApproved, updates projection
```

The loan officer sees the pre-disbursement credit limit because the projection has not yet processed the event. This is the fundamental tradeoff of eventual consistency in CQRS.

**What the system does — a three-layer approach:**

**Layer 1: Timestamp and freshness metadata in the response.**
Every query response from the `ApplicationSummary` projection includes:

```json
{
  "application_id": "loan-123",
  "available_credit_limit_usd": 500000,
  "_projection_last_event_at": "2026-03-20T14:30:00.100Z",
  "_projection_lag_ms": 180,
  "_freshness_warning": true
}
```

The `_freshness_warning` flag is set when `projection_lag_ms > 100ms`. The UI layer uses this to display a "Data may be updating…" indicator.

**Layer 2: The SLO contract.**
The `ApplicationSummary` projection has a 500ms SLO. If the loan officer waits 500ms and refreshes, the data will be current. This is documented in the API contract. The UI can auto-refresh after the SLO period.

**Layer 3: Direct stream read as a fallback.**
For critical decisions (like approving a disbursement based on available credit), the system provides a "strong-read" path: load the stream directly and compute the current state via aggregate replay. This is expensive (O(n) in stream length) but guarantees read-your-writes consistency. In the MCP resource model, this is the justified exception for `ledger://applications/{id}/audit-trail`, which reads the stream directly.

**Communication to the user interface:**

The response header (or JSON envelope) always includes:

- `X-Projection-Lag-Ms: 180` — the daemon's current lag for this projection
- `X-Data-As-Of: 2026-03-20T14:30:00.100Z` — the timestamp of the last event the projection has processed

The frontend interprets lag > SLO/2 as a visual warning. Lag > SLO triggers an auto-retry. This pattern eliminates the class of bugs where a user makes a decision based on stale data without knowing it's stale.

---

## 5. The Upcasting Scenario

**Question:** `CreditDecisionMade` was defined in 2024 with `{application_id, decision, reason}`. In 2026 it needs `{application_id, decision, reason, model_version, confidence_score, regulatory_basis}`. Write the upcaster. What is your inference strategy?

**Answer:**

```python
@registry.register("CreditDecisionMade", from_version=1)
def upcast_credit_decision_v1_to_v2(payload: dict) -> dict:
    """v1 → v2: Add model_version, confidence_score, regulatory_basis.

    Inference strategy:
    - model_version: "legacy-pre-2025" — All v1 events predate model tracking.
      This is a deterministic inference with zero error rate: every event
      stored before the model_version field existed was produced by the
      legacy model pipeline.
    - confidence_score: None — Genuinely unknown. The 2024 system did not
      compute confidence scores. Fabricating a value (e.g., 0.5 as a
      "default") would be worse than null for two reasons:
        1. Downstream consumers (e.g., the confidence floor rule at 0.6)
           would incorrectly classify all legacy decisions as "REFER",
           retroactively changing historical outcomes.
        2. Regulators examining the audit trail would see fabricated data
           presented as measured data — a compliance violation.
      Null forces every consumer to handle the "unknown" case explicitly.
    - regulatory_basis: Inferred from the active regulation set at the
      event's recorded_at timestamp. The system maintains a regulation
      version history table mapping date ranges to regulation_set_versions.
      For events before 2025-01-01, the applicable regulation is "Basel-III-2024".
      For events between 2025-01-01 and 2025-12-31, "Basel-III-2025-rev1".
      Error rate estimate: <1% — regulation transitions happen on known dates.
    """
    return {
        **payload,
        "model_version": "legacy-pre-2025",
        "confidence_score": None,
        "regulatory_basis": _infer_regulatory_basis(payload),
    }

def _infer_regulatory_basis(payload: dict) -> str:
    """Infer regulatory basis from context available in the v1 payload.

    Since v1 events don't carry a timestamp in payload, we use a
    conservative default. In the full implementation, the upcaster
    receives the StoredEvent's recorded_at via the registry.
    """
    # Default: the regulation active during the v1 event era
    return "Basel-III-2024"
```

**When to choose null vs. inference:**

| Field              | Strategy                      | Justification                                                                |
| ------------------ | ----------------------------- | ---------------------------------------------------------------------------- |
| `model_version`    | Inference ("legacy-pre-2025") | Deterministic — zero ambiguity about which model existed before tracking     |
| `confidence_score` | Null                          | Genuinely unmeasured — any fabrication creates false data in the audit trail |
| `regulatory_basis` | Inference from date           | Low error rate (<1%) — regulation versions are a known function of time      |

**General rule:** Infer when the mapping is deterministic or near-deterministic (error rate < 5%) and the downstream consequence of error is recoverable. Use null when the data was never measured and fabrication would create misleading audit records. In a regulated environment, an honest "unknown" is always preferable to a plausible-looking fabrication.

---

## 6. The Marten Async Daemon Parallel

**Question:** Marten 7.0 introduced distributed projection execution across multiple nodes. Describe how you would achieve the same pattern in Python.

**Answer:**

**Marten's approach:** The Marten Async Daemon uses a "session-level" lock via PostgreSQL advisory locks (`pg_advisory_lock`). A single leader node acquires the lock and distributes projection work to worker nodes via a task queue. If the leader crashes, another node acquires the lock and takes over. Marten 7.0 added the ability to partition projections across nodes — different nodes process different projection "slices" (e.g., Node A handles `ApplicationSummary`, Node B handles `ComplianceAuditView`).

**Python equivalent architecture:**

```
┌─────────────────────────────────────────────────────────────┐
│                   PostgreSQL Advisory Lock                    │
│         pg_advisory_lock(hash('ledger-daemon-leader'))        │
└───────────────┬─────────────────────────────────────────────┘
                │ acquired by exactly one node
                ▼
┌─────────────────────────┐
│    Leader Daemon Node    │
│  ┌───────────────────┐  │
│  │ Projection Router  │  │ ← polls events table from last checkpoint
│  │ Routes events to   │  │
│  │ projection workers  │  │
│  └───────────────────┘  │
└────────┬────────────────┘
         │ distribute via Redis Streams
         ├──────────────────────────────┐
         ▼                              ▼
┌──────────────────┐        ┌──────────────────┐
│  Worker Node A    │        │  Worker Node B    │
│  ApplicationSum   │        │  ComplianceAudit  │
│  AgentPerformance │        │  AuditIntegrity   │
└──────────────────┘        └──────────────────┘
```

**Coordination primitive:** PostgreSQL advisory locks for leader election + Redis Streams for work distribution.

**Implementation outline:**

```python
# Leader election via advisory lock
LEADER_LOCK_ID = 7_247_283_910  # deterministic hash

async def try_become_leader(pool: asyncpg.Pool) -> bool:
    """Non-blocking attempt to acquire the leader advisory lock."""
    async with pool.acquire() as conn:
        acquired = await conn.fetchval(
            "SELECT pg_try_advisory_lock($1)", LEADER_LOCK_ID
        )
        return acquired

async def daemon_loop(pool, projections):
    """Main daemon loop — leader distributes, followers wait."""
    while True:
        if await try_become_leader(pool):
            # I am the leader: poll events, distribute to workers
            await distribute_events(pool, projections)
        else:
            # I am a follower: process work items from Redis Stream
            await process_assigned_projections()
        await asyncio.sleep(0.1)
```

**Failure mode guarded against: Split-brain projection processing.**

Without coordination, two daemon instances could process the same events for the same projection, leading to:

- **Duplicate state updates** (e.g., double-counting an agent's performance metrics)
- **Checkpoint desynchronisation** (Node A advances checkpoint to position 500, Node B is still at 400, then Node B overwrites checkpoint back to 450)

The advisory lock ensures exactly one leader controls event distribution. If the leader crashes, its PostgreSQL connection closes, the advisory lock is automatically released, and another node acquires leadership within one poll interval (~100ms). The maximum data loss window is one batch (100 events) — the new leader replays from the last committed checkpoint.

**What Marten gives you that this implementation must work for:**

- Marten's Async Daemon handles projection rebuild, error tracking, and dead-letter queues out of the box.
- Our Python implementation must build each of these explicitly: retry counts per event per projection, skip-after-N-failures logic, and a rebuild command that truncates projection tables and replays from position 0.
- Marten automatically tracks "high water mark" per projection tenant; we must implement per-projection checkpoints manually (the `projection_checkpoints` table).
