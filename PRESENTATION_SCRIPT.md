# 6-Minute Presentation Script
## The Ledger — Agentic Event Store & Enterprise Audit Infrastructure

**Project:** Apex Financial Services — Multi-Agent Loan Processing Platform
**Date:** March 26, 2026
**Format:** Live dashboard demo at `http://localhost:8000`

---

## Timing Guide

| Section | Duration | Cumulative |
|:--- |:--- |:--- |
| Opening | 0:30 | 0:30 |
| Step 1 — The Week Standard | 1:00 | 1:30 |
| Step 2 — Concurrency Under Pressure | 0:50 | 2:20 |
| Step 3 — Temporal Compliance Query | 0:40 | 3:00 |
| Step 4 — Upcasting & Immutability | 0:50 | 3:50 |
| Step 5 — Gas Town Recovery | 0:50 | 4:40 |
| Step 6 — What-If Counterfactual | 0:40 | 5:20 |
| Close | 0:20 | **5:40** |

> 20 seconds of buffer for any demo loading delay.

---

## OPENING — 0:30

> "My name is [your name]. I'm presenting The Ledger — a production-grade event sourcing system for multi-agent AI workflows, built for Apex Financial Services.
>
> The business problem: AI agents are making loan decisions worth millions of dollars, but we have no tamper-proof record of what happened, who decided what, or why. If a regulator asks 'show me the complete decision history of application X', we need an answer in under 60 seconds — with cryptographic proof it hasn't been altered.
>
> That is what I built. Let me show you."

`[Click: Demo Suite tab]`

---

## STEP 1 — THE WEEK STANDARD
### Minutes 1–2 · Target: 1:00

> "Step one — the core deliverable. I'm going to run the full loan pipeline end to end, and show you the complete decision history of a real application."

`[Click: Run Step 1 — Week Standard]`

> "The system is now submitting an application, assigning agents, running credit analysis, fraud screening, three compliance checks, generating a decision, human review, and final approval — all in one shot."

*(wait for output to appear)*

> "Done. You can see the complete event stream — every agent action at a named stream position. ApplicationSubmitted, CreditAnalysisCompleted by agent credit-model-v2.3, FraudScreeningCompleted with score 0.04, four compliance rules passed under regulation reg-v2026-q1, DecisionGenerated with confidence 0.91, human review approved, ApplicationApproved — twelve events, causal chain intact."

`[Point to the integrity section of the output]`

> "At the bottom — cryptographic integrity verification. SHA-256 hash chain over all twelve events. Chain valid: true. Tamper detected: false. That hash is the cryptographic proof this audit trail has not been altered since it was written."

`[Point to the timing line]`

> "Completed in under 60 seconds. Week standard met."

---

## STEP 2 — CONCURRENCY UNDER PRESSURE
### Minutes 2–3 · Target: 0:50

> "Step two — concurrency. In a real system, multiple AI agents process the same application simultaneously. Without concurrency control, two agents could both write a decision, and you'd have two conflicting outcomes with no way to know which is true."

`[Click: Run Step 2 — Concurrency Under Pressure]`

*(wait for output)*

> "Two agents — Alpha and Beta — both read the stream at version 3 and both try to write at the same moment. Watch what happens."

`[Point to Alpha success line]`

> "Agent Alpha wins. Stream advances to version 4."

`[Point to Beta error line]`

> "Agent Beta receives an OptimisticConcurrencyError — expected version 3, actual version 4. This is not a crash. It is a structured, catchable signal with a suggested action: reload and retry."

`[Point to retry success]`

> "Beta reloads, re-evaluates with the new state, retries — and succeeds at version 5. Both decisions are preserved. No lost updates. No phantom writes. The database constraint made this mathematically impossible to corrupt."

---

## STEP 3 — TEMPORAL COMPLIANCE QUERY
### Minute 3 · Target: 0:40

> "Step three — temporal queries. Regulators don't just ask about current state. They ask: what was the compliance status of this application at 2 PM last Tuesday?"

`[Click: Run Step 3 — Temporal Compliance Query]`

*(wait for output)*

> "I queried the compliance state at two points in time — before any compliance checks ran, and after. Look at the difference."

`[Point to the 'before' result]`

> "At timestamp T1, before compliance events: no checks exist. Status is null."

`[Point to the 'after' result]`

