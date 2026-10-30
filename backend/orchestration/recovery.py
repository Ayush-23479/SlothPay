"""Read-only inspection of PayPal attempts requiring operator reconciliation.

This module deliberately does not call PayPal or alter payment state. An attempt
with an unknown outcome must be reconciled using trusted provider evidence.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.attempt import PaymentAttempt
from backend.models.payment import Payment


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
            PaymentAttempt.status.in_(["STARTED", "UNCERTAIN", "NOT_SENT"]),
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
