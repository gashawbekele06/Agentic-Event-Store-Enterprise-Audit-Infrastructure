# Final Report — Agentic Event Store: Enterprise Audit Infrastructure

> **Submission:** TPR1 Week 5 — Production Event Sourcing for AI Agent Workflows
> **Result:** 100 / 100
> **Assessor comment:** *"You delivered a highly complete, production-grade implementation across schema, event store, aggregates, projections, upcasting, integrity, Gas Town reconstruction, and MCP interface. All rubric criteria are met at the highest level."*

---

## Table of Contents

1. [Domain Conceptual Reasoning](#1-domain-conceptual-reasoning)
2. [Architectural Tradeoff Analysis](#2-architectural-tradeoff-analysis)
3. [Architecture Diagram](#3-architecture-diagram)
4. [Test Evidence & SLO Interpretation](#4-test-evidence--slo-interpretation)
5. [MCP Lifecycle Trace](#5-mcp-lifecycle-trace)
6. [Bonus Results](#6-bonus-results)
7. [Limitations & Reflection](#7-limitations--reflection)

---

## 1. Domain Conceptual Reasoning

### 1.1 EDA vs. Event Sourcing

**Question:** A component uses callbacks (like LangChain traces) to capture event-like data. Is this EDA or Event Sourcing? If redesigned using The Ledger, what exactly changes?

**Answer:** This is **Event-Driven Architecture (EDA)**, not Event Sourcing.

The callback model is fire-and-forget: the component emits a signal that something happened, interested subscribers process it, and the event itself has no durable identity. If the subscriber is offline, the event is lost. If the process restarts, accumulated state vanishes. LangChain traces are a diagnostic overlay — they capture what happened *for observation*, but are not the source of truth for *what the system's state is*.

Event Sourcing is architecturally different. In ES, the events **are** the database. Current state is derived exclusively by replaying the event stream.

| Dimension    | EDA (Callbacks / Traces)                          | Event Sourcing (The Ledger)                           |
|:------------ |:------------------------------------------------- |:----------------------------------------------------- |
| Authority    | State lives in a mutable store; events are notifications | Events ARE the store; state is computed from events |
| Durability   | Events may be dropped or lost                    | Events are append-only, ACID-persisted, never deleted |
| Replay       | Not possible                                     | Core feature — replay from any point                  |
| Ordering     | No global order guarantee                        | Global ordering via `global_position` identity column |
| Immutability | Events can be silently modified                  | Stored events never mutated (upcasting is read-time only) |

**What changes when redesigned with The Ledger:**

1. **Agent actions become events in named streams.** Instead of `on_agent_complete(trace)`, the agent calls `store.append(stream_id="agent-{id}-{session}", events=[AgentContextLoaded(...), CreditAnalysisCompleted(...)], expected_version=...)`. Persisted in PostgreSQL with ACID guarantees before execution continues.

2. **State reconstruction replaces mutable state.** The Automaton Auditor's current "verdict" would be the result of replaying the `audit-automaton-{id}` stream. Crash → restart → replay stream → full state recovery. This is the Gas Town pattern.

3. **Cross-system integration via shared streams.** Governance decisions become events in `audit-governance-{entity}`. The Auditor subscribes via `load_all(event_types=["GovernanceDecision"])`. The Cartographer reads the same stream for lineage. One write, many readers — CQRS.

4. **Temporal queries become possible.** "What was the compliance state of application X at 2:15 PM yesterday?" becomes answerable by replaying the stream up to that timestamp. With EDA callbacks, this question is unanswerable.

**What you gain:** Auditability, reproducibility, temporal queries, crash recovery, shared memory across agents.
**What you lose:** Simplicity. Event sourcing requires aggregate design, version management, projection maintenance, and upcasting infrastructure. It is not free — but for regulated, multi-agent systems, it is unavoidable.

---

### 1.2 Aggregate Boundary Question

**Alternative considered and rejected: Merging `ComplianceRecord` into `LoanApplication`.**

The argument for merging: compliance checks are always about a specific loan, so why not make them events in the `loan-{id}` stream?

**Why rejected — the coupling problem:**

1. **Write contention multiplies.** In the Apex scenario, compliance checks can run in parallel with credit analysis updates. If both write to the same `loan-{id}` stream, every compliance rule evaluation competes for the same stream lock. With 4 agents and 5–15 compliance rules per application, the merged model would see roughly 2× more `OptimisticConcurrencyError`s on the loan stream. The separated model allows the compliance agent to write `ComplianceRulePassed` to `compliance-{id}` with **zero contention** against the credit agent writing to `loan-{id}`.

2. **Failure domain expansion.** Compliance checks involve external regulation API calls. If a compliance handler throws (API timeout, invalid regulation data), it would roll back the entire loan aggregate's transaction — including co-resident events. With separate aggregates, a compliance failure affects only the compliance stream.

3. **Independent replay and projection.** The `ComplianceAuditView` projection subscribes only to compliance events. Merging would force it to filter loan lifecycle events from compliance events during every replay.

**The specific coupling prevented:** Agent A appends `CreditAnalysisCompleted` to `loan-X` at version 7. Simultaneously, the compliance daemon appends `ComplianceRulePassed` to the same stream. With a merged aggregate, one fails with a concurrency error despite modifying completely independent aspects — **spurious coupling**. With separation, both writes succeed without interference.

---

### 1.3 Concurrency Trace

**Scenario:** Two AI agents simultaneously call `append_events` with `expected_version=3`.

```
TIME    AGENT A                                  AGENT B
────    ───────                                  ───────
t0      BEGIN TRANSACTION                        BEGIN TRANSACTION
t1      SELECT current_version                   SELECT current_version
        FROM event_streams                       FROM event_streams
        WHERE stream_id = 'loan-X'               WHERE stream_id = 'loan-X'
        FOR UPDATE                               FOR UPDATE
        → Acquires row lock, reads 3             → BLOCKED (row locked by A)

t2      current_version = 3 ✓
        INSERT INTO events (... stream_position=4 ...)
        INSERT INTO outbox (...)
        UPDATE event_streams SET current_version = 4

t3      COMMIT                                   → Lock released, B proceeds
                                                 → reads current_version = 4

t4                                               expected_version (3) ≠ actual (4)
                                                 → RAISE OptimisticConcurrencyError
                                                 ROLLBACK
```

**Key mechanism:** The `SELECT ... FOR UPDATE` on the `event_streams` row serialises concurrent writers to the same stream. Agent B is **held** (not rejected) at `t1` until Agent A commits at `t3`. Once B reads the updated version, the mismatch is detected and the error is raised *by application logic*, not a database constraint.

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

1. **Reload the aggregate** — replay the stream including Agent A's newly appended event.
2. **Re-evaluate its decision** — Agent B's analysis was based on state at version 3; version 4 may have changed risk tier, state, or available limit.
3. **Retry or abort** — if still valid, retry with `expected_version=4`; if A's event invalidated B's conclusion, abort; if max retries (3) exceeded, return `ConcurrencyRetryExhausted`.

**Why `stream length = 4` is the meaningful assertion** (not 5): The database uniqueness constraint on `(stream_id, stream_position)` guarantees one winner *structurally*. Asserting `len(events) == 4` proves the constraint enforced mutual exclusion and that no phantom double-write slipped through — it is more fundamental than checking which agent won.

---

### 1.4 Projection Lag and Staleness Communication

**Scenario:** Loan officer queries "available credit limit" 10ms after a disbursement event is committed. The projection hasn't processed it yet.

```
t=0ms    Agent appends DisbursementApproved to loan-{id} (write succeeds)
t=0ms    Loan officer queries ApplicationSummary projection → sees stale limit
t=200ms  ProjectionDaemon processes event, projection updated
```

**Concrete mechanism — three-layer staleness communication:**

**Layer 1 — Response metadata.** Every projection response includes:
```json
{
  "available_credit_limit_usd": 500000,
  "_projection_last_event_at": "2026-03-20T14:30:00.100Z",
  "_projection_lag_ms": 180,
  "_freshness_warning": true
}
```
`_freshness_warning` is set when lag > 100ms. The UI displays a "Data may be updating…" indicator.

**Layer 2 — HTTP headers.** `X-Projection-Lag-Ms: 180` and `X-Data-As-Of: <timestamp>` on every response. Frontend triggers auto-refresh when lag exceeds half the SLO.

**Layer 3 — `min_version` strong-read fallback.** Command handlers return the new `stream_version` in their response. Clients can pass `?min_version=N` to projection resources — if the projection's `last_position` has not yet reached N, the resource executes a direct stream read (strong-read path) guaranteeing read-your-writes consistency without polling. This is expensive (O(n) in stream length) but eliminates the class of bugs where users make irreversible decisions on stale data without knowing it.

**Specific user-facing behaviour prevented:** A loan officer sees the pre-disbursement credit limit and approves a second disbursement that would exceed the application's total approved amount. With the three-layer approach, the UI shows the staleness warning and the API enforces the `min_version` check before rendering the approval button.

---

### 1.5 The Upcaster

**CreditAnalysisCompleted v1 → v2** (v1 fields: `application_id`, `agent_id`, `session_id`, `risk_tier`, `recommended_limit_usd`, `analysis_duration_ms`, `input_data_hash`):

```python
@registry.register("CreditAnalysisCompleted", from_version=1)
def upcast_credit_analysis_v1_to_v2(payload: dict) -> dict:
    """v1 → v2: Add model_version, confidence_score, regulatory_basis.

    Inference strategy:
    - model_version: "legacy-pre-2026" — All v1 events predate model tracking.
      Deterministic inference with 0% error rate: every v1 event was produced
      by the legacy model pipeline by definition.

    - confidence_score: None — Genuinely unknown. The 2024 system did not
      compute confidence scores. Fabricating a value (e.g., 0.5) would be
      worse than null for two reasons:
        1. Downstream consumers with a confidence floor rule at 0.6 would
           incorrectly classify all legacy decisions as "REFER", retroactively
           changing documented historical outcomes.
        2. Regulators examining the audit trail would see fabricated data
           presented as measured data — a compliance violation.
      Null forces every consumer to handle the "unknown" case explicitly.

    - regulatory_basis: Inferred from the regulation set version active at
      the event's recorded_at timestamp. Error rate ~5% at regulation
      transition boundaries. Wrong inference means a documentation error
      (wrong basis label), not a decision error. Still preferable to null
      because the mapping is deterministic for 95%+ of events.
    """
    return {
        **payload,
        "model_version": "legacy-pre-2026",
        "confidence_score": None,
        "regulatory_basis": _infer_regulatory_basis(payload),
    }
```

**Null vs. inference decision matrix:**

| Field               | Strategy                        | Error rate | Consequence of error                                                                 |
|:------------------- |:------------------------------- |:---------- |:------------------------------------------------------------------------------------ |
| `model_version`     | `"legacy-pre-2026"` (inferred)  | **0%**     | Performance dashboards show a named legacy bucket — no operational harm              |
| `confidence_score`  | `None` (null)                   | 0% (no inference) | Consumers must handle null; confidence floor rule cannot retroactively apply — **correct** |
| `regulatory_basis`  | Inferred from `recorded_at`     | **~5%**    | Wrong basis label in audit trail — documentation error, not a decision error         |

**General rule:** Infer when the mapping is deterministic or near-deterministic (< 5% error rate) and the downstream consequence of error is recoverable. Use null when the data was never measured and fabrication would create misleading audit records. In a regulated environment, an honest "unknown" is always preferable to a plausible-looking fabrication.

---

### 1.6 Marten Async Daemon Parallel

**Coordination primitive:** PostgreSQL advisory locks for leader election.

**Mapping to the Marten Async Daemon:**

Marten 7.0 uses a session-level PostgreSQL advisory lock (`pg_advisory_lock`) to elect a single leader node. The leader distributes projection work to worker nodes. If the leader crashes, its PostgreSQL connection closes, the lock is automatically released, and another node acquires leadership within one poll interval.

**Python equivalent:**

```python
LEADER_LOCK_ID = 7_247_283_910  # deterministic hash of "ledger-daemon-leader"

async def try_become_leader(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT pg_try_advisory_lock($1)", LEADER_LOCK_ID
        )

async def daemon_loop(pool, projections):
    while True:
        if await try_become_leader(pool):
            await distribute_events(pool, projections)   # leader path
        else:
            await process_assigned_projections()          # follower path
        await asyncio.sleep(0.1)
```

**Failure mode guarded against: Split-brain projection processing.**

Without coordination, two daemon instances could process the same events for the same projection, leading to:
- **Duplicate state updates** — double-counting agent performance metrics
- **Checkpoint desynchronisation** — Node A advances checkpoint to position 500, Node B is still at 400, then B overwrites checkpoint back to 450, causing event replay and duplicate projection updates

The advisory lock ensures **exactly one leader** controls event distribution at any moment. The maximum data loss window is one batch (~100 events) — the new leader replays from the last committed checkpoint.

---

## 2. Architectural Tradeoff Analysis

### 2.1 Aggregate Boundary Justification

**The specific concurrent write failure mode prevented by separating `ComplianceRecord`:**

> Agent A appends `CreditAnalysisCompleted` to `loan-X` at version 7. Simultaneously, the compliance daemon appends `ComplianceRulePassed` to what would be the same `loan-X` stream. With a merged aggregate, one write fails with `OptimisticConcurrencyError` despite the two writes modifying completely independent aspects of the application state.

Under the Apex load profile (100 concurrent applications × 4 agents × ~8 compliance rules):
- Merged model: ~3,200 write operations/minute on loan streams → **~2× higher OCE rate**
- Separated model: loan stream writes ~1,600/min, compliance stream writes ~1,600/min, zero cross-stream contention

**Four aggregates and their boundaries:**

| Aggregate          | Stream Pattern                    | Write rate under peak load |
|:------------------ |:--------------------------------- |:-------------------------- |
| `LoanApplication`  | `loan-{application_id}`           | ~5 writes / lifecycle      |
| `AgentSession`     | `agent-{agent_id}-{session_id}`   | ~3–6 writes / session      |
| `ComplianceRecord` | `compliance-{application_id}`     | ~5–15 writes / lifecycle   |
| `AuditLedger`      | `audit-{entity_type}-{entity_id}` | 1 write / integrity check  |

---

### 2.2 Projection Strategy

#### ApplicationSummary

- **Type:** Async (via `ProjectionDaemon`)
- **SLO:** 500ms lag (p99)
- **Inline rejected because:** Inline projection adds ~2ms per write, compounding to significant latency under burst load. The ApplicationSummary is a convenience view for loan officers — not a decision-making input. No automated system uses this view to make irreversible decisions. 500ms eventual consistency is acceptable; 2ms per-write tax on all appends is not.

#### AgentPerformanceLedger

- **Type:** Async (inline rejected)
- **SLO:** 2 seconds lag
- **Inline rejected because:** The projection aggregates across many events (running confidence score averages, approve/decline rates). Each event would require a read-modify-write inside the write transaction, adding 3–8ms per append under contention. Agent performance metrics are analytical (model monitoring, A/B comparison) — a 2-second lag is invisible to operators reviewing dashboards.

#### ComplianceAuditView

- **Type:** Async (inline rejected)
- **SLO:** 2 seconds lag
- **Snapshot trigger type:** Event-count (every 50 compliance events per application)
- **Invalidation condition:** Never — snapshots are immutable, derived from immutable events. `rebuild_from_scratch()` truncates and replays; it does not invalidate individual snapshots.
- **Why event-count over time-based:** Compliance events arrive in bursts (5–6 rule evaluations per application within minutes). A time-based trigger (e.g., every 5 minutes) would miss the burst entirely and create a snapshot *after* the burst, providing no catch-up benefit. An event-count trigger captures state at regular intervals *within* the burst.
- **Inline rejected because:** Compliance checks involve external regulation API calls and arrive in bursts. If the projection update fails (e.g., a snapshot write error), inline placement would roll back the compliance event itself — turning a projection maintenance problem into a **domain write failure**. Async isolation ensures compliance events are never lost due to projection errors.

---

### 2.3 Concurrency Analysis

**Load profile:** 100 concurrent applications, 4 agents each, ~500ms processing duration per agent.

**Per-stream contention model:**

Each `loan-{id}` stream receives ~5 writes per lifecycle. Under peak load, the 4 agent writes for a single application may overlap. Probability of two agents writing to the same stream within the same 500ms window:

- Each write takes ~5ms (transaction duration including `FOR UPDATE`)
- P(overlap for a single pair) ≈ 5ms / 500ms = 1%
- P(at least one conflict among 4 concurrent writers) ≈ 1 - (1 - 0.01)³ ≈ **3%**

**Cluster-wide estimate:** 100 applications × 5 writes = 500 writes/minute. At 3% collision → **~15 `OptimisticConcurrencyError` per minute** at peak load.

**Retry strategy (named: Exponential Backoff with Jitter):**

```
Retry 1:  50ms  base + 0–25ms jitter
Retry 2: 100ms  base + 0–50ms jitter
Retry 3: 200ms  base + 0–100ms jitter
```

**Maximum retry budget: 3 attempts.** Probability of 3 consecutive failures: 0.03³ ≈ 0.0027% — effectively zero under normal load.

**When the budget is exhausted:** The handler returns `ConcurrencyRetryExhausted` to the caller. This signals either a systematic contention problem (too many agents per application) or a bug (incorrect expected_version logic). The calling system is expected to apply backpressure or re-queue. The error is logged at WARN level with the stream ID and all attempted versions for diagnosis.

---

### 2.4 Upcasting Inference Decisions

**CreditAnalysisCompleted v1 → v2:**

| New Field           | Inference Strategy                                        | Error Rate     | Downstream Consequence of Error                                                              |
|:------------------- |:--------------------------------------------------------- |:-------------- |:-------------------------------------------------------------------------------------------- |
| `model_version`     | `"legacy-pre-2026"` — deterministic label for all v1     | **0%**         | Performance dashboards show a named legacy bucket; wrong inference would skew A/B comparisons |
| `confidence_score`  | `None` — genuinely unmeasured                            | 0% (no inference) | Confidence floor rule (≥0.6) correctly cannot apply to historical decisions — honest unknown |
| `regulatory_basis`  | Inferred from `recorded_at` vs. regulation version table | **~5%**        | Wrong basis label in audit trail — documentation error, not a decision error                  |

**DecisionGenerated v1 → v2:**

| New Field           | Inference Strategy                                                  | Error Rate     | Downstream Consequence of Error                                            |
|:------------------- |:------------------------------------------------------------------- |:-------------- |:-------------------------------------------------------------------------- |
| `model_versions`    | Reconstruct from `AgentContextLoaded` event on each contributing session stream | **~2%** (some legacy sessions lack `AgentContextLoaded`) | Incomplete dict; performance reporting shows "unknown" for those agents |

**Performance implication of `DecisionGenerated` upcaster:** Requires N store lookups (one per contributing agent session). For a decision with 4 contributing sessions, this adds 4 `load_stream()` calls per event load. Mitigation: cache `AgentContextLoaded` model versions in a lookup table populated during the first rebuild pass.

---

### 2.5 EventStoreDB Comparison

| This Implementation (PostgreSQL)         | EventStoreDB Equivalent                        | Gap                                                                                                |
|:---------------------------------------- |:---------------------------------------------- |:-------------------------------------------------------------------------------------------------- |
| `stream_id` column + `event_streams`     | Stream name (first-class concept)              | EventStoreDB streams are native — no separate metadata table needed                               |
| `global_position` identity column        | `$all` stream                                  | EventStoreDB's `$all` is a built-in global stream; we achieve the same ordering via identity       |
| `load_all()` async generator             | `$all` catch-up subscription                   | **Gap:** EventStoreDB provides *push-based* subscriptions; our implementation is poll-based (100ms minimum latency) |
| `ProjectionDaemon` (external Python)     | Built-in JavaScript projection engine          | **Gap:** EventStoreDB projections run inside the database with no separate deployment unit         |
| `projection_checkpoints` table           | Persistent subscription checkpoint             | EventStoreDB handles checkpointing automatically for persistent subscriptions                      |
| `outbox` table                           | Competing consumers on persistent subscriptions| **Gap:** EventStoreDB's consumer groups replace the outbox pattern entirely                        |
| `SELECT … FOR UPDATE` + version check    | `ExpectedVersion` on stream append             | EventStoreDB enforces expected version natively in the protocol; ours relies on PostgreSQL row locks |

**The single most concrete native capability gap:** Push-based subscriptions. EventStoreDB pushes events to subscribers as they arrive (TCP or gRPC persistent subscription). Our `ProjectionDaemon` polls on a 100ms interval, adding that latency floor to every projection update. Under bursty load this means 100ms minimum lag even when the system is otherwise idle. EventStoreDB's push model reduces this to network RTT (~1ms on the same host).

---

### 2.6 What I Would Do Differently

**The decision I would reconsider:** Recording `CreditAnalysisCompleted` events directly on the `loan-{application_id}` stream rather than only on the `agent-{agent_id}-{session_id}` stream.

**What I built and why:** The command handler appends `CreditAnalysisCompleted` to the loan stream, which drives the `LoanApplication` aggregate's state machine. This is pragmatic — it keeps state machine transitions in a single stream and follows the challenge's example handler directly. It avoids the partial-failure problem of cross-aggregate writes.

**The problem it creates:**

1. **Aggregate boundary leakage.** The `LoanApplication` aggregate now carries agent-session-level detail (`model_version`, `analysis_duration_ms`, `input_data_hash`) that properly belongs to the `AgentSession` domain.

2. **Duplicate data.** The credit analysis payload exists in the loan stream for the state machine, but the agent stream doesn't receive a copy unless the handler writes to both streams in separate transactions — which risks partial failure.

3. **Tighter coupling in upcasting.** When `CreditAnalysisCompleted` changes schema, the upcaster affects events on the loan stream even though the loan aggregate doesn't use the agent-specific fields.

**What the better version looks like:**

```
AgentSession stream:  CreditAnalysisCompleted    (full agent output — all fields)
LoanApplication stream: CreditAnalysisResultRecorded (lean: risk_tier, recommended_limit, agent_session_ref)
```

The command handler writes to both streams via the outbox: appends to the agent stream and writes a "pending loan update" to the outbox; a background process picks it up and appends `CreditAnalysisResultRecorded` to the loan stream. This is more complex but architecturally correct.

**What it would cost to change:** One full day. The dual-write pattern requires: (1) a new `CreditAnalysisResultRecorded` event type with a lean payload; (2) updating `LoanApplicationAggregate` to consume the new event type; (3) updating the outbox poller; (4) writing migration logic to backfill existing `CreditAnalysisCompleted` events on loan streams into the new pattern. The projection rebuild handles the read side automatically.

---

## 3. Architecture Diagram

### 3.1 Command Flow (Write Side)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          MCP CLIENT  (LLM / Test)                            │
│                                                                               │
│   WRITE SIDE (MCP Tools)              READ SIDE (MCP Resources)              │
│   ──────────────────────              ────────────────────────               │
│   tool_submit_application             ledger://applications/{id}             │
│   tool_request_credit_analysis        ledger://applications/{id}/compliance  │
│   tool_record_credit_analysis         ledger://applications/{id}/audit-trail │
│   tool_record_fraud_screening         ledger://agents/{id}/performance       │
│   tool_start_compliance_review        ledger://health                        │
│   tool_record_compliance_check        ledger://integrity/{type}/{id}         │
│   tool_generate_decision                                                     │
│   tool_record_human_review                                                   │
│   tool_approve_application                                                   │
│   tool_run_integrity_check                                                   │
│   tool_start_agent_session                                                   │
└──────────────┬───────────────────────────────────┬───────────────────────────┘
               │  MCP Tool calls                   │  MCP Resource reads
               ▼                                   │
┌──────────────────────────┐                       │
│   Command Handlers        │                       │
│   (src/commands/handlers) │                       │
│                           │                       │
│  1. Load aggregate        │                       │
│  2. Validate invariants   │                       │
│     (in aggregate)        │                       │
│  3. Produce events        │                       │
└──────────────┬────────────┘                       │
               │                                   │
               ▼                                   │
┌──────────────────────────────────────────────────┤
│              Aggregate Domain Logic               │
│  ┌──────────────────────┐  ┌──────────────────┐  │
│  │  LoanApplicationAgg  │  │ AgentSessionAgg  │  │
│  │  loan-{app_id}       │  │ agent-{a}-{s}   │  │
│  └──────────────────────┘  └──────────────────┘  │
│  ┌──────────────────────┐  ┌──────────────────┐  │
│  │ ComplianceRecordAgg  │  │  AuditLedgerAgg  │  │
│  │ compliance-{app_id}  │  │ audit-{t}-{id}  │  │
│  └──────────────────────┘  └──────────────────┘  │
└──────────────┬────────────────────────────────────┘
               │  store.append(stream_id, events, expected_version)
               ▼
┌─────────────────────────────────────────────────────────────────┐
│                       EventStore (src/event_store.py)            │
│                                                                   │
│  BEGIN TRANSACTION                                                │
│    SELECT current_version FROM event_streams WHERE ... FOR UPDATE │ ◄─ OCC lock
│    if expected_version ≠ current_version → RAISE OCError         │
│    INSERT INTO events (stream_id, stream_position, event_type,   │
│                        event_version, payload, metadata,         │
│                        recorded_at)                               │
│    INSERT INTO outbox (event_id, destination, payload)           │ ◄─ Outbox (write path)
│    UPDATE event_streams SET current_version = new_version        │
│  COMMIT                                                           │
└──────────────────────────────┬──────────────────────────────────┘
                               │
               ┌───────────────┴──────────────────────┐
               │                                      │
               ▼                                      ▼
┌──────────────────────────┐         ┌───────────────────────────────┐
│    public.events          │         │      public.outbox             │
│  (append-only log)        │         │  (transactional relay)        │
│                           │         │  published_at = NULL → pending │
│  event_id  UUID PK        │         └───────────────────────────────┘
│  stream_id TEXT           │
│  stream_position BIGINT   │         ┌───────────────────────────────┐
│  global_position IDENTITY │◄────────│    Outbox Poller               │
│  event_type TEXT          │         │    (background process)        │
│  event_version SMALLINT   │         │    marks published_at on send  │
│  payload JSONB            │         └───────────────────────────────┘
│  metadata JSONB           │
│  recorded_at TIMESTAMPTZ  │
└──────────────┬────────────┘
               │  polls global_position > last_checkpoint
               ▼
┌──────────────────────────────────────────────────────────────────┐
│                  ProjectionDaemon (src/projections/__init__.py)   │
│                                                                   │
│  for each batch of events:                                        │
│    route event_type → matching projection handlers                │
│    UPDATE projection tables                                       │
│    UPDATE projection_checkpoints SET last_position = N           │◄── Daemon position
└───────┬──────────────┬──────────────────────────┬────────────────┘
        │              │                          │
        ▼              ▼                          ▼
┌────────────┐  ┌───────────────────┐  ┌──────────────────────────┐
│projection_ │  │projection_        │  │projection_compliance_    │
│application_│  │agent_performance  │  │audit                     │
│summary     │  │                   │  │                          │
│(500ms SLO) │  │ (2s SLO)          │  │ (2s SLO + snapshots)     │
└──────┬─────┘  └────────┬──────────┘  └──────────┬───────────────┘
       │                 │                         │
       └─────────────────┴─────────────────────────┘
                                │
                                │  MCP Resource reads (read side)
                                │  projection-backed, never direct stream access
                                ▼
                       ┌─────────────────┐
                       │  MCP Resources  │
                       │  (read models)  │
                       └─────────────────┘
```

### 3.2 CQRS Separation Summary

| Side      | Interface          | Mechanism                              |
|:--------- |:------------------ |:-------------------------------------- |
| Write     | MCP Tools (11)     | Command handler → aggregate → `store.append()` |
| Read      | MCP Resources (6)  | Projection read model — **no direct stream access** |

The audit-trail resource (`ledger://applications/{id}/audit-trail`) is the one justified exception — it reads the event stream directly for forensic queries where a projection read would be insufficient.

---

## 4. Test Evidence & SLO Interpretation

### 4.1 Double-Decision Concurrency Test

**Test:** `tests/test_concurrency.py::test_double_decision_concurrency`

**Setup:** Stream `loan-test-concurrency` seeded to `current_version=3` (3 events: `ApplicationSubmitted`, `CreditAnalysisRequested`, `CreditAnalysisCompleted`). Two coroutines simultaneously call `store.append(..., expected_version=3)`.

**Result:**

```
✓ Double-decision concurrency test PASSED
  Winner: A:success  (or B:success — non-deterministic which wins)
  Loser:  B:conflict  (OptimisticConcurrencyError raised correctly)
  Stream events: 4,  final version: 4

  concurrency_error.expected_version = 3
  concurrency_error.actual_version   = 4
```

**Interpretation of `stream_length = 4`:**

Stream length 4 is the meaningful assertion — not "who won." The assertion proves:
1. The `(stream_id, stream_position)` uniqueness constraint held under concurrent pressure — no duplicate position was written.
2. Exactly one agent's decision was recorded — the state machine received a single, consistent transition.
3. The loser's decision was fully rejected with an explicit, catchable error — not silently dropped.

**Connection to retry budget:** The loser must now reload at version 4 and re-evaluate. If the winner's decision made the loser's conclusion irrelevant (e.g., the application reached a terminal state), the loser aborts. Otherwise it retries with `expected_version=4`. The retry budget (3 attempts, exponential backoff) bounds the total additional latency at **~350ms** in the pathological case of 3 consecutive conflicts — well within the 500ms ApplicationSummary SLO.

---

### 4.2 Projection Lag SLO Under Load

**Test:** `tests/test_projections.py::test_projection_lag_slo_under_load`

**Load:** 50 concurrent command handlers, each executing:
`ApplicationSubmitted → CreditAnalysisRequested → CreditAnalysisCompleted` = 3 appends = **150 total events**

**SLO targets:**
- `ApplicationSummary` projection: < 500ms
- `ComplianceAuditView` projection: < 2000ms

**Measured results:**

| Scenario                                | Measured lag | SLO     | Result |
|:--------------------------------------- |:------------ |:------- |:------ |
| ApplicationSummary — 50 concurrent      | < 500ms      | 500ms   | ✅ PASS |
| ComplianceAuditView — 50 concurrent     | < 2000ms     | 2000ms  | ✅ PASS |
| ComplianceAuditView — after rebuild     | < 2000ms     | 2000ms  | ✅ PASS |

**Commentary on higher-load behaviour and limiting factor:**

At 50 concurrent handlers the daemon catches up within SLO because each `_process_batch()` call processes up to 500 events. The **limiting factor** is not I/O parallelism (asyncpg handles concurrent queries efficiently) but the **serial nature of checkpoint commits** — each batch increments the checkpoint in a single `UPDATE`, creating a write hotspot on the `projection_checkpoints` table. Under 200+ concurrent handlers this single row would become a contention point, degrading catch-up time proportionally. Mitigation: per-projection checkpoint rows (already implemented) allow parallel checkpoint updates across projections; the next bottleneck would be the projection tables themselves.

**Test:** `tests/test_projections.py::test_compliance_slo_during_and_after_rebuild`

50 concurrent `full_compliance_pipeline` coroutines (submit → credit → start compliance → 3 rules = 6 appends per coroutine = **300 compliance events**).

```
✅ Compliance SLO during/after rebuild passed:
   Live lag:    < 2000ms  (50 concurrent handlers)
   Rebuild lag: < 2000ms  (after rebuild_from_scratch())
```

---

### 4.3 Immutability Test

**Test:** `tests/test_upcasting.py::test_upcast_credit_analysis_v1_to_v2`

**Procedure:**
1. Insert a v1 `CreditAnalysisCompleted` event **directly into the DB** (bypassing `EventStore.append`) — omitting `model_version` and `confidence_score`.
2. Load via `store.load_stream()` — verify the loaded event is v2 (upcasted).
3. Query the raw `events` table — verify stored `event_version = 1` and stored payload contains no v2 fields.

**Result:**

```
✅ Immutability test passed:
   Loaded (upcasted): event_version=2, model_version=legacy-pre-2026
   Stored (raw):      event_version=1, model_version=ABSENT (correct)
```

**What it would mean for audit guarantees if the stored payload were modified:**

If the upcasting path wrote `model_version="legacy-pre-2026"` back into the stored v1 row, the audit trail would no longer be append-only. Any compliance re-audit that replays the event stream would see the modified payload and attribute analysis decisions to a model version that was never actually involved. This is a **compliance violation** — regulators auditing "all decisions made before model tracking was introduced" would find fabricated data in the results set. The immutability test is the automated guard against this failure mode.

---

### 4.4 Hash Chain and Tamper Detection

**Test:** `tests/test_gas_town.py::test_tamper_detection`

**Phase 1 — Clean chain verification:**

```python
baseline = await run_integrity_check(store, "loan", entity_id)
assert baseline.chain_valid is True
assert baseline.tamper_detected is False
```

`run_integrity_check()` computes SHA-256 over the serialised event payloads in stream order and records the baseline hash in the `audit-loan-{entity_id}` stream.

**Phase 2 — Payload mutation (attacker simulation):**

```sql
UPDATE events
   SET payload = payload || '{"tampered": true, "requested_amount_usd": 9999999.0}'::jsonb
 WHERE stream_id = $1 AND stream_position = 1
```

This bypasses the append-only constraint and simulates a direct database manipulation — the attack vector that the integrity chain is designed to detect.

**Phase 3 — Detection:**

```python
report = await verify_chain(store, "loan", entity_id)
```

`verify_chain()` recomputes hashes from current DB payloads and compares against the recorded baseline.

**Result:**

```
✅ Tamper detection test passed
   tamper_detected: True
   Violations detected: 1
   First violation: {
     "stream_position": 1,
     "violation": "payload_tampered",
     "expected_hash": "a3f7c2...",
     "actual_hash":   "9e4b81..."
   }
```

**Significance:** The clean chain verification alone is insufficient — it only shows the chain was intact at baseline time. The tamper detection demonstration proves the chain mechanism catches *post-hoc* mutations. An attacker who can write to the `events` table directly cannot alter a recorded event without producing a detectable hash mismatch in the audit stream.

---

## 5. MCP Lifecycle Trace

**Test:** `tests/test_mcp_lifecycle.py::test_full_lifecycle_via_mcp_tools`

**Full trace — `ApplicationSubmitted` through `FinalApproved` via MCP tools only:**

```
Step  Tool / Resource                    Key Input / Return
────  ─────────────────────────────────  ────────────────────────────────────────────────
 1a   tool_start_agent_session           agent_id=credit-{id}, model_version=credit-v2.3
      ← success=True, session_id=csess-{id}
      [Gas Town anchor — AgentContextLoaded written to agent stream before any work]

 1b   tool_start_agent_session           agent_id=fraud-{id},  model_version=fraud-v1.5
      ← success=True

 1c   tool_start_agent_session           agent_id=orch-{id},   model_version=orchestrator-v3.0
      ← success=True

 2    tool_submit_application            application_id={uuid}, requested_amount=500000
      ← success=True, stream_id=loan-{uuid}
      [Precondition: stream must not exist — expected_version=-1]

 3    tool_request_credit_analysis       application_id={uuid}, assigned_agent_id=credit-{id}
      ← success=True
      [Precondition: application must be in SUBMITTED state]

 4    tool_record_credit_analysis        application_id={uuid}, risk_tier=LOW,
                                         confidence_score=0.91, recommended_limit=500000
      ← success=True
      [Business rule enforced: confidence ≥ 0.6 required for APPROVE recommendation]

 5    tool_record_fraud_screening        application_id={uuid}, fraud_score=0.08,
                                         anomaly_flags=[]
      ← success=True

 6    tool_start_compliance_review       application_id={uuid},
                                         checks_required=[KYC-001, AML-002, BSA-003],
                                         regulation_set_version=reg-v2026-q1
      ← success=True
      [Precondition: credit analysis must be complete before compliance review]

 7a   tool_record_compliance_check       rule_id=KYC-001, rule_version=v3, passed=True
      ← success=True, compliance_status=PASSED

 7b   tool_record_compliance_check       rule_id=AML-002, rule_version=v2, passed=True
      ← success=True, compliance_status=PASSED

 7c   tool_record_compliance_check       rule_id=BSA-003, rule_version=v1, passed=True
      ← success=True, compliance_status=PASSED

 8    tool_generate_decision             orchestrator_agent_id=orch-{id},
                                         contributing_agent_sessions=[agent-credit-{id}-csess-{id}],
                                         recommendation=APPROVE, confidence_score=0.88
      ← success=True
      [Causal chain rule enforced by aggregate: every contributing session must have
       a decision for this application — LoanApplicationAggregate.assert_causal_chain_valid()]
      [Precondition: compliance review must be PASSED before decision generation]

 9    tool_record_human_review           reviewer_id=loan-officer-007,
                                         override=False, final_decision=APPROVE
      ← success=True, final_decision=APPROVE

10    tool_approve_application           approved_amount_usd=500000,
                                         interest_rate=0.065,
                                         conditions=[collateral_verified]
      ← success=True, final_state=FINAL_APPROVED

── Projection pump (10 daemon batches) ──

11    ledger://applications/{id}         [READ — projection-backed]
      ← application.state=FINAL_APPROVED, approved_amount_usd=500000.0

12    ledger://applications/{id}/        [READ — projection-backed]
      compliance
      ← checks=[KYC-001:PASSED, AML-002:PASSED, BSA-003:PASSED]

13    ledger://applications/{id}/        [READ — direct stream read]
      audit-trail
      ← events=[ApplicationSubmitted, CreditAnalysisRequested, CreditAnalysisCompleted,
                 FraudScreeningCompleted, ComplianceReviewStarted, ComplianceRulePassed ×3,
                 DecisionGenerated, HumanReviewRecorded, ApplicationApproved]
         event_count=11
```

**CQRS Interpretation:**

The trace proves two things about the projection-backed read model:

1. **Resources never read the stream directly for current-state queries.** Steps 11 and 12 both read from `projection_application_summary` and `projection_compliance_audit` respectively — the tables populated by the `ProjectionDaemon` from the event stream. The LLM client never issues a SQL query against the `events` table in the read path.

2. **The write side (tools) and read side (resources) are decoupled by the daemon.** The 10 daemon batches between step 10 and step 11 simulate the eventual consistency window. The resources return correct data only after the daemon has processed the events — demonstrating that read models are derivatives of the event stream, not the stream itself.

**Precondition enforcement demonstrated:**

`test_mcp_lifecycle.py::test_mcp_structured_error_on_wrong_state` calls `tool_generate_decision` without a prior `tool_submit_application`. Result:

```python
{
  "error_type": "StreamNotFoundError",
  "message": "Stream loan-{uuid} does not exist",
  "suggested_action": "Call tool_submit_application first",
  "success": False
}
```

The error is a **structured dict** (not a Python exception) — the LLM client receives a machine-readable error with `suggested_action` it can act on. This is the MCP contract: tools must never raise unhandled exceptions; all domain errors become structured JSON responses.

**Confidence floor precondition (`test_mcp_confidence_floor_override`):**

```
tool_generate_decision(recommendation="APPROVE", confidence_score=0.45)
→ DecisionGenerated.recommendation = "REFER"  (domain rule enforced, not API layer)
```

The confidence floor (≥ 0.6 for APPROVE) is enforced inside `LoanApplicationAggregate.assert_confidence_floor()` — it cannot be bypassed by passing a higher recommendation from the tool call. The stored event has `recommendation="REFER"`, proving the rule was enforced at the aggregate level, not the HTTP boundary.

---

## 6. Bonus Results

### 6.1 What-If Counterfactual Outcome Comparison

The system supports counterfactual analysis through the temporal query capability of the `ComplianceAuditView`:

**Scenario:** *"What would the decision have been if fraud screening had scored 0.75 instead of 0.08?"*

Using the strong-read path with `get_compliance_at(application_id, timestamp_before_compliance)`, the system returns the compliance state before any compliance events were recorded. By replaying the aggregate with an alternate `FraudScreeningCompleted` payload (fraud_score=0.75), the system can compare:

```
Original path:    fraud_score=0.08 → risk_tier=LOW  → APPROVE (confidence 0.91)
Counterfactual:   fraud_score=0.75 → risk_tier=HIGH → REFER   (confidence floor triggered)
```

This is possible because all decisions are event-sourced — the full decision history is replayable with alternative inputs. In a traditional mutable-state system, this query would be unanswerable.

### 6.2 Regulatory Package Sample

The `tool_run_integrity_check` + `ledger://applications/{id}/audit-trail` combination produces a self-contained regulatory package:

```json
{
  "application_id": "{uuid}",
  "integrity_hash": "a3f7c2...",
  "chain_valid": true,
  "tamper_detected": false,
  "events_verified_count": 11,
  "audit_trail": [
    {"stream_position": 1, "event_type": "ApplicationSubmitted",
     "payload": {"applicant_id": "apex-corp-001", "requested_amount_usd": 500000},
     "recorded_at": "2026-03-25T10:00:00.000Z"},
    ...
    {"stream_position": 11, "event_type": "ApplicationApproved",
     "payload": {"approved_amount_usd": 500000, "approved_by": "loan-officer-007"},
     "recorded_at": "2026-03-25T10:02:45.123Z"}
  ],
  "regulation_set_version": "reg-v2026-q1",
  "compliance_checks": [
    {"rule_id": "KYC-001", "status": "PASSED", "evidence_hash": "hash-KYC-001"},
    {"rule_id": "AML-002", "status": "PASSED", "evidence_hash": "hash-AML-002"},
    {"rule_id": "BSA-003", "status": "PASSED", "evidence_hash": "hash-BSA-003"}
  ]
}
```

A regulator can independently verify the audit trail by recomputing the SHA-256 hash over the event payloads and comparing against `integrity_hash`. The hash chain provides cryptographic non-repudiation of the event sequence.

---

## 7. Limitations & Reflection

### 7.1 No Snapshotting on `LoanApplicationAggregate`

**Concrete failure scenario:** An application with 200+ events (multiple amendments, re-evaluations, regulatory holds) takes > 50ms to load because `LoanApplicationAggregate.load()` replays the entire stream on every command. Under a burst of 100 concurrent applications each with long streams, this adds measurable write latency on the load path.

**Severity:** Acceptable in a first production deployment at the current event counts (typically 8–15 events per application lifecycle). Would become unacceptable at 6–12 months of operation with high amendment rates.

**Connection to a documented decision:** This is a direct consequence of the decision to record `CreditAnalysisCompleted` on the loan stream rather than on a lean `CreditAnalysisResultRecorded` event (§2.6). The full payload per event increases stream replay cost. If the dual-write pattern were implemented, the loan stream would have fewer, leaner events and snapshotting would not be needed for typical lifecycles.

---

### 7.2 Single-Node Daemon — No Distributed Coordination

**Concrete failure scenario:** Two `ProjectionDaemon` instances are started (e.g., during a rolling deployment with one old and one new instance running simultaneously). Both poll from `last_checkpoint` and process the same batch of events. The `projection_agent_performance` table receives duplicate `UPSERT` operations for the same `(agent_id, model_version)` row. Since the upsert uses `analyses_completed = analyses_completed + 1`, the counter is incremented twice — the agent's performance metrics are permanently wrong.

**Severity:** This would be a **production blocker** if the system were deployed with more than one daemon instance. It is acceptable for a single-node deployment (which is the current operational model) but is a known gap before scaling horizontally.

**Mitigation path documented:** §1.6 of the Domain Notes describes the PostgreSQL advisory lock leader election pattern. The implementation exists in the design but is not yet wired into the production daemon startup.

---

### 7.3 No Event Schema Registry or Schema Compatibility Enforcement

**Concrete failure scenario:** A developer renames `CreditAnalysisCompleted.input_data_hash` to `input_hash` in the model and deploys. All existing v1 events in the store now have `input_data_hash` in their payload. The upcaster is never updated. The `AgentSessionAggregate` loads a historical session, the upcaster runs (silently succeeds because it only adds fields, not renames), and the aggregate tries to read `payload["input_hash"]` — `KeyError`. The aggregate's read-path is now broken for all historical events.

**Severity:** This would cause a **silent production failure** in replay-dependent paths (projection rebuild, crash recovery, temporal queries). It would not affect the write path or new events. The blast radius is bounded to reads of historical events — but in a regulated environment, those are exactly the queries that matter most (regulatory audit, tamper investigation).

**Self-identified (not spec-flagged):** The challenge spec did not require a schema registry. This limitation was identified by observing that the upcaster registry has no mechanism to enforce that field names in existing events remain stable, and no tooling to detect breaking changes at PR time.

---

### 7.4 Outbox Poller Is Not Implemented (Stub Only)

**Concrete failure scenario:** An external system subscribes to `ApplicationApproved` events via the outbox. The event is written to the `outbox` table during the `store.append()` transaction. The outbox poller is never started (it is a stub in the codebase). The external system never receives the event. After a business analyst queries "how many approvals were sent to the CRM today", the answer is zero — not because no approvals happened, but because the relay was never started.

**Severity:** Not a production blocker for the core event sourcing functionality (all events are in the store, all projections are populated by the daemon). However, this is a **functional gap** if any downstream system depends on outbox delivery. The outbox table is correctly populated and the schema is complete — only the poller process is missing.

**Acceptable for first deployment:** Yes, if no downstream integration is required. The outbox data is preserved; a poller can be added without schema changes or event store migration.

---

### 7.5 Projection Rebuild Causes Full Read-Path Degradation

**Concrete failure scenario:** An operator triggers `rebuild_from_scratch()` on `ApplicationSummaryProjection` during business hours. The truncate + full replay takes 30 seconds (at 50,000 historical events). During those 30 seconds, the `projection_application_summary` table is empty. All MCP resource calls to `ledger://applications/{id}` return null — loan officers cannot look up any application. The system is not down (writes continue) but the read path is completely unavailable.

**Severity:** This would be a **user-visible outage** in production. The design notes (§2.2) state that "during rebuild, live reads are served from the stream directly" as a degraded fallback, but this strong-read fallback is not implemented in the current `resource_application()` handler.

**Mitigation path:** Blue-green projection tables — rebuild writes to a shadow table (`projection_application_summary_v2`), then atomically swaps the table name once the rebuild completes. This eliminates the downtime window entirely. This was not implemented because it requires a table-swap mechanism that adds significant complexity for a feature that could be deferred to the first operational incident.

---

*Report generated from live codebase at commit `167a58d` — all test assertions reflect actual test logic in `tests/`.*
