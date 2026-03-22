"""
Upcasters — Registered version migration functions for schema evolution.

These transform old event schemas into new ones at read time without
touching stored events (immutability preserved).

Upcasters registered here:
  1. CreditAnalysisCompleted v1 → v2: add model_version + confidence_score
  2. DecisionGenerated v1 → v2: add model_versions dict
"""
from __future__ import annotations

from typing import Any

from src.upcasting.registry import registry


@registry.register("CreditAnalysisCompleted", from_version=1)
def _upcast_credit_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """v1 → v2: Add model_version and confidence_score.

    Inference strategy for historically missing fields:
    - model_version: Set to 'legacy-pre-2026'. All v1 events pre-date model tracking.
      Using a labeled sentinel rather than 'unknown' makes downstream filtering explicit.
    - confidence_score: Set to None. The score was genuinely not computed for v1 events.
      Fabricating a number (e.g., 0.5 as a neutral midpoint) would be worse than null —
      it would give consumers false confidence that the value was measured. Null forces
      explicit null checks and prevents silent incorrect computations downstream.

    Error rate: ~100% of v1 events lack a measured confidence_score.
    Downstream consequence: systems that require confidence_score must handle None.
    Acceptable — the Gas Town pattern means these are all historical events from before
    the confidence floor rule (Rule 4) was introduced.
    """
    return {
        **payload,
        "model_version": payload.get("model_version", "legacy-pre-2026"),
        "confidence_score": payload.get("confidence_score"),  # None is correct — do not fabricate
    }


@registry.register("DecisionGenerated", from_version=1)
def _upcast_decision_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """v1 → v2: Add model_versions dict.

    Inference strategy:
    - model_versions: Ideally reconstructed by loading each contributing session's
      AgentContextLoaded event to get the model version each agent used.
      However, upcasters must be pure functions (no I/O) per the registry contract —
      performing a store lookup here would break the synchronous chain and create
      circular loading dependencies.

      Resolution: Set to {} (empty dict). Consumers that require model provenance
      must handle the empty dict case and use the audit chain to trace contributing
      sessions. For regulatory examination, the contributing_agent_sessions[] field
      remains fully intact and can be used to reconstruct model_versions manually.

    Performance implication of store-lookup variant (documented, not implemented):
      Each upcast would require N additional DB queries (one per contributing session).
      For an event with 3 contributing sessions, this triples the load latency.
      For the ApplicationSummary projection replaying 1,000+ events, this would add
      ~15,000 queries. Not acceptable for production catch-up subscriptions.
    """
    return {
        **payload,
        "model_versions": payload.get("model_versions", {}),  # empty dict = no fabrication
    }