> "At timestamp T2, after all checks: KYC-001 passed, AML-002 passed, BSA-003 passed. Distinct states, proven by event position. This is only possible because every compliance event is stored with a wall-clock timestamp and replayed to reconstruct past state — the audit trail is a time machine."

---

## STEP 4 — UPCASTING & IMMUTABILITY
### Minute 4 · Target: 0:50

> "Step four — schema evolution without rewriting history. The system stores events forever. When a v1 event from 2024 is loaded in 2026, it must arrive with the new fields required by current code — but the database record must be unchanged. This is the immutability guarantee of event sourcing."

`[Click: Run Step 4 — Upcasting & Immutability]`

*(wait for output)*

`[Point to the 'Stored in DB' line]`

> "This is what is stored in PostgreSQL: event version 1. No model_version field. No confidence_score field. Exactly what was written in 2024."

`[Point to the 'Loaded via store' line]`

> "This is what the aggregate receives when it loads the event: version 2. model_version is 'legacy-pre-2026' — inferred deterministically. confidence_score is null — genuinely unknown, and we do not fabricate it."

`[Point to 'DB after load']`

> "And the database after loading: still version 1. Still no model_version. The past cannot be altered. Only the read-time view evolves. If upcasting wrote back to the database, our audit trail would be fabricated data — a compliance violation."

---

## STEP 5 — GAS TOWN AGENT MEMORY RECOVERY
### Minute 5 · Target: 0:50

> "Step five — crash recovery. AI agents run for hours. If a process crashes mid-analysis, how does the agent know what it was doing? Traditional systems lose all in-memory state. The Ledger uses the Gas Town pattern: every agent action is written to the event store before execution. The agent's memory is the event stream."

`[Click: Run Step 5 — Gas Town Recovery]`

*(wait for output)*

> "I started an agent session, appended five events — context loaded, two credit analyses completed, one fraud screening completed, and a credit analysis requested but not completed. Then I deleted all in-memory state, simulating a process kill."

`[Point to reconstructed context output]`

> "reconstruct_agent_context() replays the stream from scratch. Look at what comes back: model version restored, decisions made listed, applications processed listed, last event position is five."

`[Point to pending_work and health status]`

> "And critically — pending_work is non-empty. The agent crashed while analysing application three. That work item is flagged as in-progress with no completion event. Session health: NEEDS_RECONCILIATION. The agent knows exactly where to pick up without any external coordination."

---

## STEP 6 — WHAT-IF COUNTERFACTUAL
### Minute 6 · Target: 0:40

> "Step six — the bonus. What if the credit analyst had submitted HIGH risk instead of MEDIUM? What would have happened to this application?"

`[Click: Run Step 6 — What-If Counterfactual]`

*(wait for output)*

`[Point to the comparison table]`

> "Real history: risk tier MEDIUM, confidence 0.72 — recommendation APPROVE. Counterfactual: risk tier HIGH, confidence 0.45 — the confidence floor rule kicks in. Below 0.6, the system overrides APPROVE to REFER regardless of what the agent requested. The final decision changes from APPROVE to REFER."

`[Point to store unchanged line]`

> "And the store is unchanged — still twelve events, same as before. Counterfactual analysis runs entirely in memory. Nothing was written. The audit trail is clean."

---

## CLOSE — 0:20

> "Six properties demonstrated: complete audit history with cryptographic integrity, optimistic concurrency with no lost updates, temporal compliance queries, immutable schema evolution, crash-safe agent memory, and counterfactual analysis. All on a PostgreSQL foundation with no specialised event store infrastructure.
>
> This is The Ledger. Thank you."

---

## Backup Lines (if demo hangs)

| Step | Recovery line |
|:--- |:--- |
| Step 1 | *"The output is in FINAL_REPORT.md — twelve events, integrity hash verified, timing under 60 seconds."* |
| Step 2 | *"The test in tests/test_concurrency.py proves this — one success, one OptimisticConcurrencyError, stream length 4."* |
| Step 3 | *"Implemented in ComplianceAuditViewProjection.get_compliance_at() — distinct states proven by event position."* |
| Step 4 | *"The immutability test in tests/test_upcasting.py — DB shows v1, load returns v2, DB still shows v1 after load."* |
| Step 5 | *"The crash recovery test in tests/test_gas_town.py — last_event_position=5, pending_work non-empty, NEEDS_RECONCILIATION."* |
| Step 6 | *"APPROVE real, REFER counterfactual, store unchanged — counterfactual runs in memory only."* |
