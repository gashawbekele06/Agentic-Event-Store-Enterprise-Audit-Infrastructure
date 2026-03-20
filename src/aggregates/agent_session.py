"""
AgentSessionAggregate — Tracks all actions taken by a specific AI agent instance.

Enforces the Gas Town pattern: an AgentContextLoaded event MUST be the first
event in every session stream before any decision events can be appended.
Also enforces model version tracking and output→context linkage.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.models.events import DomainError, StoredEvent

if TYPE_CHECKING:
    from src.event_store import EventStore


class AgentSessionAggregate:
    """Consistency boundary for a single AI agent session.

    Stream: ``agent-{agent_id}-{session_id}``
    """

    def __init__(self, agent_id: str, session_id: str) -> None:
        self.agent_id = agent_id
        self.session_id = session_id
        self.version: int = 0
        self.context_loaded: bool = False
        self.model_version: str | None = None
        self.context_source: str | None = None
        self.context_token_count: int = 0
        self.decisions_made: list[str] = []      # event_types of decision events
        self.applications_processed: list[str] = []
        self.completed: bool = False

    @property
    def stream_id(self) -> str:
        return f"agent-{self.agent_id}-{self.session_id}"

    # ------------------------------------------------------------------
    # Load (event replay)
    # ------------------------------------------------------------------

    @classmethod
    async def load(
        cls, store: EventStore, agent_id: str, session_id: str
    ) -> AgentSessionAggregate:
        """Reconstruct aggregate state by replaying the agent session stream."""
        agg = cls(agent_id=agent_id, session_id=session_id)
        events = await store.load_stream(agg.stream_id)
        for event in events:
            agg._apply(event)
        return agg

    # ------------------------------------------------------------------
    # Apply handlers
    # ------------------------------------------------------------------

    def _apply(self, event: StoredEvent) -> None:
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler:
            handler(event)
        self.version = event.stream_position

    def _on_AgentContextLoaded(self, event: StoredEvent) -> None:
        self.context_loaded = True
        self.model_version = event.payload.get("model_version")
        self.context_source = event.payload.get("context_source")
        self.context_token_count = event.payload.get("context_token_count", 0)

    def _on_CreditAnalysisCompleted(self, event: StoredEvent) -> None:
        self.decisions_made.append("CreditAnalysisCompleted")
        app_id = event.payload.get("application_id")
        if app_id and app_id not in self.applications_processed:
            self.applications_processed.append(app_id)

    def _on_FraudScreeningCompleted(self, event: StoredEvent) -> None:
        self.decisions_made.append("FraudScreeningCompleted")
        app_id = event.payload.get("application_id")
        if app_id and app_id not in self.applications_processed:
            self.applications_processed.append(app_id)

    def _on_DecisionGenerated(self, event: StoredEvent) -> None:
        self.decisions_made.append("DecisionGenerated")
        app_id = event.payload.get("application_id")
        if app_id and app_id not in self.applications_processed:
            self.applications_processed.append(app_id)

    # ------------------------------------------------------------------
    # Assertions (Gas Town pattern enforcement)
    # ------------------------------------------------------------------

    def assert_context_loaded(self) -> None:
        """Rule 2 — Gas Town: AgentContextLoaded must precede any decision."""
        if not self.context_loaded:
            raise DomainError(
                f"Agent {self.agent_id} session {self.session_id} has no "
                f"AgentContextLoaded event. Gas Town pattern requires context "
                f"to be loaded before any decision can be made.",
                rule="gas_town_context_required",
            )

    def assert_model_version_current(self, expected: str) -> None:
        """Verify the session is running the expected model version."""
        if self.model_version and self.model_version != expected:
            raise DomainError(
                f"Agent session model version mismatch: session has "
                f"'{self.model_version}', command specifies '{expected}'",
                rule="model_version_mismatch",
            )

    def has_decision_for_application(self, application_id: str) -> bool:
        """Check whether this session has produced a decision event for *application_id*."""
        return application_id in self.applications_processed
