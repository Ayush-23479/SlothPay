"""Payment repository: concurrency-safe state transitions and intent read/write helpers.

Provider response handling must reconcile PayPal status independently; transition_payment
only persists a *validated* application-level transition. The intent helpers below are the
only place Task 5 (duplicate protection) touches the payments table, so nobody else needs
raw database commands.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .audit import PaymentAudit
from .payment import Payment


class StalePaymentVersion(Exception):
    """Payment was modified by another worker; reconcile and retry read."""


async def transition_payment(
    session: AsyncSession,
    *,
    payment_id: uuid.UUID,
    expected_version: int,
    expected_status: str,
    new_status: str,
    actor: str,
    evidence: dict,
) -> int:
    """CAS update plus audit INSERT, atomically committed by caller.

    Caller MUST check PayPal evidence and permitted transitions before calling.
    """
    result = await session.execute(
        update(Payment)
        .where(
            Payment.id == payment_id,
            Payment.version == expected_version,
            Payment.status == expected_status,
        )
        .values(status=new_status, version=Payment.version + 1)
        .returning(Payment.version)
    )
    version = result.scalar_one_or_none()
    if version is None:
        raise StalePaymentVersion("Stale payment state/version; reconcile before proceeding")
    session.add(
        PaymentAudit(
            payment_id=payment_id,
            event_type="STATE_TRANSITION",
            from_state=expected_status,
            to_state=new_status,
            actor=actor,
            evidence=evidence,
        )
    )
    await session.flush()
    return version


# --- intent helpers (Task 5) -------------------------------------------------------------
# A "payment intent" is one row of `payments`. These helpers return plain dicts so callers
# never hold ORM objects. Column names are mapped to the names used across the project:
#   merchant_id -> owner_id, intent_key -> idempotency_key, mode -> path.


def intent_to_dict(payment: Payment) -> dict[str, Any]:
    return {
        "id": str(payment.id),
        "owner_id": payment.merchant_id,
        "idempotency_key": payment.intent_key,
        "merchant_order_id": payment.merchant_order_id,
        "path": payment.mode,
        "amount": payment.amount,
        "currency": payment.currency,
        "status": payment.status,
        "version": payment.version,
        "created_at": payment.created_at,
    }


async def get_intent_by_key(
    session: AsyncSession, owner_id: str, idempotency_key: str
) -> dict[str, Any] | None:
    result = await session.execute(
        select(Payment)
        .where(Payment.merchant_id == owner_id, Payment.intent_key == idempotency_key)
        .execution_options(populate_existing=True)
    )
    payment = result.scalar_one_or_none()
    return intent_to_dict(payment) if payment is not None else None


async def create_intent(
    session: AsyncSession,
    *,
    owner_id: str,
    idempotency_key: str,
    merchant_order_id: str,
    path: str,
    amount: Decimal,
    currency: str,
) -> dict[str, Any]:
    """Insert one payment. Raises sqlalchemy IntegrityError on a duplicate key/order id."""
    payment = Payment(
        merchant_id=owner_id,
        merchant_order_id=merchant_order_id,
        intent_key=idempotency_key,
        amount=amount,
        currency=currency,
        mode=path,
        status="CREATED",
        version=0,
    )
    session.add(payment)
    await session.flush()
    # Load server-generated columns (created_at) now; async sessions cannot lazy-load later.
    await session.refresh(payment)
    return intent_to_dict(payment)


async def find_recent_intents(
    session: AsyncSession,
    *,
    owner_id: str,
    amount: Decimal,
    currency: str,
    since: datetime,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Newest-first payments for this owner, amount and currency created at or after `since`."""
    result = await session.execute(
        select(Payment)
        .where(
            Payment.merchant_id == owner_id,
            Payment.amount == amount,
            Payment.currency == currency,
            Payment.created_at >= since,
        )
        .order_by(Payment.created_at.desc(), Payment.id.desc())
        .limit(limit)
        .execution_options(populate_existing=True)
    )
    return [intent_to_dict(payment) for payment in result.scalars().all()]


async def add_audit_event(
    session: AsyncSession,
    *,
    payment_id: str | uuid.UUID,
    event_type: str,
    actor: str,
    evidence: dict[str, Any],
    from_state: str | None = None,
    to_state: str | None = None,
) -> None:
    """Append one audit row (the database blocks any later UPDATE or DELETE)."""
    session.add(
        PaymentAudit(
            payment_id=uuid.UUID(str(payment_id)),
            event_type=event_type,
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            evidence=evidence,
        )
    )
    await session.flush()