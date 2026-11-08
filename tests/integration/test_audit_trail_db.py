import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from backend.models import Payment, PaymentAudit

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1",
        reason="Opt in to local PostgreSQL integration tests",
    ),
]


async def one_audit_row(session):
    payment = Payment(
        merchant_id="audit-owner",
        merchant_order_id=f"order-{uuid.uuid4().hex}",
        intent_key=f"key-{uuid.uuid4().hex}",
        amount=Decimal("10.00"),
        currency="USD",
        mode="SMARTPAY",
        status="CREATED",
        version=0,
    )
    session.add(payment)
    await session.flush()
    audit = PaymentAudit(payment_id=payment.id, event_type="TEST", actor="test", evidence={})
    session.add(audit)
    await session.flush()
    return audit


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE payment_audit SET actor = 'tampered'",
        "DELETE FROM payment_audit",
        "TRUNCATE payment_audit",
        "TRUNCATE payments CASCADE",
    ],
)
async def test_updating_deleting_or_truncating_the_audit_trail_is_refused(
    isolated_maker, statement
):
    async with isolated_maker() as session:
        await one_audit_row(session)
        await session.commit()

    async with isolated_maker() as session:
        with pytest.raises(DBAPIError):
            await session.execute(text(statement))
        await session.rollback()

    async with isolated_maker() as session:
        count = await session.scalar(select(func.count(PaymentAudit.id)))
        actor = await session.scalar(select(PaymentAudit.actor))
    assert count == 1 and actor == "test"