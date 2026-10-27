"""Concurrency-safe state transition helper.

Provider response handling must reconcile PayPal status independently; this method
only persists a *validated* application-level transition.
"""

import uuid

from sqlalchemy import update
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
