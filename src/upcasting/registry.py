"""
UpcasterRegistry — Automatic version chain application on event load.

Upcasters transform old event payloads into new schemas at read time
without touching the stored data, preserving event immutability.
"""
from __future__ import annotations

from typing import Any, Callable

from src.models.events import StoredEvent


class UpcasterRegistry:
    """Central registry for event version migrations.

    Upcasters are registered as (event_type, from_version) -> transform_fn.
    When an event is loaded, ``upcast()`` applies all registered upcasters
    in version order until the event reaches the latest known version.
    """

    def __init__(self) -> None:
        self._upcasters: dict[tuple[str, int], Callable[[dict[str, Any]], dict[str, Any]]] = {}

    def register(self, event_type: str, from_version: int):
        """Decorator.  Registers *fn* as the upcaster for *event_type* at *from_version*."""
        def decorator(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable:
            self._upcasters[(event_type, from_version)] = fn
            return fn
        return decorator

    def upcast(self, event: StoredEvent) -> StoredEvent:
        """Apply all registered upcasters for this event type, in version order."""
        current = event
        v = event.event_version
        while (event.event_type, v) in self._upcasters:
            new_payload = self._upcasters[(event.event_type, v)](current.payload)
            current = current.with_payload(new_payload, version=v + 1)
            v += 1
        return current


# ---------------------------------------------------------------------------
# Singleton registry — imported by EventStore at load-time
# ---------------------------------------------------------------------------
registry = UpcasterRegistry()


# ---------------------------------------------------------------------------
# Registered upcasters
# ---------------------------------------------------------------------------

@registry.register("CreditAnalysisCompleted", from_version=1)
def _upcast_credit_analysis_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """v1 → v2: Add model_version (inferred) and confidence_score (null).

    Inference strategy:
    - model_version: Set to 'legacy-pre-2026' — all v1 events predate the
      model-version-tracking requirement.  This is a safe inference because
      no v1 events existed after model tracking was introduced.
    - confidence_score: Set to None.  Fabricating a score for historical
      events would give downstream consumers false confidence in data
      that was never measured.  Null forces consumers to handle the absence
      explicitly.
    """
    return {
        **payload,
        "model_version": payload.get("model_version", "legacy-pre-2026"),
        "confidence_score": payload.get("confidence_score"),  # genuinely unknown → None
    }
