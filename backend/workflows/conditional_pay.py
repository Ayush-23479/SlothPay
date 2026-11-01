from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from backend.workflows.common import (
    ACTIVITY_MARK_EXPIRED,
    ACTIVITY_RECONCILE,
    ACTIVITY_RESOLVE_CONDITION,
    CONDITIONAL_WORKFLOW,
    DEFAULT_MAX_HOURS,
    REASON_CANCELLED,
    REASON_DEADLINE,
    REASON_FAILED,
    REASON_INVALID_DEADLINE,
    REASON_VERIFIED,
    RECONCILE_WORKFLOW,
    SIGNAL_CANCEL,
    SIGNAL_CONDITION_RESULT,
    clamp_wait_seconds,
    parse_deadline,
    reconcile_workflow_id,
)

# At most 5 tries per activity, then the workflow fails visibly instead of looping forever.
ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
    non_retryable_error_types=["PaymentMissing"],
)
ACTIVITY_TIMEOUT = timedelta(seconds=120)


@workflow.defn(name=CONDITIONAL_WORKFLOW)
class ConditionalPayWorkflow:
    def __init__(self) -> None:
        # The FIRST decision wins; later signals are ignored.
        self._decision: dict[str, Any] | None = None

    @workflow.signal(name=SIGNAL_CONDITION_RESULT)
    def condition_result(self, verified: Any, evidence: Any = None) -> None:
        if self._decision is None:
            # Typed Any on purpose: a malformed value must still be recorded as a FAILURE
            # (default deny) instead of being dropped by the type decoder.
            ok = verified is True
            self._decision = {
                "verified": ok,
                "evidence": evidence if isinstance(evidence, dict) else {},
                "reason": REASON_VERIFIED if ok else REASON_FAILED,
            }

    @workflow.signal(name=SIGNAL_CANCEL)
    def cancel(self) -> None:
        if self._decision is None:
            self._decision = {"verified": False, "evidence": {}, "reason": REASON_CANCELLED}

    @workflow.run
    async def run(
        self, intent_id: str, deadline_iso: str, max_hours: int = DEFAULT_MAX_HOURS
    ) -> dict[str, Any]:
        # max_hours is an argument (recorded in the workflow history), never read from settings or
        # a file: the sandbox forbids file access and replays must see the same value.
        deadline = parse_deadline(deadline_iso)
        wait = clamp_wait_seconds(workflow.now(), deadline, max_hours)
        if self._decision is None and wait > 0:
            try:
                await workflow.wait_condition(
                    lambda: self._decision is not None, timeout=timedelta(seconds=wait)
                )
            except TimeoutError:
                pass

        if self._decision is None:
            reason = REASON_INVALID_DEADLINE if deadline is None else REASON_DEADLINE
            await workflow.execute_activity(
                ACTIVITY_MARK_EXPIRED,
                args=[intent_id, reason],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
            )
            self._decision = {"verified": False, "evidence": {}, "reason": reason}

        decision = self._decision
        result: dict[str, Any] = await workflow.execute_activity(
            ACTIVITY_RESOLVE_CONDITION,
            args=[intent_id, decision["verified"], decision["evidence"], decision["reason"]],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )

        reconcile_started = False
        if result.get("needs_reconciliation"):
            # The outcome is unknown: hand it to the reconcile workflow, which owns the checks.
            try:
                await workflow.start_child_workflow(
                    RECONCILE_WORKFLOW,
                    intent_id,
                    id=reconcile_workflow_id(intent_id),
                    parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                )
                reconcile_started = True
            except WorkflowAlreadyStartedError:
                reconcile_started = True  # one is already running: that is what we wanted

        # Safety check once after the capture or void.
        check: dict[str, Any] = await workflow.execute_activity(
            ACTIVITY_RECONCILE,
            args=[intent_id],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )
        return {
            "intent_id": intent_id,
            "outcome": result.get("action", "NONE"),
            "reason": decision["reason"],
            "state": check.get("status") or result.get("state"),
            "needs_reconciliation": bool(result.get("needs_reconciliation")),
            "reconcile_started": reconcile_started,
            "safety_check": check.get("decision"),
        }