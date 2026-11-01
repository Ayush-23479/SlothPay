
from __future__ import annotations

import inspect
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.condition import PaymentCondition
from backend.models.payment import Payment
from backend.models.repository import add_audit_event, get_intent_created_evidence
from backend.orchestration.lifecycle import (
    LifecycleSafetyError,
    OperationAlreadyStarted,
    execute_operation,
)
from backend.payments.paypal_client import PayPalError, PayPalNotSent, PayPalOutcomeUnknown
from backend.state_machine.guards import OperationNotPermitted
from backend.state_machine.states import (
    APPROVED,
    AUTHORIZE_REQUESTED,
    AUTHORIZED,
    AWAITING_APPROVAL,
    AWAITING_CONDITION,
    CAPTURE_PENDING,
    CAPTURE_REQUESTED,
    CONDITIONALPAY,
    FINAL_STATUSES,
    INTENT_CREATED,
    ORDER_CREATED,
    UNCERTAIN,
    VOID_REQUESTED,
)

# Another operation is already running or unresolved: reconcile it, never start a second one.
IN_FLIGHT_STATUSES = frozenset({CAPTURE_REQUESTED, CAPTURE_PENDING, VOID_REQUESTED, UNCERTAIN})
# No authorization exists yet, so there is nothing to void.
NO_FUNDS_HELD_STATUSES = frozenset({INTENT_CREATED, ORDER_CREATED, AWAITING_APPROVAL, APPROVED})
# Authorization is being created right now: try again shortly.
TRANSIENT_STATUSES = frozenset({AUTHORIZE_REQUESTED, AUTHORIZED})

_CONDITION_STATUS = {
    "verified": "VERIFIED",
    "failed": "FAILED",
    "cancelled": "CANCELLED",
    "deadline": "EXPIRED",
    "invalid_deadline": "EXPIRED",
}
_DECIDED = frozenset(_CONDITION_STATUS.values())


log = logging.getLogger("slothpay.conditions")


class PaymentMissing(Exception):
    """The payment does not exist. Retrying cannot help."""


class ConditionNotReady(Exception):
    """The payment is not yet in a state where the condition can be resolved. Retry later."""


