"""Inspect unresolved operations and execute evidence-guarded rescue actions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.attempt import PaymentAttempt
from backend.models.condition import PaymentCondition
from backend.models.payment import Payment
from backend.models.reconciliation import PaymentReconciliation
from backend.models.repository import add_audit_event
from backend.orchestration.lifecycle import (
    LifecycleSafetyError,
    _complete_reconciliation,
    _provider_result,
    _schedule_reconciliation,
    _transition,
    _uncertain,
    execute_operation,
    stable_operation_request_id,
)
from backend.orchestration.reconcile import (
    UNRESOLVED,
    ReconciliationObservation,
    reconcile_attempt,
)
from backend.payments.paypal_client import PayPalError, PayPalNotSent, PayPalOutcomeUnknown
from backend.state_machine.guards import authorization_capturable
from backend.state_machine.policy import allowed_actions, display_status_for
from backend.state_machine.states import (
    ACTION_CAPTURE,
    ACTION_CONTACT_SUPPORT,
    ACTION_NONE,
    ACTION_RECONCILE,
    ACTION_RETRY_SAME_REQUEST,
    ACTION_VOID,
    ACTION_WAIT,
    FINAL_STATUSES,
)


@dataclass(frozen=True)
class PendingAttempt:
    payment_id: uuid.UUID
    payment_status: str
    operation: str
    request_id: str
    attempt_status: str
    provider_operation_id: str | None


async def inspect_unresolved_attempts(
    session: AsyncSession, *, payment_id: uuid.UUID
) -> list[PendingAttempt]:
    """Return unresolved attempts, without executing or retrying operations."""
    rows = await session.execute(
        select(PaymentAttempt, Payment)
        .join(Payment, PaymentAttempt.payment_id == Payment.id)
        .where(
            PaymentAttempt.payment_id == payment_id,
            PaymentAttempt.status.in_(UNRESOLVED),
        )
        .order_by(PaymentAttempt.started_at, PaymentAttempt.id)
    )
    return [
        PendingAttempt(
            payment_id=payment.id,
            payment_status=payment.status,
            operation=attempt.operation,
            request_id=attempt.request_id,
            attempt_status=attempt.status,
            provider_operation_id=attempt.provider_operation_id,
        )
        for attempt, payment in rows.all()
    ]


class RescueActionNotAllowed(Exception):
    """The requested rescue action is not currently supported by persisted evidence."""


def _buyer_state(status: str, *, action_needed: bool) -> str:
    if action_needed:
        return "Action needed"
    display = display_status_for(status)
    return {
        "COMPLETED": "Completed",
        "PENDING": "Pending",
        "CLOSED": "Closed",
        "ACTION_NEEDED": "Action needed",
    }[display]


async def get_rescue_plan(session: AsyncSession, *, payment_id: uuid.UUID) -> dict:
    """Return a deterministic action plan from persisted state and trusted evidence only."""
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise ValueError("Payment does not exist")
    attempts = list(
        await session.scalars(
            select(PaymentAttempt)
            .where(
                PaymentAttempt.payment_id == payment_id,
                PaymentAttempt.status.in_(UNRESOLVED),
            )
            .order_by(PaymentAttempt.started_at, PaymentAttempt.id)
        )
    )
    if len(attempts) > 1:
        return {
            "payment_id": str(payment.id),
            "payment_state": payment.status,
            "buyer_facing_state": "Action needed",
            "allowed_actions": [ACTION_CONTACT_SUPPORT],
            "recommended_action": ACTION_CONTACT_SUPPORT,
            "evidence_reason": "Multiple unresolved provider attempts require operator review.",
        }

    if attempts:
        attempt = attempts[0]
        schedule = await session.get(PaymentReconciliation, attempt.id)
        exhausted = bool(
            schedule
            and schedule.completed_at is not None
            and attempt.status in {"STARTED", "UNCERTAIN", "PENDING"}
        )
        if attempt.status == "NOT_SENT":
            retry_allowed = (
                (attempt.operation == "create_order" and payment.status == "INTENT_CREATED")
                or (attempt.operation == "authorize" and payment.status == "AUTHORIZE_REQUESTED")
                or (attempt.operation == "capture" and payment.status == "CAPTURE_REQUESTED")
                or (attempt.operation == "void" and payment.status == "VOID_REQUESTED")
            )
            if payment.mode == "CONDITIONALPAY" and attempt.operation == "capture":
                retry_allowed = False
            actions = [ACTION_RETRY_SAME_REQUEST] if retry_allowed else [ACTION_CONTACT_SUPPORT]
            reason = (
                "The durable attempt records that the PayPal money request was never sent."
                if retry_allowed
                else "A request was not sent, but the current payment state does not permit retry."
            )
            return {
                "payment_id": str(payment.id),
                "payment_state": payment.status,
                "buyer_facing_state": _buyer_state(payment.status, action_needed=not retry_allowed),
                "allowed_actions": actions,
                "recommended_action": actions[0],
                "evidence_reason": reason,
            }
        if attempt.status == "NOT_OCCURRED":
            retry_allowed = (
                attempt.operation == "void"
                and payment.status == "AWAITING_CONDITION"
                and payment.mode == "CONDITIONALPAY"
            )
            actions = [ACTION_RETRY_SAME_REQUEST] if retry_allowed else [ACTION_CONTACT_SUPPORT]
            return {
                "payment_id": str(payment.id),
                "payment_state": payment.status,
                "buyer_facing_state": _buyer_state(
                    payment.status, action_needed=not retry_allowed
                ),
                "allowed_actions": actions,
                "recommended_action": actions[0],
                "evidence_reason": (
                    "PayPal confirms the authorization remains active; only a same-ID void retry "
                    "is permitted after a fresh status check."
                    if retry_allowed
                    else "PayPal confirmed the operation did not occur, but current state does not "
                    "permit retry."
                ),
            }
        if exhausted or payment.status in FINAL_STATUSES:
            action = ACTION_CONTACT_SUPPORT
            reason = (
                "Five PayPal checks did not establish a safe outcome."
                if exhausted
                else "The payment is final; unresolved attempt evidence cannot reopen it."
            )
            return {
                "payment_id": str(payment.id),
                "payment_state": payment.status,
                "buyer_facing_state": _buyer_state(
                    payment.status, action_needed=exhausted or payment.status not in FINAL_STATUSES
                ),
                "allowed_actions": [action],
                "recommended_action": action,
                "evidence_reason": reason,
            }
        actions = [ACTION_RECONCILE, ACTION_WAIT]
        due = schedule is None or schedule.next_check_at <= datetime.now(UTC)
        recommended = ACTION_RECONCILE if due else ACTION_WAIT
        return {
            "payment_id": str(payment.id),
            "payment_state": payment.status,
            "buyer_facing_state": _buyer_state(payment.status, action_needed=False),
            "allowed_actions": actions,
            "recommended_action": recommended,
            "evidence_reason": (
                "The operation outcome is not yet authoritative; no money operation may be retried."
            ),
        }

    if payment.status == "UNCERTAIN":
        return {
            "payment_id": str(payment.id),
            "payment_state": payment.status,
            "buyer_facing_state": "Action needed",
            "allowed_actions": [ACTION_CONTACT_SUPPORT],
            "recommended_action": ACTION_CONTACT_SUPPORT,
            "evidence_reason": (
                "No unique durable provider attempt can be matched to the uncertain state."
            ),
        }

    condition = await session.scalar(
        select(PaymentCondition).where(
            PaymentCondition.payment_id == payment_id,
            PaymentCondition.status == "VERIFIED",
        )
    )
    context = {"condition_verified": condition is not None}
    actions = allowed_actions(payment.mode, payment.status, context)
    recommended = actions[0]
    return {
        "payment_id": str(payment.id),
        "payment_state": payment.status,
        "buyer_facing_state": _buyer_state(payment.status, action_needed=False),
        "allowed_actions": actions,
        "recommended_action": recommended,
        "evidence_reason": (
            "No unresolved provider attempt exists; actions follow the persisted payment policy."
        ),
    }


async def _retry_not_sent(
    session: AsyncSession,
    paypal,
    *,
    payment_id: uuid.UUID,
    actor: str,
) -> dict:
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
    if payment is None:
        raise ValueError("Payment does not exist")
    attempts = list(
        await session.scalars(
            select(PaymentAttempt)
            .where(
                PaymentAttempt.payment_id == payment_id,
                PaymentAttempt.status.in_(UNRESOLVED),
            )
            .with_for_update()
        )
    )
    if len(attempts) != 1 or attempts[0].status not in {"NOT_SENT", "NOT_OCCURRED"}:
        raise RescueActionNotAllowed("There is no unique operation proven safe to retry")
    attempt = attempts[0]
    operation = attempt.operation
    attempt_status = attempt.status
    attempt_id = attempt.id
    request_id = stable_operation_request_id(payment.id, operation)
    if attempt.request_id != request_id:
        raise RescueActionNotAllowed("Stored request ID does not match the stable operation ID")
    expected_state = {
        "create_order": "INTENT_CREATED",
        "authorize": "AUTHORIZE_REQUESTED",
        "capture": "CAPTURE_REQUESTED",
        "void": "VOID_REQUESTED",
    }.get(operation)
    allowed_states = {expected_state}
    if attempt_status == "NOT_OCCURRED":
        if operation != "void":
            raise RescueActionNotAllowed("Only a confirmed not-occurred void may be retried")
        allowed_states.add("AWAITING_CONDITION")
    if payment.status not in allowed_states:
        raise RescueActionNotAllowed("Payment state changed; the retry plan is stale")
    if operation == "capture" and payment.mode == "CONDITIONALPAY":
        raise RescueActionNotAllowed("Conditional capture requires the trusted Task 9 proof flow")

    provider_id = payment.paypal_order_id
    if operation == "void":
        authorization = await session.scalar(
            select(PaymentAttempt).where(
                PaymentAttempt.payment_id == payment.id,
                PaymentAttempt.operation == "authorize",
                PaymentAttempt.status == "SUCCEEDED",
            )
        )
        provider_id = authorization.provider_operation_id if authorization else None
        if not provider_id:
            raise RescueActionNotAllowed("No confirmed authorization ID is available")
        current_authorization = await paypal.get_authorization(provider_id)
        amount = (
            current_authorization.get("amount")
            if isinstance(current_authorization, dict)
            else None
        )
        try:
            amount_matches = (
                isinstance(amount, dict)
                and Decimal(amount.get("value")) == payment.amount
                and amount.get("currency_code") == payment.currency
            )
        except (InvalidOperation, TypeError, ValueError):
            amount_matches = False
        if (
            not isinstance(current_authorization, dict)
            or current_authorization.get("id") != provider_id
            or not amount_matches
        ):
            raise RescueActionNotAllowed("Fresh authorization evidence does not match this payment")
        if current_authorization.get("status") == "VOIDED":
            target = "VOIDED"
            if payment.status != target:
                await _transition(
                    session,
                    payment,
                    target,
                    evidence={
                        "source": "paypal_get",
                        "confirmed": True,
                        "operation": operation,
                        "request_id": request_id,
                        "provider_operation_id": provider_id,
                    },
                )
            attempt.status = "SUCCEEDED"
            reconciliation = await session.get(PaymentReconciliation, attempt_id)
            if reconciliation is not None:
                reconciliation.completed_at = datetime.now(UTC)
                reconciliation.last_reason = "Fresh PayPal evidence confirms void"
            await session.commit()
            return {"state": target, "needs_reconciliation": False}
        if current_authorization.get("status") != "CREATED":
            raise RescueActionNotAllowed("Fresh authorization status does not permit a void retry")
    if operation == "capture" and payment.mode == "CONDITIONALPAY":
        condition = await session.scalar(
            select(PaymentCondition).where(
                PaymentCondition.payment_id == payment.id,
                PaymentCondition.status == "VERIFIED",
            )
        )
        authorization_state = await paypal.get_authorization(provider_id)
        if condition is None or not authorization_capturable(authorization_state):
            raise RescueActionNotAllowed("Trusted condition or live authorization is missing")
    if operation != "create_order" and not provider_id:
        raise RescueActionNotAllowed("The stored PayPal operation identifier is missing")

    mode = payment.mode
    amount = f"{payment.amount:.2f}"
    currency = payment.currency
    if attempt_status == "NOT_OCCURRED" and payment.status == "AWAITING_CONDITION":
        await _transition(
            session,
            payment,
            "VOID_REQUESTED",
            evidence={
                "source": "internal_record",
                "confirmed": False,
                "operation": operation,
                "request_id": request_id,
                "phase": "retry_claimed",
            },
        )
    attempt.status = "STARTED"
    await _schedule_reconciliation(session, payment, attempt)
    await add_audit_event(
        session,
        payment_id=payment.id,
        event_type="RESCUE_RETRY_CLAIMED",
        actor=actor,
        evidence={
            "source": "internal_record",
            "confirmed": False,
            "operation": operation,
            "request_id": request_id,
            "reason": "previous request is durably proven not sent",
        },
    )
    await session.commit()

    try:
        if operation == "create_order":
            response = await paypal.create_order(
                intent="CAPTURE" if mode == "SMARTPAY" else "AUTHORIZE",
                amount=amount,
                currency=currency,
                request_id=request_id,
            )
            order_id = response.get("id")
            if (
                not isinstance(order_id, str)
                or not order_id
                or response.get("status") not in {"CREATED", "PAYER_ACTION_REQUIRED"}
            ):
                raise LifecycleSafetyError("PayPal create-order response is not definitive")
            payment.paypal_order_id = order_id
            target, provider_operation_id = "ORDER_CREATED", order_id
        elif operation == "authorize":
            response = await paypal.authorize_order(provider_id, request_id=request_id)
            target, provider_operation_id = _provider_result(operation, response, mode)
        elif operation == "capture":
            if mode == "SMARTPAY":
                response = await paypal.capture_order(provider_id, request_id=request_id)
            else:
                response = await paypal.capture_authorization(provider_id, request_id=request_id)
            target, provider_operation_id = _provider_result(operation, response, mode)
        else:
            response = await paypal.void_authorization(provider_id, request_id=request_id)
            target, provider_operation_id = _provider_result(operation, response, mode)
    except PayPalNotSent as exc:
        attempt = await session.get(PaymentAttempt, attempt_id)
        if attempt is None:
            raise LifecycleSafetyError("Committed attempt row was not found") from exc
        attempt.status = "NOT_SENT"
        await add_audit_event(
            session,
            payment_id=payment.id,
            event_type="PROVIDER_OPERATION_NOT_SENT",
            actor=actor,
            evidence={
                "source": "internal_client",
                "confirmed": False,
                "operation": operation,
                "request_id": request_id,
                "reason": type(exc).__name__,
            },
        )
        await session.commit()
        raise
    except (PayPalOutcomeUnknown, PayPalError) as exc:
        await _uncertain(session, payment, attempt_id, operation, type(exc).__name__)
        raise
    except LifecycleSafetyError:
        await _uncertain(session, payment, attempt_id, operation, "unrecognized_provider_response")
        return {"state": "UNCERTAIN", "needs_reconciliation": True}

    await _transition(
        session,
        payment,
        target,
        evidence={
            "source": "paypal_response",
            "confirmed": True,
            "operation": operation,
            "request_id": request_id,
            "provider_operation_id": provider_operation_id,
        },
    )
    attempt = await session.get(PaymentAttempt, attempt_id)
    if attempt is None:
        raise LifecycleSafetyError("Committed attempt row was not found")
    attempt.status = "PENDING" if target == "CAPTURE_PENDING" else "SUCCEEDED"
    if provider_operation_id:
        attempt.provider_operation_id = provider_operation_id
    if target == "AUTHORIZED":
        await _transition(
            session,
            payment,
            "AWAITING_CONDITION",
            evidence={
                "source": "paypal_response",
                "confirmed": True,
                "operation": operation,
                "request_id": request_id,
            },
        )
    if target == "CAPTURE_PENDING":
        await _schedule_reconciliation(session, payment, attempt)
    else:
        await _complete_reconciliation(
            session, attempt_id, "PayPal operation response confirmed"
        )
    await session.commit()
    return {"state": payment.status, "needs_reconciliation": False}


async def execute_rescue_action(
    session: AsyncSession,
    paypal,
    *,
    payment_id: uuid.UUID,
    action: str,
    actor: str = "admin",
) -> dict:
    """Rebuild the plan immediately before action execution; reject stale/forbidden input."""
    plan = await get_rescue_plan(session, payment_id=payment_id)
    if action not in plan["allowed_actions"] or action == ACTION_NONE:
        raise RescueActionNotAllowed("Requested rescue action is not currently allowed")
    if action == ACTION_RECONCILE:
        pending = list(
            await session.scalars(
                select(PaymentAttempt)
                .where(
                    PaymentAttempt.payment_id == payment_id,
                    PaymentAttempt.status.in_(("STARTED", "UNCERTAIN", "PENDING")),
                )
                .order_by(PaymentAttempt.started_at, PaymentAttempt.id)
            )
        )
        if len(pending) != 1:
            raise RescueActionNotAllowed("There is no unique unresolved attempt to reconcile")
        result: ReconciliationObservation = await reconcile_attempt(
            session, paypal, attempt_id=pending[0].id, force=True
        )
        return {"action": action, "result": result.to_dict()}
    if action == ACTION_RETRY_SAME_REQUEST:
        return {
            "action": action,
            "result": await _retry_not_sent(
                session, paypal, payment_id=payment_id, actor=actor
            ),
        }
    if action in {ACTION_CAPTURE, ACTION_VOID}:
        result = await execute_operation(
            session,
            paypal,
            payment_id=payment_id,
            operation="capture" if action == ACTION_CAPTURE else "void",
        )
        return {"action": action, "result": result}
    if action in {
        ACTION_WAIT,
        ACTION_CONTACT_SUPPORT,
        "ASK_BUYER_TO_APPROVE",
        "ASK_BUYER_NEW_METHOD",
    }:
        return {"action": action, "result": plan}
    raise RescueActionNotAllowed("Rescue action has no safe execution handler")
