"""Task 6 claim-before-execute core (sandbox only).

This module implements the safety-critical payment execution boundary. It is not
an order-creation or webhook orchestration API. The caller must obtain the payment
ID, verified buyer approval, provider IDs and stable request ID beforehand.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.payment import Payment
from backend.models.repository import StalePaymentVersion, transition_payment
from backend.payments.paypal_client import PayPalNotSent, PayPalOutcomeUnknown
from backend.state_machine.guards import authorization_capturable, claim_status_for
from backend.state_machine.transitions import validate_transition


class PaymentNotFound(Exception):
    """The internal payment intent does not exist."""


class PaymentSafetyError(Exception):
    """A precondition prevents initiating a PayPal financial operation."""


async def _persist_transition(
    session: AsyncSession,
    payment: Payment,
    *,
    new_status: str,
    actor: str,
    evidence: dict[str, Any],
) -> None:
    """Validate and commit one CAS transition before returning."""
    ok, reason = validate_transition(payment.mode, payment.status, new_status)
    if not ok:
        raise PaymentSafetyError(reason)
    new_version = await transition_payment(
        session,
        payment_id=payment.id,
        expected_version=payment.version,
        expected_status=payment.status,
        new_status=new_status,
        actor=actor,
        evidence=evidence,
    )
    await session.commit()
    payment.status = new_status
    payment.version = new_version


async def execute_payment_operation(
    session: AsyncSession,
    paypal: Any,
    *,
    payment_id: uuid.UUID,
    operation: str,
    provider_id: str,
    request_id: str,
    condition_verified: bool = False,
    buyer_approved: bool = False,
) -> dict[str, Any]:
    """Atomically claim capture/void/authorize, then make at most one PayPal call.

    Preconditions:
      * payment already exists with an appropriate persisted state;
      * provider_id is the PayPal order ID for SmartPay capture or authorize,
        or the authorization ID for ConditionalPay capture/void;
      * request_id is a caller-owned stable idempotency ID (Task 5);
      * buyer_approved comes from verified PayPal evidence, never UI assertion;
      * condition_verified comes from the trusted condition verifier.

    The caller must own this transaction/session (no pending unrelated writes).
    Task 6 later milestones must persist operation attempts and provider IDs.
    """
    if operation not in {"authorize", "capture", "void"}:
        raise PaymentSafetyError("Unsupported payment operation")
    if not isinstance(request_id, str) or not request_id.strip():
        raise PaymentSafetyError("Stable request_id required")
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise PaymentSafetyError("Provider identifier required")

    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise PaymentNotFound(str(payment_id))

    if operation == "authorize" and not buyer_approved:
        raise PaymentSafetyError("Verified buyer approval is required")
    if payment.mode == "SMARTPAY" and operation != "capture":
        raise PaymentSafetyError("SmartPay supports order capture only")
    if payment.mode == "SMARTPAY" and not buyer_approved:
        raise PaymentSafetyError("Verified buyer approval is required")

    context: dict[str, Any] = {"condition_verified": condition_verified}
    if payment.mode == "CONDITIONALPAY" and operation == "capture":
        # A *fresh* read is mandatory, and no capture is attempted on unknown data.
        authorization = await paypal.get_authorization(provider_id)
        context["authorization_valid"] = authorization_capturable(authorization)

    claim_status = claim_status_for(payment.mode, payment.status, operation, context)
    try:
        await _persist_transition(
            session,
            payment,
            new_status=claim_status,
            actor="orchestration",
            evidence={"operation": operation, "request_id": request_id, "phase": "claimed"},
        )
    except StalePaymentVersion:
        await session.rollback()
        raise  # Must never call PayPal after losing a persisted claim.

    try:
        if operation == "authorize":
            result = await paypal.authorize_order(provider_id, request_id=request_id)
        elif operation == "void":
            result = await paypal.void_authorization(provider_id, request_id=request_id)
        elif payment.mode == "SMARTPAY":
            result = await paypal.capture_order(provider_id, request_id=request_id)
        else:
            result = await paypal.capture_authorization(provider_id, request_id=request_id)
    except PayPalOutcomeUnknown:
        await _persist_transition(
            session,
            payment,
            new_status="UNCERTAIN",
            actor="orchestration",
            evidence={"operation": operation, "request_id": request_id, "phase": "unknown"},
        )
        raise  # Reconciliation, not retry, determines eventual result.
    except PayPalNotSent:
        # Provider money endpoint was not contacted. Keep the durable claim;
        # a recovery worker must decide what happens next using the same request ID.
        raise

    if operation == "void":
        if result.get("http_status") != 204:
            raise PaymentSafetyError("Unexpected void result; reconcile provider state")
        new_status = "VOIDED"
    elif operation == "authorize":
        # The response from /orders/{id}/authorize is an order wrapper; do not
        # infer AUTHORIZED without extracting verifiable authorization evidence.
        return {"state": claim_status, "provider_response": result, "needs_reconciliation": True}
    else:
        # An order capture returns a nested capture, an authorization capture
        # returns a direct capture. Unknown shapes must not be marked CAPTURED.
        if result.get("status") == "COMPLETED" and "id" in result:
            new_status = "CAPTURED"
        elif result.get("status") == "PENDING" and "id" in result:
            new_status = "CAPTURE_PENDING"
        else:
            return {
                "state": claim_status,
                "provider_response": result,
                "needs_reconciliation": True,
            }

    await _persist_transition(
        session,
        payment,
        new_status=new_status,
        actor="orchestration",
        evidence={"operation": operation, "request_id": request_id, "phase": "provider_confirmed"},
    )
    return {"state": new_status, "provider_response": result, "needs_reconciliation": False}