class PaymentService:
    def __init__(
        self,
        session_factory: Callable[[], Any],
        gateway: Any,
        on_event: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._gateway = gateway
        # Called after a change is committed, so live screens can refresh (Task 10).
        # It is injected, never imported: a failing notifier must not undo a payment fact.
        self._on_event = on_event

    async def _notify(self, intent_id: str, kind: str) -> None:
        if self._on_event is None:
            return
        try:
            result = self._on_event(intent_id, {"type": kind})
            if inspect.isawaitable(result):
                await result
        except Exception:
            log.exception("live-update notification failed for payment %s", intent_id)

    async def resolve_condition(
        self,
        intent_id: str,
        verified: bool,
        evidence: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """verified=True captures the held funds; anything else voids them (default deny)."""
        try:
            return await self._resolve_condition(intent_id, verified, evidence, reason)
        finally:
            await self._notify(intent_id, "condition.resolved")

    async def _resolve_condition(
        self,
        intent_id: str,
        verified: bool,
        evidence: dict[str, Any] | None,
        reason: str | None,
    ) -> dict[str, Any]:
        payment_id = _payment_id(intent_id)
        reason = reason or ("verified" if verified else "failed")
        async with self._session_factory() as session:
            payment = await session.get(Payment, payment_id)
            if payment is None:
                raise PaymentMissing(intent_id)
            status = payment.status
            if payment.mode != CONDITIONALPAY:
                return _result(status, "NONE", skipped="not_conditional")
            if status in FINAL_STATUSES:
                return _result(status, "NONE", already_final=True)
            if status in IN_FLIGHT_STATUSES:
                await self._record_condition(session, payment_id, verified, reason, evidence)
                return _result(status, "NONE", needs_reconciliation=True)
            if status in TRANSIENT_STATUSES:
                raise ConditionNotReady(f"payment is {status}")
            if status != AWAITING_CONDITION:
                if verified:
                    # Verified before the authorization finished: wait for it, never skip it.
                    raise ConditionNotReady(f"payment is {status}")
                await self._record_condition(session, payment_id, False, reason, evidence)
                return _result(status, "NONE", nothing_to_void=status in NO_FUNDS_HELD_STATUSES)

            await self._record_condition(session, payment_id, verified, reason, evidence)
            if verified:
                return await self._capture_or_void(session, payment_id, reason)
            return await self._run(session, payment_id, "void", "VOID")

    async def mark_condition_expired(
        self, intent_id: str, reason: str = "deadline"
    ) -> dict[str, Any]:
        """Record that the deadline passed. A verified condition is never overwritten."""
        try:
            return await self._mark_condition_expired(intent_id, reason)
        finally:
            await self._notify(intent_id, "condition.expired")

    async def _mark_condition_expired(self, intent_id: str, reason: str) -> dict[str, Any]:
        payment_id = _payment_id(intent_id)
        async with self._session_factory() as session:
            if await session.get(Payment, payment_id) is None:
                raise PaymentMissing(intent_id)
            await self._record_condition(session, payment_id, False, reason, {"reason": reason})
            row = await _latest_condition(session, payment_id)
            return {"condition_status": row.status if row else None}

    async def _capture_or_void(
        self, session: AsyncSession, payment_id: uuid.UUID, reason: str
    ) -> dict[str, Any]:
        try:
            return await self._run(session, payment_id, "capture", "CAPTURE", raise_safety=True)
        except (LifecycleSafetyError, OperationNotPermitted) as exc:
            # Capture was refused before any PayPal call (e.g. the authorization expired or is
            # no longer capturable). The held funds must not linger: void them instead.
            await add_audit_event(
                session,
                payment_id=payment_id,
                event_type="CONDITION_CAPTURE_REFUSED",
                actor="workflow",
                evidence={
                    "source": "internal_record",
                    "confirmed": False,
                    "reason": str(exc)[:200],
                },
            )
            await session.commit()
            result = await self._run(session, payment_id, "void", "VOID")
            result["capture_refused"] = True
            return result

    async def _run(
        self,
        session: AsyncSession,
        payment_id: uuid.UUID,
        operation: str,
        action: str,
        *,
        raise_safety: bool = False,
    ) -> dict[str, Any]:
        try:
            outcome = await execute_operation(
                session,
                self._gateway,
                payment_id=payment_id,
                operation=operation,
                condition_verified=operation == "capture",
            )
            return _result(
                outcome["state"],
                action,
                needs_reconciliation=outcome.get("needs_reconciliation", False),
            )
        except OperationAlreadyStarted:
            return _result(await _status(session, payment_id), "NONE", needs_reconciliation=True)
        except (PayPalNotSent, PayPalOutcomeUnknown, PayPalError):
            # The attempt and the UNCERTAIN/claimed state are already saved. Never replay it:
            # reconciliation finds out what happened.
            await session.rollback()
            return _result(await _status(session, payment_id), action, needs_reconciliation=True)
        except (LifecycleSafetyError, OperationNotPermitted):
            if raise_safety:
                raise
            await session.rollback()
            return _result(await _status(session, payment_id), "NONE", needs_reconciliation=True)

    async def _record_condition(
        self,
        session: AsyncSession,
        payment_id: uuid.UUID,
        verified: bool,
        reason: str,
        evidence: dict[str, Any] | None,
    ) -> None:
        wanted = "VERIFIED" if verified else _CONDITION_STATUS.get(reason, "FAILED")
        row = await _latest_condition(session, payment_id)
        now = datetime.now(UTC)
        if row is None:
            row = PaymentCondition(
                payment_id=payment_id,
                condition_type=await _condition_type(session, payment_id),
                status="PENDING",
            )
            session.add(row)
        if row.status in _DECIDED:
            await session.commit()  # the first decision stands: never overwrite or downgrade it
            return
        row.status = wanted
        row.evidence = _safe_evidence(evidence, reason)
        row.verified_by = "workflow"
        row.verified_at = now
        await session.commit()


def _payment_id(intent_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(intent_id))
    except ValueError as exc:
        raise PaymentMissing(str(intent_id)) from exc


def _result(state: str, action: str, **extra: Any) -> dict[str, Any]:
    return {"state": state, "action": action, "needs_reconciliation": False, **extra}


async def _status(session: AsyncSession, payment_id: uuid.UUID) -> str:
    payment = await session.get(Payment, payment_id, populate_existing=True)
    return payment.status if payment else "UNKNOWN"


async def _latest_condition(
    session: AsyncSession, payment_id: uuid.UUID
) -> PaymentCondition | None:
    return await session.scalar(
        select(PaymentCondition)
        .where(PaymentCondition.payment_id == payment_id)
        .order_by(PaymentCondition.created_at.desc(), PaymentCondition.id.desc())
        .limit(1)
    )


async def _condition_type(session: AsyncSession, payment_id: uuid.UUID) -> str:
    original = await get_intent_created_evidence(session, payment_id)
    condition = original.get("condition") if isinstance(original, dict) else None
    if isinstance(condition, dict):
        for field in ("type", "kind"):
            if isinstance(condition.get(field), str) and condition[field]:
                return condition[field][:64]
    return "seller_condition"


def _safe_evidence(evidence: dict[str, Any] | None, reason: str) -> dict[str, Any]:
    clean = evidence if isinstance(evidence, dict) else {}
    return {"reason": reason, "evidence": clean}