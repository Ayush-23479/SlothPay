from __future__ import annotations

from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from backend.orchestration.conditions import PaymentMissing, PaymentService
from backend.reliability.reconcile import DbReconcileStep
from backend.workflows.common import (
    ACTIVITY_MARK_EXPIRED,
    ACTIVITY_RECONCILE,
    ACTIVITY_RESOLVE_CONDITION,
)


class PaymentActivities:
    def __init__(self, service: PaymentService, reconcile_step: DbReconcileStep) -> None:
        self._service = service
        self._reconcile_step = reconcile_step

    @activity.defn(name=ACTIVITY_RESOLVE_CONDITION)
    async def resolve_condition(
        self, intent_id: str, verified: bool, evidence: dict[str, Any], reason: str
    ) -> dict[str, Any]:
        """verified is True -> capture; anything else -> void."""
        try:
            return await self._service.resolve_condition(
                intent_id, verified is True, evidence, reason
            )
        except PaymentMissing as exc:
            raise ApplicationError(
                f"Payment not found: {intent_id}", type="PaymentMissing", non_retryable=True
            ) from exc

    @activity.defn(name=ACTIVITY_MARK_EXPIRED)
    async def mark_condition_expired(self, intent_id: str, reason: str) -> dict[str, Any]:
        try:
            return await self._service.mark_condition_expired(intent_id, reason)
        except PaymentMissing as exc:
            raise ApplicationError(
                f"Payment not found: {intent_id}", type="PaymentMissing", non_retryable=True
            ) from exc

    @activity.defn(name=ACTIVITY_RECONCILE)
    async def reconcile(self, intent_id: str) -> dict[str, Any]:
        """One Task 8 reconciliation read. The workflow owns the waiting between reads, so the
        0.3 / 1 / 2 / 4 / 8 second schedule survives a restart. A PayPal read failure comes back
        as an UNKNOWN decision and never changes the payment status."""
        try:
            outcome = await self._reconcile_step(intent_id)
        except ValueError as exc:
            raise ApplicationError(str(exc), type="PaymentMissing", non_retryable=True) from exc
        return {"decision": outcome.decision, "status": outcome.status}

    def all(self) -> list[Any]:
        return [self.resolve_condition, self.mark_condition_expired, self.reconcile]