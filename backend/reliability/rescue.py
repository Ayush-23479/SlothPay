

from __future__ import annotations

import uuid
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.attempt import PaymentAttempt
from backend.models.condition import PaymentCondition
from backend.models.payment import Payment
from backend.models.reconciliation import PaymentReconciliation
from backend.models.repository import add_audit_event
from backend.orchestration.lifecycle import stable_operation_request_id
from backend.orchestration.reconcile import UNRESOLVED
from backend.orchestration.recovery import (
    RescueActionNotAllowed,
)
from backend.orchestration.recovery import (
    execute_rescue_action as execute_db_rescue_action,
)
from backend.orchestration.recovery import (
    get_rescue_plan as get_db_rescue_plan,
)
from backend.state_machine.policy import allowed_actions, display_status_for
from backend.state_machine.states import (
    ACTION_ASK_BUYER_NEW_METHOD,
    ACTION_ASK_BUYER_TO_APPROVE,
    ACTION_CAPTURE,
    ACTION_CONTACT_SUPPORT,
    ACTION_NONE,
    ACTION_RECONCILE,
    ACTION_RETRY_SAME_REQUEST,
    ACTION_VOID,
    APPROVED,
    AUTHORIZE_REQUESTED,
    AWAITING_CONDITION,
    CAPTURE_REQUESTED,
    ERROR_ESCALATE,
    ERROR_OUR_BUG,
    ERROR_REAPPROVE,
    ERROR_SWITCH_METHOD,
    FINAL_STATUSES,
    INTENT_CREATED,
    UNCERTAIN,
    VOID_REQUESTED,
)

ERROR_ACTION = {
    ERROR_REAPPROVE: ACTION_ASK_BUYER_TO_APPROVE,
    ERROR_SWITCH_METHOD: ACTION_ASK_BUYER_NEW_METHOD,
    ERROR_ESCALATE: ACTION_CONTACT_SUPPORT,
    ERROR_OUR_BUG: ACTION_CONTACT_SUPPORT,
}
_RETRY_PROOF_STATUSES = frozenset(
    {
        INTENT_CREATED,
        APPROVED,
        AWAITING_CONDITION,
        AUTHORIZE_REQUESTED,
        CAPTURE_REQUESTED,
        VOID_REQUESTED,
    }
)
_AUDIT_ACTOR = "rescue"
_NOT_PERMITTED = {"error": "Action not permitted"}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def build_rescue_plan(intent: dict, last_error: dict | None, provider_status: str | None) -> dict:
    """The deterministic rescue plan for one payment.

    intent: {"status", "path", and optionally "reconcile_exhausted", "condition_verified",
             "authorization_valid", "retry_allowed"}. `retry_allowed` must be True only when
             reconciliation PROVED the operation did not happen.
    last_error: {"error_class": ...} or None.   provider_status: PayPal's last word or None.
    """
    status = _text(intent.get("status")) or ""
    path = _text(intent.get("path")) or ""
    error_class = _text((last_error or {}).get("error_class"))
    exhausted = intent.get("reconcile_exhausted") is True
    context = {
        "reconcile_exhausted": exhausted,
        "error_class": error_class,
        "condition_verified": intent.get("condition_verified"),
        "authorization_valid": intent.get("authorization_valid"),
        "retry_allowed": intent.get("retry_allowed") is True,
    }
    actions = list(allowed_actions(path, status, context))
    # An operation proven unsent / not done may be resent with its ORIGINAL request id. The
    # generic policy only knows some resting statuses, so the proof flag (set only from
    # persisted PayPal evidence, never by a caller's request) opens the retry here.
    if (
        context["retry_allowed"]
        and status in _RETRY_PROOF_STATUSES
        and ACTION_RETRY_SAME_REQUEST not in actions
    ):
        actions = (
            [ACTION_RETRY_SAME_REQUEST]
            if actions == [ACTION_NONE]
            else [
                *actions,
                ACTION_RETRY_SAME_REQUEST,
            ]
        )

    if status in FINAL_STATUSES and status != "FAILED":
        recommended = ACTION_NONE
    elif status == UNCERTAIN:
        recommended = ACTION_CONTACT_SUPPORT if exhausted else ACTION_RECONCILE
    elif status == "FAILED":
        recommended = ERROR_ACTION.get(error_class or "", actions[0])
    elif error_class in ERROR_ACTION and ERROR_ACTION[error_class] in actions:
        recommended = ERROR_ACTION[error_class]
    else:
        recommended = actions[0]
    if recommended not in actions:
        recommended = actions[0]

    facts = [f"Payment status is {status or 'unknown'}"]
    if path:
        facts.append(f"Payment path is {path}")
    if provider_status:
        facts.append(f"PayPal last reported {provider_status}")
    if error_class:
        facts.append(f"Last error class is {error_class}")
    if exhausted:
        facts.append("Reconciliation checks are exhausted")
    if context["retry_allowed"]:
        facts.append("PayPal showed the operation did not happen")

    return {
        "display_status": display_status_for(status, context),
        "facts": facts,
        "allowed_actions": actions,
        "recommended_action": recommended,
        "needs_human": recommended == ACTION_CONTACT_SUPPORT or exhausted,
    }


