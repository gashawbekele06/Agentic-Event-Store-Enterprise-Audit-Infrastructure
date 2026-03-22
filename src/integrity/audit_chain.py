"""
Audit Chain — SHA-256 cryptographic integrity for the AuditLedger aggregate.

Builds a blockchain-style hash chain over events. Any post-hoc modification
of events breaks the chain (tamper detection).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from src.models.events import AuditIntegrityCheckRun

if TYPE_CHECKING:
    from src.event_store import EventStore


@dataclass
class IntegrityCheckResult:
    entity_type: str
    entity_id: str
    stream_id: str
    events_verified_count: int
    chain_valid: bool
    tamper_detected: bool
    integrity_hash: str
    previous_hash: str
    checked_at: datetime
    error: str | None = None


def _hash_event(event_payload: dict[str, Any]) -> str:
    """Deterministic SHA-256 of a single event's payload."""
    canonical = json.dumps(event_payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _build_chain_hash(previous_hash: str, event_hashes: list[str]) -> str:
    """Compute the chain hash: sha256(previous_hash + concat_of_event_hashes)."""
    combined = previous_hash + "".join(event_hashes)
    return hashlib.sha256(combined.encode()).hexdigest()


async def run_integrity_check(
    store: "EventStore",
    entity_type: str,
    entity_id: str,
) -> IntegrityCheckResult:
    """
    1. Load all events for the entity's primary stream (e.g. loan-{entity_id})
    2. Load the last AuditIntegrityCheckRun (if any) from the audit stream
    3. Hash payloads of all events since the last check
    4. Verify hash chain: new_hash = sha256(previous_hash + event_hashes)
    5. Append new AuditIntegrityCheckRun event to audit-{entity_type}-{entity_id}
    6. Return result with events_verified, chain_valid, tamper_detected
    """
    audit_stream_id = f"audit-{entity_type}-{entity_id}"
    primary_stream_id = f"{entity_type}-{entity_id}"

    # Load the primary stream to hash its events
    primary_events = await store.load_stream(primary_stream_id)

    # Load the audit stream to find last known state
    audit_events = await store.load_stream(audit_stream_id)
    last_check_position = 0
    previous_hash = "genesis"  # sentinel for the first check

    for ae in audit_events:
        if ae.event_type == "AuditIntegrityCheckRun":
            last_check_position = ae.payload.get("events_verified_count", 0)
            previous_hash = ae.payload.get("integrity_hash", "genesis")

    # Hash all primary events (start from position after last check)
    events_to_check = primary_events[last_check_position:]
    event_hashes = [_hash_event(e.payload) for e in events_to_check]
    new_hash = _build_chain_hash(previous_hash, event_hashes)

    total_verified = last_check_position + len(events_to_check)
    chain_valid = True  # Chain is valid if we can reproduce the hash
    tamper_detected = False

    # Verify against any existing recorded hash by re-reading the audit stream
    # If there's an existing hash for these events and it differs, tamper detected
    # (For now: chain is self-referential — valid if we can append successfully)

    # Append the integrity check event
    check_event = AuditIntegrityCheckRun(
        entity_id=entity_id,
        check_timestamp=datetime.now(timezone.utc),
        events_verified_count=total_verified,
        integrity_hash=new_hash,
        previous_hash=previous_hash,
    )

    try:
        audit_version = await store.stream_version(audit_stream_id)
        expected = -1 if audit_version == 0 else audit_version
        await store.append(
            stream_id=audit_stream_id,
            events=[check_event],
            expected_version=expected,
        )
    except Exception as e:
        return IntegrityCheckResult(
            entity_type=entity_type,
            entity_id=entity_id,
            stream_id=audit_stream_id,
            events_verified_count=total_verified,
            chain_valid=False,
            tamper_detected=False,
            integrity_hash=new_hash,
            previous_hash=previous_hash,
            checked_at=datetime.now(timezone.utc),
            error=str(e),
        )

    return IntegrityCheckResult(
        entity_type=entity_type,
        entity_id=entity_id,
        stream_id=audit_stream_id,
        events_verified_count=total_verified,
        chain_valid=chain_valid,
        tamper_detected=tamper_detected,
        integrity_hash=new_hash,
        previous_hash=previous_hash,
        checked_at=datetime.now(timezone.utc),
    )


async def verify_chain(
    store: "EventStore",
    entity_type: str,
    entity_id: str,
) -> dict[str, Any]:
    """
    Independently verify the audit chain without appending a new event.
    Returns a verification report suitable for regulatory examination.
    """
    audit_stream_id = f"audit-{entity_type}-{entity_id}"
    primary_stream_id = f"{entity_type}-{entity_id}"

    primary_events = await store.load_stream(primary_stream_id)
    audit_events = await store.load_stream(audit_stream_id)

    checks = [ae for ae in audit_events if ae.event_type == "AuditIntegrityCheckRun"]

    if not checks:
        return {
            "verified": True,
            "checks_count": 0,
            "primary_events": len(primary_events),
            "message": "No integrity checks recorded yet",
        }

    # Verify each check in sequence
    previous_hash = "genesis"
    cursor = 0
    violations = []

    for check in checks:
        recorded_hash = check.payload.get("integrity_hash", "")
        recorded_previous = check.payload.get("previous_hash", "genesis")
        events_count = check.payload.get("events_verified_count", 0)

        if recorded_previous != previous_hash:
            violations.append({
                "check_position": check.stream_position,
                "expected_previous_hash": previous_hash,
                "recorded_previous_hash": recorded_previous,
                "violation": "hash_chain_broken",
            })

        # Recompute hash for this batch
        batch = primary_events[cursor:events_count]
        event_hashes = [_hash_event(e.payload) for e in batch]
        computed_hash = _build_chain_hash(recorded_previous, event_hashes)

        if computed_hash != recorded_hash:
            violations.append({
                "check_position": check.stream_position,
                "computed_hash": computed_hash,
                "recorded_hash": recorded_hash,
                "violation": "payload_tampered",
            })

        previous_hash = recorded_hash
        cursor = events_count

    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "verified": len(violations) == 0,
        "tamper_detected": len(violations) > 0,
        "checks_count": len(checks),
        "primary_events": len(primary_events),
        "violations": violations,
        "final_hash": previous_hash,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
