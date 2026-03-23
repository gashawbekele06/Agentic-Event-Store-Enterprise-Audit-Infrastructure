"""
What-If Projector — Counterfactual scenario analysis for regulatory examination.

Enables the Apex compliance team to ask: "What would the decision have been if
we had used a different risk model or different input data?"

Architecture:
1. Load real events up to the branch point (inclusive of all prior events).
2. Inject counterfactual events in place of the branched event type.
3. Continue replaying real events that are causally INDEPENDENT of the branch.
   An event is causally dependent if its causation_id traces back to an event
   at or after the branch point.
4. Apply both the real and counterfactual event sequences to the supplied
   projections (in-memory, never touching the real store).
5. Return the divergence between real and counterfactual outcomes.

NEVER writes counterfactual events to the real store.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.models.events import BaseEvent, StoredEvent

if TYPE_CHECKING:
    from src.event_store import EventStore
    from src.projections import Projection


@dataclass
class WhatIfResult:
    """Result of a counterfactual scenario analysis."""

    application_id: str
    branch_at_event_type: str
    real_outcome: dict[str, Any]       # projection state under real history
    counterfactual_outcome: dict[str, Any]  # projection state under counterfactual
    divergence_events: list[str] = field(default_factory=list)
    # event_types that differ between real and counterfactual sequences
    branch_position: int = 0           # global_position where the branch occurred
    events_before_branch: int = 0
    events_replayed_real: int = 0
    events_replayed_counterfactual: int = 0


class _InMemoryProjectionRunner:
    """Runs a list of projections against a sequence of StoredEvents in memory.

    Each run() call is independent — projections are NOT shared between real
    and counterfactual replays.
    """

    def __init__(self, projections: list["Projection"]) -> None:
        self._projections = projections

    async def run(self, events: list[StoredEvent]) -> None:
        for event in events:
            for proj in self._projections:
                try:
                    await proj.apply_event(event)
                except Exception:
                    pass  # best-effort in counterfactual replay


def _is_causally_dependent(
    event: StoredEvent,
    branched_event_ids: set[str],
) -> bool:
    """Return True if the event's causation_id traces back to a branched event."""
    causation = event.metadata.get("causation_id")
    if causation and causation in branched_event_ids:
        return True
    return False


def _make_counterfactual_stored_event(
    base_event: BaseEvent,
    template: StoredEvent,
    offset: int,
) -> StoredEvent:
    """Wrap a counterfactual BaseEvent in a StoredEvent shell using the template's
    stream/position metadata (with a simulated position offset so global_position
    ordering is preserved without touching the DB)."""
    import uuid as _uuid
    payload = base_event.model_dump(
        exclude={"event_type", "event_version"}, mode="json"
    )
    return StoredEvent(
        event_id=_uuid.uuid4(),
        stream_id=template.stream_id,
        stream_position=template.stream_position,
        global_position=template.global_position + offset,
        event_type=base_event.event_type,
        event_version=base_event.event_version,
        payload=payload,
        metadata={**template.metadata, "counterfactual": True},
        recorded_at=template.recorded_at,
    )


async def run_what_if(
    store: "EventStore",
    application_id: str,
    branch_at_event_type: str,
    counterfactual_events: list[BaseEvent],
    projections: list["Projection"],
    store_ref: "EventStore | None" = None,
) -> WhatIfResult:
    """
    Run a counterfactual scenario for *application_id*.

    Parameters
    ----------
    store:
        The real EventStore (read-only in this function).
    application_id:
        The loan application to analyse.
    branch_at_event_type:
        The event type at which the branch occurs (e.g. "CreditAnalysisCompleted").
        The first occurrence of this event type in the stream is the branch point.
    counterfactual_events:
        Events to inject INSTEAD of the real branch event.
    projections:
        Projections to evaluate under both real and counterfactual histories.
        Each projection will be run twice (real vs counterfactual) against
        an in-memory copy — the store is never written to.

    Returns
    -------
    WhatIfResult
        Contains real_outcome and counterfactual_outcome dicts, each being the
        serialised state of every supplied projection after replay.
    """
    stream_id = f"loan-{application_id}"
    all_events = await store.load_stream(stream_id)

    # ------------------------------------------------------------------ #
    # 1. Split events at the branch point                                  #
    # ------------------------------------------------------------------ #
    pre_branch: list[StoredEvent] = []
    branch_event: StoredEvent | None = None
    post_branch: list[StoredEvent] = []

    for event in all_events:
        if branch_event is None:
            if event.event_type == branch_at_event_type:
                branch_event = event
            else:
                pre_branch.append(event)
        else:
            post_branch.append(event)

    if branch_event is None:
        # Branch event not found — real and counterfactual are identical
        return WhatIfResult(
            application_id=application_id,
            branch_at_event_type=branch_at_event_type,
            real_outcome={"note": f"No {branch_at_event_type} event found in stream."},
            counterfactual_outcome={"note": "Same as real — branch event not found."},
        )

    # ------------------------------------------------------------------ #
    # 2. Identify causally dependent post-branch events                    #
    # ------------------------------------------------------------------ #
    # An event is causally dependent if its causation_id points to the
    # branch event or any event that is itself causally dependent.
    branched_ids: set[str] = {str(branch_event.event_id)}
    independent_post: list[StoredEvent] = []
    dependent_post: list[StoredEvent] = []
    divergence_event_types: list[str] = []

    for event in post_branch:
        if _is_causally_dependent(event, branched_ids):
            dependent_post.append(event)
            branched_ids.add(str(event.event_id))
            divergence_event_types.append(event.event_type)
        else:
            independent_post.append(event)

    # ------------------------------------------------------------------ #
    # 3. Build real and counterfactual event sequences                     #
    # ------------------------------------------------------------------ #
    real_sequence = pre_branch + [branch_event] + post_branch
    cf_stored = [
        _make_counterfactual_stored_event(cf_evt, branch_event, i)
        for i, cf_evt in enumerate(counterfactual_events)
    ]
    counterfactual_sequence = pre_branch + cf_stored + independent_post

    # ------------------------------------------------------------------ #
    # 4. Run both sequences through deep-copied projection instances       #
    # ------------------------------------------------------------------ #
    import importlib

    async def _run_projections(
        sequence: list[StoredEvent],
        proj_list: list["Projection"],
    ) -> dict[str, Any]:
        runner = _InMemoryProjectionRunner(proj_list)
        await runner.run(sequence)
        # Collect state from each projection
        result: dict[str, Any] = {}
        for proj in proj_list:
            state = getattr(proj, "_state", None) or getattr(proj, "_rows", None)
            # Fall back to __dict__ snapshot for simple projections
            result[proj.name] = state if state is not None else repr(proj)
        return result

    # Deep-copy projections so real and cf runs are independent
    import copy as _copy
    real_projs = [_copy.deepcopy(p) for p in projections]
    cf_projs = [_copy.deepcopy(p) for p in projections]

    real_outcome = await _run_projections(real_sequence, real_projs)
    cf_outcome = await _run_projections(counterfactual_sequence, cf_projs)

    return WhatIfResult(
        application_id=application_id,
        branch_at_event_type=branch_at_event_type,
        real_outcome=real_outcome,
        counterfactual_outcome=cf_outcome,
        divergence_events=divergence_event_types,
        branch_position=branch_event.global_position,
        events_before_branch=len(pre_branch),
        events_replayed_real=len(real_sequence),
        events_replayed_counterfactual=len(counterfactual_sequence),
    )


