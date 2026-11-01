

from __future__ import annotations

from typing import Any, Protocol


class ConditionWorkflowStarter(Protocol):
    async def start(self, intent_id: str, deadline_iso: str) -> bool:
        """Start the condition wait for this intent. True if started now, False if it
        already existed (starting twice is a harmless no-op)."""
        ...

    async def signal(self, intent_id: str, verified: bool, evidence: dict[str, Any]) -> bool:
        """Deliver the condition result. True if the workflow received it. Only a trusted
        server-side verifier may call this: the result decides whether money is captured."""
        ...


class NoopWorkflowStarter:
    """Does nothing. Used when Temporal is not configured; payments simply stay waiting."""

    async def start(self, intent_id: str, deadline_iso: str) -> bool:
        return False

    async def signal(self, intent_id: str, verified: bool, evidence: dict[str, Any]) -> bool:
        return False