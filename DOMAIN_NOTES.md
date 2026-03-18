# DOMAIN_NOTES.md

## 1. EDA vs. ES distinction

In the scenario where a component uses callbacks (like LangChain traces) to capture event-like data, this is Event-Driven Architecture (EDA). The component fires events and forgets, with no guarantee of delivery or ordering. If redesigned using The Ledger (event sourcing), the architecture changes fundamentally: events become the source of truth, stored immutably in an append-only log. The component would append events to streams (e.g., agent-{id} for agent actions), and other systems would load and replay these streams to reconstruct state. This gains auditability, temporal queries, and the ability to rebuild any read model by replaying events, but loses the fire-and-forget simplicity of EDA.

## 2. The aggregate question

In the Apex scenario, I will build four aggregates: LoanApplication (loan-{id}), AgentSession (agent-{type}-{session_id}), ComplianceRecord (compliance-{id}), AuditLedger (audit-{entity_type}-{entity_id}). An alternative boundary I considered was merging ComplianceRecord into LoanApplication, treating compliance as a sub-entity within the loan lifecycle. I rejected this because it would create a coupling where compliance checks and loan decisions must happen in the same transaction, preventing concurrent compliance evaluation while a loan is being processed. This could lead to contention and reduced throughput under load.

## 3. Concurrency in practice

Two AI agents simultaneously process the same loan application and both call append_events with expected_version=3. The first agent succeeds, appending its event and advancing the stream to version 4. The second agent receives OptimisticConcurrencyError with expected_version=3 but actual_version=4. It must reload the stream, re-evaluate based on the new state, and retry with expected_version=4. If the second agent's analysis is still relevant, it appends; otherwise, it may abort or notify.

## 4. Projection lag and its consequences

The LoanApplication projection is eventually consistent with a typical lag of 200ms. A loan officer queries "available credit limit" immediately after an agent commits a disbursement event. They see the old limit. The system should display the query result with a timestamp and a "data may be stale" warning, or refresh the projection synchronously if within SLO. The user is informed via UI indicators (e.g., "updating..." spinner), and the system logs the lag for monitoring.

## 5. The upcasting scenario

For CreditDecisionMade v1→v2, I would infer model_version from recorded_at: if before 2025-06-01, "legacy-pre-2025"; else "v1.0". Confidence_score would be null, as it's genuinely unknown—fabricating would be worse than null, as it could mislead auditors. Regulatory_basis would be inferred from active regulations at recorded_at date, sourced from a regulation history table.

## 6. The Marten Async Daemon parallel

In Python, I would use asyncio with a distributed task queue like Celery or Redis-based workers. Each worker polls the event store for new events, processes projections, and updates checkpoints. Coordination uses Redis pub/sub for event notifications and distributed locks for checkpoint consistency. This guards against single-point-of-failure by allowing multiple nodes to process in parallel, but introduces complexity in handling out-of-order events and ensuring idempotent projection updates.