class InMemoryApplicationSummary:
    """Lightweight in-memory projection of LoanApplication state for what-if analysis.

    Does NOT require a database connection — state is accumulated in a dict.
    Use this with run_what_if() when you need a concrete outcome comparison.
    """

    STATE_TRANSITIONS = {
        "ApplicationSubmitted": "SUBMITTED",
        "CreditAnalysisRequested": "AWAITING_ANALYSIS",
        "CreditAnalysisCompleted": "ANALYSIS_COMPLETE",
        "FraudScreeningCompleted": "ANALYSIS_COMPLETE",
        "ComplianceReviewStarted": "COMPLIANCE_REVIEW",
        "ComplianceCheckRequested": "COMPLIANCE_REVIEW",
        "DecisionGenerated": "PENDING_DECISION",
        "HumanReviewCompleted": None,  # set from payload
        "ApplicationApproved": "FINAL_APPROVED",
        "ApplicationDeclined": "FINAL_DECLINED",
    }

    def __init__(self) -> None:
        self.name = "in_memory_application_summary"
        self._rows: dict[str, dict[str, Any]] = {}

    async def apply_event(self, event: StoredEvent) -> None:
        app_id = event.payload.get("application_id") or event.stream_id.replace("loan-", "", 1)
        row = self._rows.setdefault(app_id, {
            "application_id": app_id,
            "state": "UNKNOWN",
            "risk_tier": None,
            "fraud_score": None,
            "recommendation": None,
            "decision": None,
            "approved_amount_usd": None,
        })

        et = event.event_type
        p = event.payload

        if et == "ApplicationSubmitted":
            row.update(state="SUBMITTED", applicant_id=p.get("applicant_id"),
                       requested_amount_usd=p.get("requested_amount_usd"))
        elif et == "CreditAnalysisRequested":
            row["state"] = "AWAITING_ANALYSIS"
        elif et == "CreditAnalysisCompleted":
            row.update(state="ANALYSIS_COMPLETE", risk_tier=p.get("risk_tier"),
                       confidence_score=p.get("confidence_score"))
        elif et == "FraudScreeningCompleted":
            row["fraud_score"] = p.get("fraud_score")
        elif et in ("ComplianceReviewStarted", "ComplianceCheckRequested"):
            row["state"] = "COMPLIANCE_REVIEW"
        elif et == "DecisionGenerated":
            row.update(state="PENDING_DECISION", recommendation=p.get("recommendation"),
                       decision_confidence=p.get("confidence_score"))
        elif et == "HumanReviewCompleted":
            final = p.get("final_decision", "")
            row["state"] = "APPROVED_PENDING_HUMAN" if final == "APPROVE" else "DECLINED_PENDING_HUMAN"
            row["decision"] = final
        elif et == "ApplicationApproved":
            row.update(state="FINAL_APPROVED", approved_amount_usd=p.get("approved_amount_usd"),
                       decision="APPROVE")
        elif et == "ApplicationDeclined":
            row.update(state="FINAL_DECLINED", decision="DECLINE")

    def get(self, application_id: str) -> dict[str, Any] | None:
        return self._rows.get(application_id)
