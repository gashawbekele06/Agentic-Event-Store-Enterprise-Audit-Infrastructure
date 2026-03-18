# DESIGN.md

## 1. Aggregate boundary justification

ComplianceRecord is a separate aggregate from LoanApplication to prevent coupling between loan processing and compliance evaluation. Merging them would require compliance checks and loan decisions to happen in the same transaction, blocking concurrent compliance evaluation while loans are being processed. This leads to contention under load. The boundary allows independent scaling and prevents one domain's failures from affecting the other.

## 2. Projection strategy

ApplicationSummary: Async projection with 500ms SLO. Inline would increase write latency; async allows eventual consistency for read-heavy queries.

AgentPerformanceLedger: Async, 2s SLO. Aggregates metrics over time, eventual consistency acceptable.

ComplianceAuditView: Async with snapshot strategy (manual trigger), 2s SLO. Temporal queries require state reconstruction; snapshots reduce replay cost.

## 3. Concurrency analysis

Under peak load (100 concurrent applications, 4 agents each), expect 10-20 OCC errors per minute on loan streams. Retry strategy: exponential backoff (1s, 2s, 4s), max 3 retries. Beyond that, return failure to caller. This keeps success rate >95% while preventing infinite loops.

## 4. Upcasting inference decisions

For missing model_version: infer from recorded_at timestamp (pre-2025 = "legacy"). Error rate: low, as timestamps are reliable. For confidence_score: null, as fabrication could mislead auditors. Choose null when inference error rate >5% or downstream impact is high.

## 5. EventStoreDB comparison

EventStoreDB uses streams as first-class citizens; my schema maps streams to stream_id, load_all() to $all subscription. EventStoreDB provides built-in projections and subscriptions; my implementation requires custom daemon and checkpointing, trading simplicity for PostgreSQL integration.

## 6. What you would do differently

With another full day, I'd redesign the projection daemon for distributed execution using Redis coordination, eliminating the single-point bottleneck. This would improve scalability but add complexity in failure handling.