class RescueService(Protocol):
    """What execute_rescue_action needs from the outside world (database + PayPal)."""

    async def load_intent(self, intent_id: str) -> dict: ...

    async def reconcile(self, intent_id: str) -> dict: ...

    async def retry_same_request(self, intent_id: str) -> dict: ...

    async def run_operation(self, intent_id: str, action: str) -> dict: ...

    async def audit(self, intent_id: str, event_type: str, evidence: dict) -> None: ...


async def execute_rescue_action(service: RescueService, intent_id: str, action: str) -> dict:
    """Run one rescue action, but only if a freshly built plan allows it."""
    intent = await service.load_intent(intent_id)
    plan = build_rescue_plan(intent, intent.get("last_error"), intent.get("provider_status"))
    await service.audit(
        intent_id,
        "RESCUE_PLAN_BUILT",
        {
            "allowed_actions": plan["allowed_actions"],
            "recommended_action": plan["recommended_action"],
        },
    )
    if not isinstance(action, str) or action not in plan["allowed_actions"]:
        return dict(_NOT_PERMITTED)

    try:
        if action == ACTION_RECONCILE:
            result = await service.reconcile(intent_id)
        elif action == ACTION_RETRY_SAME_REQUEST:
            if intent.get("retry_allowed") is not True:
                return dict(_NOT_PERMITTED)
            result = await service.retry_same_request(intent_id)
        elif action in {ACTION_CAPTURE, ACTION_VOID}:
            result = await service.run_operation(intent_id, action)
        else:
            # WAIT, CONTACT_SUPPORT, ASK_*, NONE: nothing to run, the plan is the answer.
            result = {"executed": False, "plan": plan}
    except RescueActionNotAllowed:
        # The stored evidence changed since the plan was built: refuse, do not retry.
        return dict(_NOT_PERMITTED)

    await service.audit(
        intent_id,
        "RESCUE_ACTION_EXECUTED",
        {"action": action, "state": result.get("state"), "executed": result.get("executed", True)},
    )
    return {"action": action, "result": result}


class DbRescueService:
    """RescueService backed by PostgreSQL and the PayPal gateway for one request session."""

    def __init__(self, session: AsyncSession, gateway: Any, *, actor: str = "admin") -> None:
        self._session = session
        self._gateway = gateway
        self._actor = actor

    async def load_intent(self, intent_id: str) -> dict:
        payment_id = uuid.UUID(str(intent_id))
        session = self._session
        payment = await session.get(Payment, payment_id)
        if payment is None:
            raise ValueError("Payment does not exist")
        # Same persisted-evidence plan the API shows: it decides whether a retry is proven safe.
        stored = await get_db_rescue_plan(session, payment_id=payment_id)
        attempts = list(
            await session.scalars(
                select(PaymentAttempt).where(
                    PaymentAttempt.payment_id == payment_id,
                    PaymentAttempt.status.in_(UNRESOLVED),
                )
            )
        )
        exhausted = False
        if len(attempts) == 1 and attempts[0].status in {"STARTED", "UNCERTAIN", "PENDING"}:
            schedule = await session.get(PaymentReconciliation, attempts[0].id)
            exhausted = bool(schedule and schedule.completed_at is not None)
        condition = await session.scalar(
            select(PaymentCondition).where(
                PaymentCondition.payment_id == payment_id,
                PaymentCondition.status == "VERIFIED",
            )
        )
        return {
            "id": str(payment.id),
            "status": payment.status,
            "path": payment.mode,
            "reconcile_exhausted": exhausted or len(attempts) > 1,
            "condition_verified": condition is not None,
            "retry_allowed": stored.get("retry_allowed") is True,
            "last_error": None,
            "provider_status": None,
        }

    async def reconcile(self, intent_id: str) -> dict:
        outcome = await self._delegate(intent_id, ACTION_RECONCILE)
        return {"state": outcome["result"].get("payment_status"), **outcome["result"]}

    async def retry_same_request(self, intent_id: str) -> dict:
        outcome = await self._delegate(intent_id, ACTION_RETRY_SAME_REQUEST)
        result = outcome["result"]
        attempt = await self._session.scalar(
            select(PaymentAttempt)
            .where(PaymentAttempt.payment_id == uuid.UUID(str(intent_id)))
            .order_by(PaymentAttempt.started_at.desc())
            .limit(1)
        )
        return {
            **result,
            "request_id": attempt.request_id if attempt else None,
            "same_request_id": bool(
                attempt
                and attempt.request_id
                == stable_operation_request_id(attempt.payment_id, attempt.operation)
            ),
        }

    async def run_operation(self, intent_id: str, action: str) -> dict:
        outcome = await self._delegate(intent_id, action)
        return outcome["result"]

    async def audit(self, intent_id: str, event_type: str, evidence: dict) -> None:
        await add_audit_event(
            self._session,
            payment_id=intent_id,
            event_type=event_type,
            actor=_AUDIT_ACTOR,
            evidence={"source": "rescue", "confirmed": False, **evidence},
        )
        await self._session.commit()

    async def _delegate(self, intent_id: str, action: str) -> dict:
        return await execute_db_rescue_action(
            self._session,
            self._gateway,
            payment_id=uuid.UUID(str(intent_id)),
            action=action,
            actor=self._actor,
        )