"""Conservative, transactional processing of verified PayPal webhook inbox events."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.attempt import PaymentAttempt
from backend.models.event import ProviderEvent
from backend.models.payment import Payment
from backend.models.repository import StalePaymentVersion, transition_payment
from backend.state_machine.transitions import validate_transition

SUPPORTED = frozenset({"PAYMENT.CAPTURE.COMPLETED"})


@dataclass(frozen=True)
class ProcessingResult:
    disposition: str
    event_id: str
    payment_id: str | None = None
    reason: str | None = None


def extract_capture_id(payload: Any) -> str | None:
    if not isinstance(payload, dict) or payload.get("event_type") not in SUPPORTED:
        return None
    resource = payload.get("resource")
    if not isinstance(resource, dict):
        return None
    capture_id = resource.get("id")
    if not isinstance(capture_id, str) or not 1 <= len(capture_id) <= 128:
        return None
    if resource.get("status") != "COMPLETED":
        return None
    return capture_id


async def process_webhook_event(session: AsyncSession, *, event_id: str) -> ProcessingResult:
    """Process a committed verified event once; never make provider requests."""
    event = await session.scalar(
        select(ProviderEvent)
        .where(ProviderEvent.provider == "paypal", ProviderEvent.event_id == event_id)
        .with_for_update()
    )
    if event is None:
        return ProcessingResult("missing", event_id)
    if not event.verified:
        return ProcessingResult("unverified", event_id)

    previous = await session.execute(
        text(
            "SELECT disposition, payment_id FROM webhook_processing "
            "WHERE provider_event_id = :id"
        ),
        {"id": event.id},
    )
    existing = previous.first()
    if existing:
        return ProcessingResult(
            "already_processed",
            event_id,
            str(existing.payment_id) if existing.payment_id else None,
        )

    disposition = "needs_review"
    reason = "unsupported_or_ambiguous_event"
    payment_id = None
    capture_id = extract_capture_id(event.payload)

    if event.event_type in SUPPORTED and capture_id:
        attempts = (
            await session.scalars(
                select(PaymentAttempt).where(
                    PaymentAttempt.provider == "paypal",
                    PaymentAttempt.operation == "capture",
                    PaymentAttempt.provider_operation_id == capture_id,
                    PaymentAttempt.status == "SUCCEEDED",
                )
            )
        ).all()
        if len(attempts) == 1:
            attempt = attempts[0]
            payment = await session.get(Payment, attempt.payment_id, with_for_update=True)
            resource = event.payload.get("resource", {})
            amount = resource.get("amount")
            related = resource.get("supplementary_data")
            related = related.get("related_ids") if isinstance(related, dict) else None
            webhook_order = related.get("order_id") if isinstance(related, dict) else None
            currency = amount.get("currency_code") if isinstance(amount, dict) else None
            value = amount.get("value") if isinstance(amount, dict) else None
            try:
                amount_matches = (
                    isinstance(value, str)
                    and Decimal(value) == payment.amount
                    and currency == payment.currency
                    if payment is not None
                    else False
                )
            except (InvalidOperation, ValueError):
                amount_matches = False
            if (
                payment is not None
                and payment.paypal_order_id
                and webhook_order == payment.paypal_order_id
                and amount_matches
            ):
                payment_id = payment.id
                if payment.status == "CAPTURED":
                    disposition, reason = "already_final", "payment_already_captured"
                elif payment.status in {"CAPTURE_REQUESTED", "CAPTURE_PENDING"}:
                    allowed, _ = validate_transition(payment.mode, payment.status, "CAPTURED")
                    if allowed:
                        try:
                            await transition_payment(
                                session,
                                payment_id=payment.id,
                                expected_version=payment.version,
                                expected_status=payment.status,
                                new_status="CAPTURED",
                                actor="paypal_webhook",
                                evidence={"event_id": event_id, "capture_id": capture_id},
                            )
                            disposition, reason = "applied", "capture_confirmed"
                        except StalePaymentVersion:
                            disposition, reason = "needs_review", "concurrent_payment_update"
                else:
                    reason = "out_of_order_or_incompatible_payment_state"
            else:
                reason = "provider_identity_or_amount_mismatch"
        else:
            reason = "capture_attempt_not_uniquely_matched"

    await session.execute(
        text(
            """
            INSERT INTO webhook_processing
              (provider_event_id, disposition, payment_id, reason)
            VALUES (:id, :disposition, :payment_id, :reason)
            ON CONFLICT (provider_event_id) DO NOTHING
            """
        ),
        {"id": event.id, "disposition": disposition, "payment_id": payment_id, "reason": reason},
    )
    if payment_id is not None:
        event.payment_id = payment_id
    return ProcessingResult(
        disposition, event_id, str(payment_id) if payment_id else None, reason
    )
