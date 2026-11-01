
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from backend.config.settings import get_settings
from backend.workflows.common import (
    CONDITIONAL_WORKFLOW,
    RECONCILE_WORKFLOW,
    SIGNAL_CANCEL,
    SIGNAL_CONDITION_RESULT,
    SIGNAL_WEBHOOK_CONFIRMED,
    SILENCE_WAIT_SECONDS,
    SILENCE_WORKFLOW,
    TASK_QUEUE,
    clamp_deadline,
    conditional_workflow_id,
    reconcile_workflow_id,
    silence_workflow_id,
)


class TemporalWorkflowStarter:
    """Implements ConditionWorkflowStarter (start, signal) plus cancel and the helpers."""

    def __init__(
        self, client: Client, *, task_queue: str = TASK_QUEUE, max_hours: int | None = None
    ) -> None:
        self._client = client
        self._task_queue = task_queue
        self._max_hours = max_hours

    @classmethod
    async def connect(
        cls, address: str | None = None, namespace: str | None = None
    ) -> TemporalWorkflowStarter:
        settings = get_settings()
        client = await Client.connect(
            address or settings.TEMPORAL_ADDRESS, namespace=namespace or settings.TEMPORAL_NAMESPACE
        )
        return cls(client)

    def _max(self) -> int:
        return (
            self._max_hours if self._max_hours is not None else get_settings().CONDITION_MAX_HOURS
        )

    async def start(self, intent_id: str, deadline_iso: str) -> bool:
        """Start the condition wait. The deadline is clamped to CONDITION_MAX_HOURS."""
        max_hours = self._max()
        deadline = clamp_deadline(datetime.now(UTC), deadline_iso, max_hours)
        try:
            await self._client.start_workflow(
                CONDITIONAL_WORKFLOW,
                args=[intent_id, deadline, max_hours],
                id=conditional_workflow_id(intent_id),
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            return False
        return True

    async def signal(self, intent_id: str, verified: bool, evidence: dict[str, Any]) -> bool:
        return await self._send(
            conditional_workflow_id(intent_id),
            SIGNAL_CONDITION_RESULT,
            [verified is True, evidence if isinstance(evidence, dict) else {}],
        )

    async def cancel(self, intent_id: str) -> bool:
        """Cancel the wait: the held funds are voided."""
        return await self._send(conditional_workflow_id(intent_id), SIGNAL_CANCEL, [])

    async def start_reconcile(self, intent_id: str) -> None:
        """Settle an UNCERTAIN payment. One at a time per payment; a finished one may re-run."""
        await self._client.start_workflow(
            RECONCILE_WORKFLOW,
            intent_id,
            id=reconcile_workflow_id(intent_id),
            task_queue=self._task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )

    async def watch_capture(
        self, intent_id: str, wait_seconds: float = SILENCE_WAIT_SECONDS
    ) -> bool:
        """After a capture: check PayPal ourselves if no webhook confirms it in time."""
        try:
            await self._client.start_workflow(
                SILENCE_WORKFLOW,
                args=[intent_id, wait_seconds],
                id=silence_workflow_id(intent_id),
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            return False
        return True

    async def webhook_confirmed(self, intent_id: str) -> bool:
        return await self._send(silence_workflow_id(intent_id), SIGNAL_WEBHOOK_CONFIRMED, [])

    async def _send(self, workflow_id: str, name: str, args: list[Any]) -> bool:
        handle: WorkflowHandle = self._client.get_workflow_handle(workflow_id)
        try:
            await handle.signal(name, args=args)
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return False  # no such workflow, or it already finished
            raise
        return True