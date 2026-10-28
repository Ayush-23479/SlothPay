"""Task 7 processing acceptance: disposable PostgreSQL, no PayPal POSTs."""

import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config.settings import get_settings
from backend.models.attempt import PaymentAttempt
from backend.models.event import ProviderEvent
from backend.models.payment import Payment
from backend.models.audit import PaymentAudit
from backend.webhooks.processing import process_webhook_event

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1", reason="opt in to local PostgreSQL tests"
    ),
]


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine(get_settings().DATABASE_URL)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def seed(maker, *, status="CAPTURE_REQUESTED", amount="15.00"):
    suffix = uuid.uuid4().hex
    event_id, capture_id, order_id = "WH" + suffix, "CAP" + suffix, "ORDER" + suffix
    payment = Payment(
        merchant_id="task7-processing-test",
        merchant_order_id="order" + suffix,
        intent_key="intent" + suffix,
        amount=Decimal("15.00"),
        currency="USD",
        mode="SMARTPAY",
        status=status,
        version=0,
        paypal_order_id=order_id,
    )
    async with maker() as session:
        session.add(payment)
        await session.flush()
        session.add(
            PaymentAttempt(
                payment_id=payment.id,
                provider="paypal",
                operation="capture",
                request_id="REQ" + suffix,
                status="SUCCEEDED",
                provider_operation_id=capture_id,
            )
        )
        session.add(
            ProviderEvent(
                provider="paypal",
                event_id=event_id,
                event_type="PAYMENT.CAPTURE.COMPLETED",
                verified=True,
                payload={
                    "id": event_id,
                    "event_type": "PAYMENT.CAPTURE.COMPLETED",
                    "resource": {
                        "id": capture_id,
                        "status": "COMPLETED",
                        "amount": {"currency_code": "USD", "value": amount},
                        "supplementary_data": {"related_ids": {"order_id": order_id}},
                    },
                },
            )
        )
        await session.commit()
    return payment.id, event_id


async def test_verified_completed_capture_changes_state_and_writes_audit(maker):
    payment_id, event_id = await seed(maker)
    async with maker() as session:
        result = await process_webhook_event(session, event_id=event_id)
        assert result.disposition == "applied"
        await session.commit()
    async with maker() as session:
        payment = await session.get(Payment, payment_id)
        assert (payment.status, payment.version) == ("CAPTURED", 1)
        audits = (
            await session.scalars(
                select(PaymentAudit).where(
                    PaymentAudit.payment_id == payment_id, PaymentAudit.actor == "paypal_webhook"
                )
            )
        ).all()
        assert len(audits) == 1


async def test_duplicate_is_idempotent(maker):
    payment_id, event_id = await seed(maker)
    for expected in ("applied", "already_processed"):
        async with maker() as session:
            assert (await process_webhook_event(session, event_id=event_id)).disposition == expected
            await session.commit()
    async with maker() as session:
        assert (await session.get(Payment, payment_id)).version == 1


async def test_amount_mismatch_fails_closed(maker):
    payment_id, event_id = await seed(maker, amount="14.99")
    async with maker() as session:
        result = await process_webhook_event(session, event_id=event_id)
        assert result.disposition == "needs_review"
        await session.commit()
    async with maker() as session:
        assert (await session.get(Payment, payment_id)).status == "CAPTURE_REQUESTED"


async def test_out_of_order_state_fails_closed(maker):
    payment_id, event_id = await seed(maker, status="APPROVED")
    async with maker() as session:
        result = await process_webhook_event(session, event_id=event_id)
        assert result.disposition == "needs_review"
        await session.commit()
    async with maker() as session:
        assert (await session.get(Payment, payment_id)).status == "APPROVED"


async def test_unverified_event_fails_closed(maker):
    _, event_id = await seed(maker)
    async with maker() as session:
        event = await session.scalar(
            select(ProviderEvent).where(ProviderEvent.event_id == event_id)
        )
        event.verified = False
        await session.commit()
    async with maker() as session:
        assert (await process_webhook_event(session, event_id=event_id)).disposition == "unverified"
        await session.commit()
