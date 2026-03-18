from .event_store import EventStore
from .models.events import StoredEvent
from typing import Dict, Any
import hashlib
import json

class UpcasterRegistry:
    def __init__(self):
        self._upcasters: Dict[tuple, callable] = {}

    def register(self, event_type: str, from_version: int):
        def decorator(fn):
            self._upcasters[(event_type, from_version)] = fn
            return fn
        return decorator

    def upcast(self, event: StoredEvent) -> StoredEvent:
        current = event
        v = event.event_version
        while (event.event_type, v) in self._upcasters:
            new_payload = self._upcasters[(event.event_type, v)](current.payload)
            current = StoredEvent(
                event_id=current.event_id,
                stream_id=current.stream_id,
                stream_position=current.stream_position,
                global_position=current.global_position,
                event_type=current.event_type,
                event_version=v + 1,
                payload=new_payload,
                metadata=current.metadata,
                recorded_at=current.recorded_at
            )
            v += 1
        return current

registry = UpcasterRegistry()

@registry.register("CreditAnalysisCompleted", 1)
def upcast_credit_v1_to_v2(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        **payload,
        "model_version": "legacy-pre-2026",
        "confidence_score": None,  # genuinely unknown
    }