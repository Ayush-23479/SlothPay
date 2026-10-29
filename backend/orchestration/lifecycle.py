"""Task 6 production-oriented, sandbox-only payment lifecycle.

Uses a durable attempt + exclusive state claim before each money operation.
One SQLAlchemy session is owned per invocation; do not pass unrelated pending writes.
Recovery of STARTED / UNCERTAIN attempts belongs to reconciliation, never blind retry.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.attempt import PaymentAttempt
from backend.models.payment import Payment
from backend.models.repository import StalePaymentVersion, transition_payment
from backend.payments.paypal_client import PayPalError, PayPalNotSent, PayPalOutcomeUnknown
from backend.state_machine.guards import authorization_capturable, claim_status_for
from backend.state_machine.transitions import validate_transition


class LifecycleSafetyError(Exception):
    """Precondition or response prevents a safe payment action."""


class OperationAlreadyStarted(LifecycleSafetyError):
    """A durable attempt already exists: inspect/reconcile instead of replaying."""


def stable_operation_request_id(payment_id: uuid.UUID, operation: str) -> str:
    """Temporary Task 6 fallback; replace with reviewed Task 5 ID derivation.

    This key must not be changed for an already attempted operation.
    """
    if operation not in {"create_order", "authorize", "capture", "void"}:
        raise LifecycleSafetyError("Unknown provider operation")
    digest = hashlib.sha256(f"slothpay:v1:{payment_id}:{operation}".encode()).hexdigest()
    return f"sp_{digest[:60]}"


def _request_id(payment_id: uuid.UUID, operation: str, request_id: str | None) -> str:
    generated = stable_operation_request_id(payment_id, operation)
    if request_id is not None and request_id != generated:
        raise LifecycleSafetyError(
            "Caller-specified request IDs are not accepted in Task 6; "
            "connect the reviewed Task 5 derivation before changing the scheme"
        )
    return generated


async def _load(session: AsyncSession, payment_id: uuid.UUID) -> Payment:
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise LifecycleSafetyError("Payment does not exist")
    return payment


async def _transition(
    session: AsyncSession,
    payment: Payment,
    to_status: str,
    *,
    evidence: dict[str, Any],
) -> None:
    allowed, reason = validate_transition(payment.mode, payment.status, to_status)
    if not allowed:
        raise LifecycleSafetyError(reason)
    version = await transition_payment(
        session,
        payment_id=payment.id,
        expected_version=payment.version,
        expected_status=payment.status,
        new_status=to_status,
        actor="orchestration",
        evidence=evidence,
    )
    payment.status, payment.version = to_status, version


async def _attempt(
    session: AsyncSession,
    payment: Payment,
    operation: str,
    *,
    request_id: str,
    claim_status: str | None,
) -> uuid.UUID:
    """Commit attempt and exclusive claim together, before outbound POST."""
    attempt = PaymentAttempt(
        payment_id=payment.id,
        provider="paypal",
        operation=operation,
        request_id=request_id,
        status="STARTED",
    )
    try:
        if claim_status is not None:
            await _transition(
                session,
                payment,
                claim_status,
                evidence={"operation": operation, "request_id": request_id, "phase": "claimed"},
            )
        session.add(attempt)
        await session.flush()
        await session.commit()
    except (IntegrityError, StalePaymentVersion) as exc:
        await session.rollback()
        raise OperationAlreadyStarted(
            "An operation already owns this payment or request ID"
        ) from exc
    return attempt.id


async def _record(
    session: AsyncSession,
    attempt_id: uuid.UUID,
    status: str,
    *,
    provider_operation_id: str | None = None,
) -> None:
    attempt = await session.get(PaymentAttempt, attempt_id)
    if attempt is None:
        raise LifecycleSafetyError("Committed attempt row was not found")
    attempt.status = status
    if provider_operation_id:
        attempt.provider_operation_id = provider_operation_id
    await session.commit()


async def _uncertain(
    session: AsyncSession,
    payment: Payment,
    attempt_id: uuid.UUID,
    operation: str,
    reason: str,
) -> None:
    attempt = await session.get(PaymentAttempt, attempt_id)
    if attempt is None:
        raise LifecycleSafetyError("Committed attempt row was not found")
    attempt.status = "UNCERTAIN"
    if payment.status != "UNCERTAIN":
        await _transition(
            session,
            payment,
            "UNCERTAIN",
            evidence={"operation": operation, "phase": "uncertain", "reason": reason},
        )
    await session.commit()


def _embedded_resources(order: dict[str, Any], key: str) -> list[dict[str, Any]]:
    units = order.get("purchase_units")
    if not isinstance(units, list) or len(units) != 1 or not isinstance(units[0], dict):
        return []
    payments = units[0].get("payments")
    if not isinstance(payments, dict):
        return []
    values = payments.get(key)
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, dict)]


def _provider_result(operation: str, response: dict[str, Any], mode: str) -> tuple[str, str | None]:
    """Return application state and provider operation ID, or fail closed."""
    if operation == "void":
        if response.get("http_status") == 204:
            return "VOIDED", None
        raise LifecycleSafetyError("Void result is unrecognized")
    if operation == "authorize":
        resources = _embedded_resources(response, "authorizations")
        if len(resources) == 1 and resources[0].get("status") == "CREATED":
            authorization_id = resources[0].get("id")
            if isinstance(authorization_id, str) and authorization_id:
                return "AUTHORIZED", authorization_id
        raise LifecycleSafetyError("Cannot identify a confirmed authorization")
    if mode == "SMARTPAY":
        resources = _embedded_resources(response, "captures")
        if len(resources) != 1:
            raise LifecycleSafetyError("Cannot identify exactly one PayPal order capture")
        resource = resources[0]
    else:
        resource = response
    operation_id = resource.get("id")
    status = resource.get("status")
    if not isinstance(operation_id, str) or not operation_id:
        raise LifecycleSafetyError("PayPal capture ID is missing")
    if status == "COMPLETED":
        return "CAPTURED", operation_id
    if status == "PENDING":
        return "CAPTURE_PENDING", operation_id
    raise LifecycleSafetyError("PayPal capture outcome is not definitive")


async def create_order(
    session: AsyncSession,
    paypal: Any,
    *,
    payment_id: uuid.UUID,
) -> dict[str, Any]:
    """Create one sandbox order for a persisted INTENT_CREATED payment."""
    payment = await _load(session, payment_id)
    if payment.status != "INTENT_CREATED" or payment.paypal_order_id:
        raise LifecycleSafetyError("Payment is not eligible for order creation")
    request_id = _request_id(payment.id, "create_order", None)
    attempt_id = await _attempt(
        session, payment, "create_order", request_id=request_id, claim_status=None
    )
    try:
        response = await paypal.create_order(
            intent="CAPTURE" if payment.mode == "SMARTPAY" else "AUTHORIZE",
            amount=f"{payment.amount:.2f}",
            currency=payment.currency,
            request_id=request_id,
        )
    except PayPalNotSent:
        await _record(session, attempt_id, "NOT_SENT")
        raise
    except (PayPalOutcomeUnknown, PayPalError) as exc:
        await _uncertain(session, payment, attempt_id, "create_order", type(exc).__name__)
        raise
    order_id = response.get("id")
    if (
        not isinstance(order_id, str)
        or not order_id
        or response.get("status") not in {"CREATED", "PAYER_ACTION_REQUIRED"}
    ):
        await _uncertain(session, payment, attempt_id, "create_order", "unexpected_order_response")
        return {"state": "UNCERTAIN", "needs_reconciliation": True}
    payment.paypal_order_id = order_id
    await _transition(
        session,
        payment,
        "ORDER_CREATED",
        evidence={"operation": "create_order", "order_id": order_id},
    )
    attempt = await session.get(PaymentAttempt, attempt_id)
    attempt.status = "SUCCEEDED"
    attempt.provider_operation_id = order_id
    await session.commit()
    return {"state": "ORDER_CREATED", "paypal_order_id": order_id, "response": response}


async def verify_buyer_approval(
    session: AsyncSession,
    paypal: Any,
    *,
    payment_id: uuid.UUID,
) -> dict[str, str]:
    """Derive approval from PayPal GET, never from frontend flags."""
    payment = await _load(session, payment_id)
    if payment.status not in {"ORDER_CREATED", "AWAITING_APPROVAL"}:
        raise LifecycleSafetyError("Invalid state for buyer approval")
    if not payment.paypal_order_id:
        raise LifecycleSafetyError("PayPal order ID not persisted")
    response = await paypal.get_order(payment.paypal_order_id)
    if response.get("id") != payment.paypal_order_id:
        raise LifecycleSafetyError("PayPal order mismatch")
    if payment.status == "ORDER_CREATED":
        await _transition(
            session, payment, "AWAITING_APPROVAL", evidence={"phase": "awaiting_approval"}
        )
        await session.commit()
    if response.get("status") != "APPROVED":
        return {"state": "AWAITING_APPROVAL"}
    await _transition(
        session,
        payment,
        "APPROVED",
        evidence={"phase": "approved", "paypal_order_id": payment.paypal_order_id},
    )
    await session.commit()
    return {"state": "APPROVED"}


async def execute_operation(
    session: AsyncSession,
    paypal: Any,
    *,
    payment_id: uuid.UUID,
    operation: str,
    condition_verified: bool = False,
) -> dict[str, Any]:
    """Commit exclusive claim+attempt, then send one provider money operation.

    Conditional authorization ID is retrieved from the *committed* authorize attempt.
    Conditions must be independently verified by a trusted caller; Task 9 owns proof.
    """
    payment = await _load(session, payment_id)
    if operation not in {"authorize", "capture", "void"}:
        raise LifecycleSafetyError("Unsupported operation")
    if payment.mode == "SMARTPAY" and operation != "capture":
        raise LifecycleSafetyError("SmartPay supports capture only")
    if operation == "authorize" or payment.mode == "SMARTPAY":
        if not payment.paypal_order_id:
            raise LifecycleSafetyError("Missing persisted PayPal order ID")
        if payment.status != "APPROVED":
            raise LifecycleSafetyError("Approval must be verified and persisted first")
        provider_id = payment.paypal_order_id
    else:
        authorization_attempt = await session.scalar(
            select(PaymentAttempt).where(
                PaymentAttempt.payment_id == payment.id,
                PaymentAttempt.operation == "authorize",
                PaymentAttempt.status == "SUCCEEDED",
            )
        )
        provider_id = authorization_attempt.provider_operation_id if authorization_attempt else None
        if not provider_id:
            raise LifecycleSafetyError("No confirmed stored authorization ID")
    context: dict[str, bool] = {"condition_verified": condition_verified}
    if payment.mode == "CONDITIONALPAY" and operation == "capture":
        auth = await paypal.get_authorization(provider_id)
        context["authorization_valid"] = authorization_capturable(auth)
    claim_status = claim_status_for(payment.mode, payment.status, operation, context)
    request_id = _request_id(payment.id, operation, None)
    attempt_id = await _attempt(
        session, payment, operation, request_id=request_id, claim_status=claim_status
    )
    try:
        if operation == "authorize":
            response = await paypal.authorize_order(provider_id, request_id=request_id)
        elif operation == "void":
            response = await paypal.void_authorization(provider_id, request_id=request_id)
        elif payment.mode == "SMARTPAY":
            response = await paypal.capture_order(provider_id, request_id=request_id)
        else:
            response = await paypal.capture_authorization(provider_id, request_id=request_id)
    except PayPalNotSent:
        await _record(session, attempt_id, "NOT_SENT")
        # Claim is intentionally retained; a recovery worker must safely reconcile.
        raise
    except (PayPalOutcomeUnknown, PayPalError) as exc:
        await _uncertain(session, payment, attempt_id, operation, type(exc).__name__)
        raise
    try:
        target, operation_id = _provider_result(operation, response, payment.mode)
    except LifecycleSafetyError:
        await _uncertain(session, payment, attempt_id, operation, "unrecognized_provider_result")
        return {"state": "UNCERTAIN", "needs_reconciliation": True}
    await _transition(
        session,
        payment,
        target,
        evidence={"operation": operation, "provider_operation_id": operation_id},
    )
    attempt = await session.get(PaymentAttempt, attempt_id)
    attempt.status = "SUCCEEDED"
    attempt.provider_operation_id = operation_id
    await session.commit()
    # AUTHORIZED -> AWAITING_CONDITION is a separate permitted transition.
    if target == "AUTHORIZED":
        await _transition(
            session,
            payment,
            "AWAITING_CONDITION",
            evidence={"operation": "authorize", "phase": "awaiting_condition"},
        )
        await session.commit()
        target = "AWAITING_CONDITION"
    return {"state": target, "needs_reconciliation": False, "response": response}
