"""Conservative, read-only PayPal evidence gathering for unresolved Task 6 attempts.

Never POSTs or replays a payment. Provider reads alone do not mutate the ledger.
A separate reviewed reconciliation decision must use compare-and-swap plus audit.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.attempt import PaymentAttempt
from backend.models.payment import Payment
from backend.payments.paypal_client import PayPalError, PayPalNotSent, PayPalOutcomeUnknown

UNRESOLVED = frozenset({"STARTED", "UNCERTAIN", "NOT_SENT"})


@dataclass(frozen=True)
class ReconciliationObservation:
    payment_id: str
    attempt_id: str
    operation: str
    request_id: str
    attempt_status: str
    payment_status: str
    provider_status: str | None
    provider_resource_id: str | None
    decision: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _one_resource(order: dict[str, Any], kind: str) -> dict[str, Any] | None:
    units = order.get("purchase_units")
    if not isinstance(units, list) or len(units) != 1:
        return None
    unit = units[0]
    if not isinstance(unit, dict):
        return None
    payments = unit.get("payments")
    if not isinstance(payments, dict):
        return None
    resources = payments.get(kind)
    if not isinstance(resources, list) or len(resources) != 1:
        return None
    resource = resources[0]
    if not isinstance(resource, dict):
        return None
    return resource


def classify_provider_observation(
    *,
    operation: str,
    payment_mode: str,
    order: dict[str, Any] | None,
    authorization: dict[str, Any] | None = None,
) -> tuple[str | None, str | None, str, str]:
    """Classify *positive* provider evidence; lack of evidence means manual review.

    A completed order by itself does not prove a particular capture/authorization.
    Never infer nonexecution from a 404, APPROVED or absent nested resource.
    """
    if operation == "create_order":
        if order and order.get("status") in {
            "CREATED",
            "PAYER_ACTION_REQUIRED",
            "APPROVED",
            "COMPLETED",
        }:
            return (
                str(order["status"]),
                str(order["id"]),
                "EVIDENCE_FOUND",
                "Stored order exists at PayPal",
            )
        return None, None, "MANUAL_REVIEW", "Order creation has no definitive lookup evidence"

    if operation == "authorize":
        resource = _one_resource(order or {}, "authorizations")
        if resource and resource.get("status") in {
            "CREATED",
            "CAPTURED",
            "PARTIALLY_CAPTURED",
            "VOIDED",
        }:
            rid = resource.get("id")
            if isinstance(rid, str) and rid:
                return (
                    str(resource["status"]),
                    rid,
                    "EVIDENCE_FOUND",
                    "Single authorization linked to stored order",
                )
        return (
            None,
            None,
            "MANUAL_REVIEW",
            "Authorization result cannot be independently established",
        )

    if operation == "capture":
        resource = _one_resource(order or {}, "captures")
        if resource and resource.get("status") in {"COMPLETED", "PENDING", "DECLINED", "DENIED"}:
            rid = resource.get("id")
            if isinstance(rid, str) and rid:
                return (
                    str(resource["status"]),
                    rid,
                    "EVIDENCE_FOUND",
                    "Single capture linked to stored order",
                )
        return None, None, "MANUAL_REVIEW", "Capture outcome not proven by GET order"

    if operation == "void" and payment_mode == "CONDITIONALPAY":
        if authorization and authorization.get("status") == "VOIDED":
            rid = authorization.get("id")
            if isinstance(rid, str) and rid:
                return "VOIDED", rid, "EVIDENCE_FOUND", "Authorization is confirmed voided"
        return None, None, "MANUAL_REVIEW", "Void outcome has no definitive provider evidence"

    return None, None, "MANUAL_REVIEW", "Unsupported observation"


async def inspect_provider_evidence(
    session: AsyncSession,
    paypal: Any,
    *,
    payment_id: uuid.UUID,
) -> list[ReconciliationObservation]:
    """Perform safe GETs, never payment POSTs or database writes.

    Designed for offline/admin inspection. Follow-up state updates require an
    independently reviewed reconciliation policy, CAS transition and audit.
    """
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise ValueError("Payment does not exist")
    rows = (
        (
            await session.execute(
                select(PaymentAttempt)
                .where(
                    PaymentAttempt.payment_id == payment_id, PaymentAttempt.status.in_(UNRESOLVED)
                )
                .order_by(PaymentAttempt.started_at, PaymentAttempt.id)
            )
        )
        .scalars()
        .all()
    )

    order: dict[str, Any] | None = None
    lookup_issue = None
    if payment.paypal_order_id:
        try:
            order = await paypal.get_order(payment.paypal_order_id)
            if not isinstance(order, dict) or order.get("id") != payment.paypal_order_id:
                order = None
                lookup_issue = "Order ID mismatch or malformed PayPal response"
        except (PayPalError, PayPalNotSent, PayPalOutcomeUnknown):
            lookup_issue = "PayPal order lookup failed; outcome unknown"
    else:
        lookup_issue = "No stored PayPal order ID; request ID is not a lookup key"

    observations: list[ReconciliationObservation] = []
    for attempt in rows:
        auth = None
        if attempt.operation == "void":
            authorization_row = await session.scalar(
                select(PaymentAttempt).where(
                    PaymentAttempt.payment_id == payment_id,
                    PaymentAttempt.operation == "authorize",
                    PaymentAttempt.status == "SUCCEEDED",
                )
            )
            if authorization_row and authorization_row.provider_operation_id:
                try:
                    auth = await paypal.get_authorization(authorization_row.provider_operation_id)
                    if (
                        not isinstance(auth, dict)
                        or auth.get("id") != authorization_row.provider_operation_id
                    ):
                        auth = None
                except (PayPalError, PayPalNotSent, PayPalOutcomeUnknown):
                    auth = None
        status, rid, decision, reason = classify_provider_observation(
            operation=attempt.operation,
            payment_mode=payment.mode,
            order=order,
            authorization=auth,
        )
        if decision != "EVIDENCE_FOUND" and lookup_issue:
            reason = lookup_issue + "; " + reason
        observations.append(
            ReconciliationObservation(
                payment_id=str(payment.id),
                attempt_id=str(attempt.id),
                operation=attempt.operation,
                request_id=attempt.request_id,
                attempt_status=attempt.status,
                payment_status=payment.status,
                provider_status=status,
                provider_resource_id=rid,
                decision=decision,
                reason=reason,
            )
        )
    return observations
