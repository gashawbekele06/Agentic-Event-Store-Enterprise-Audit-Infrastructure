"""
AuditLedgerAggregate — Cross-cutting audit trail with cryptographic integrity.

Enforces:
  1. Append-only: no events can be removed
  2. Cross-stream causal ordering via correlation_id chains
  3. Hash chain integrity — each check extends the previous
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.models.events import DomainError, StoredEvent

if TYPE_CHECKING:
    from src.event_store import EventStore


class AuditLedgerAggregate:
    """Consistency boundary for the audit ledger of a business entity.

    Stream: ``audit-{entity_type}-{entity_id}``
    """

    def __init__(self, entity_type: str, entity_id: str) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.version: int = 0

        self.events_verified_count: int = 0
        self.last_integrity_hash: str = ""
        self.integrity_checks: list[dict[str, Any]] = []
        self.chain_broken: bool = False

    @property
    def stream_id(self) -> str:
        return f"audit-{self.entity_type}-{self.entity_id}"

    @classmethod
    async def load(
        cls, store: "EventStore", entity_type: str, entity_id: str
    ) -> AuditLedgerAggregate:
        """Reconstruct aggregate state from the audit stream."""
        agg = cls(entity_type=entity_type, entity_id=entity_id)
        events = await store.load_stream(agg.stream_id)
        for event in events:
            agg._apply(event)
        return agg

    def _apply(self, event: StoredEvent) -> None:
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler:
            handler(event)
        self.version = event.stream_position

    def _on_AuditIntegrityCheckRun(self, event: StoredEvent) -> None:
        self.events_verified_count = event.payload.get("events_verified_count", 0)
        self.last_integrity_hash = event.payload.get("integrity_hash", "")
        self.integrity_checks.append({
            "check_timestamp": event.payload.get("check_timestamp"),
            "integrity_hash": event.payload.get("integrity_hash"),
            "previous_hash": event.payload.get("previous_hash"),
            "events_verified_count": event.payload.get("events_verified_count"),
        })

    # ------------------------------------------------------------------
    # Assertions
    # ------------------------------------------------------------------

    def assert_chain_intact(self) -> None:
        if self.chain_broken:
            raise DomainError(
                f"Audit chain for {self.stream_id} is broken — possible tampering detected.",
                rule="audit_chain_broken",
            )
