from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow

from backend.workflows.common import (
    ACTIVITY_RECONCILE,
    RECONCILE_DELAYS,
    RECONCILE_RETRY_DECISIONS,
    RECONCILE_STOP_DECISIONS,
    RECONCILE_WORKFLOW,
    SIGNAL_WEBHOOK_CONFIRMED,
    SILENCE_WAIT_SECONDS,
    SILENCE_WORKFLOW,
)
from backend.workflows.conditional_pay import ACTIVITY_RETRY, ACTIVITY_TIMEOUT


async def _reconcile_once(intent_id: str) -> dict[str, Any]:
    return await workflow.execute_activity(
        ACTIVITY_RECONCILE,
        args=[intent_id],
        start_to_close_timeout=ACTIVITY_TIMEOUT,
        retry_policy=ACTIVITY_RETRY,
    )


@workflow.defn(name=RECONCILE_WORKFLOW)
class ReconcileWorkflow:
    @workflow.run
    async def run(self, intent_id: str) -> dict[str, Any]:
        last: dict[str, Any] = {"decision": "UNKNOWN", "status": None}
        checks = 0
        for delay in RECONCILE_DELAYS:
            await workflow.sleep(timedelta(seconds=delay))
            last = await _reconcile_once(intent_id)
            checks += 1
            decision = last.get("decision")
            if decision in RECONCILE_RETRY_DECISIONS:
                continue
            exhausted = decision == "EXHAUSTED"
            if decision not in RECONCILE_STOP_DECISIONS:
                decision = "AMBIGUOUS"  # never treat an unknown answer as proof
            return {
                "intent_id": intent_id,
                "decision": decision,
                "status": last.get("status"),
                "checks": checks,
                "exhausted": exhausted,
            }
        # Five reads, none proved anything: stop and let a human take over.
        return {
            "intent_id": intent_id,
            "decision": "EXHAUSTED",
            "status": last.get("status"),
            "checks": checks,
            "exhausted": True,
        }


@workflow.defn(name=SILENCE_WORKFLOW)
class SilenceWatcherWorkflow:
    def __init__(self) -> None:
        self._confirmed = False

    @workflow.signal(name=SIGNAL_WEBHOOK_CONFIRMED)
    def webhook_confirmed(self) -> None:
        self._confirmed = True

    @workflow.run
    async def run(
        self, intent_id: str, wait_seconds: float = SILENCE_WAIT_SECONDS
    ) -> dict[str, Any]:
        if not self._confirmed:
            try:
                await workflow.wait_condition(
                    lambda: self._confirmed, timeout=timedelta(seconds=wait_seconds)
                )
            except TimeoutError:
                pass
        if self._confirmed:
            return {"intent_id": intent_id, "webhook_confirmed": True, "reconciled": False}
        check = await _reconcile_once(intent_id)
        return {
            "intent_id": intent_id,
            "webhook_confirmed": False,
            "reconciled": True,
            "decision": check.get("decision"),
            "status": check.get("status"),
        }