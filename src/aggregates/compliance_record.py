"""
ComplianceRecordAggregate — Regulatory check tracking for a loan application.

Enforces:
  1. All mandatory checks must be present before clearance can be issued
  2. Each rule references a specific regulation version
  3. Hard blocks prevent any further processing
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from src.models.events import DomainError, StoredEvent

if TYPE_CHECKING:
    from src.event_store import EventStore


class ComplianceRecordAggregate:
    """Consistency boundary for compliance checks on a single application.

    Stream: ``compliance-{application_id}``
    """

    def __init__(self, application_id: str) -> None:
        self.application_id = application_id
        self.version: int = 0

        self.checks_required: list[str] = []
        self.checks_passed: list[str] = []
        self.checks_failed: list[str] = []
        self.hard_blocked: bool = False
        self.clearance_issued: bool = False
        self.regulation_set_version: str = ""
        self.rule_versions: dict[str, str] = {}  # rule_id → version evaluated against

    @classmethod
    async def load(
        cls, store: "EventStore", application_id: str
    ) -> ComplianceRecordAggregate:
        """Reconstruct aggregate state from the compliance stream."""
        agg = cls(application_id=application_id)
        events = await store.load_stream(f"compliance-{application_id}")
        for event in events:
            agg._apply(event)
        return agg

    def _apply(self, event: StoredEvent) -> None:
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler:
            handler(event)
        self.version = event.stream_position

    def _on_ComplianceCheckRequested(self, event: StoredEvent) -> None:
        self.checks_required = event.payload.get("checks_required", [])
        self.regulation_set_version = event.payload.get("regulation_set_version", "")

    def _on_ComplianceRulePassed(self, event: StoredEvent) -> None:
        rule_id = event.payload.get("rule_id", "")
        rule_ver = event.payload.get("rule_version", "")
        if rule_id and rule_id not in self.checks_passed:
            self.checks_passed.append(rule_id)
        if rule_id and rule_ver:
            self.rule_versions[rule_id] = rule_ver

    def _on_ComplianceRuleFailed(self, event: StoredEvent) -> None:
        rule_id = event.payload.get("rule_id", "")
        rule_ver = event.payload.get("rule_version", "")
        remediation = event.payload.get("remediation_required", False)
        if rule_id and rule_id not in self.checks_failed:
            self.checks_failed.append(rule_id)
        if rule_id and rule_ver:
            self.rule_versions[rule_id] = rule_ver
        # Hard block: non-remediable failure
        if not remediation:
            self.hard_blocked = True

    def _on_ComplianceReviewStarted(self, event: StoredEvent) -> None:
        self.checks_required = event.payload.get("checks_required", [])
        self.regulation_set_version = event.payload.get("regulation_set_version", "")

    # ------------------------------------------------------------------
    # Assertions
    # ------------------------------------------------------------------

    def assert_not_hard_blocked(self) -> None:
        if self.hard_blocked:
            raise DomainError(
                f"Compliance record for {self.application_id} has a hard block — "
                f"no further compliance events may be appended.",
                rule="hard_block",
            )

    def assert_all_checks_complete(self) -> None:
        """Assert all required checks have been evaluated (pass or fail)."""
        evaluated = set(self.checks_passed) | set(self.checks_failed)
        missing = set(self.checks_required) - evaluated
        if missing:
            raise DomainError(
                f"Not all compliance checks complete for {self.application_id}: "
                f"missing {missing}",
                rule="mandatory_checks_incomplete",
            )

    def assert_clearance_possible(self) -> None:
        """Assert compliance clearance can be issued: no failures, all required checks passed."""
        self.assert_not_hard_blocked()
        if self.checks_failed:
            raise DomainError(
                f"Cannot issue compliance clearance: failed rules {self.checks_failed}",
                rule="failed_checks_present",
            )
        self.assert_all_checks_complete()

    @property
    def overall_status(self) -> str:
        if self.hard_blocked or self.checks_failed:
            return "BLOCKED"
        evaluated = set(self.checks_passed) | set(self.checks_failed)
        required = set(self.checks_required)
        if required and required <= evaluated:
            return "CLEAR"
        return "PENDING"
