"""PostgreSQL integration tests for Task 6 lifecycle (PayPal is mocked).

Only run against a disposable LOCAL test database. Opt in with
SLOTHPAY_RUN_DB_TESTS=1. These tests commit rows and do not clean them up.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config.settings import get_settings
from backend.models.attempt import PaymentAttempt
from backend.models.payment import Payment
from backend.orchestration.lifecycle import (
    LifecycleSafetyError,
    OperationAlreadyStarted,
    create_order,
    execute_operation,
    stable_operation_request_id,
    verify_buyer_approval,
)
from backend.payments.paypal_client import PayPalNotSent, PayPalOutcomeUnknown

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1",
        reason="Opt in to disposable LOCAL PostgreSQL integration tests",
    ),
]


@pytest_asyncio.fixture
async def maker():
    url = get_settings().DATABASE_URL
    if "localhost" not in url and "127.0.0.1" not in url:
        pytest.skip("Refusing to run committed integration tests against a remote database")
    engine = create_async_engine(url, pool_size=5)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def seed(maker, *, mode="SMARTPAY", status="INTENT_CREATED", order_id=None):
    token = uuid.uuid4().hex
    row = Payment(
        merchant_id="task6-lifecycle-db-tests",
        merchant_order_id=f"order-{token}",
        intent_key=f"intent-{token}",
        amount=Decimal("15.00"),
        currency="USD",
        mode=mode,
        status=status,
        version=0,
        paypal_order_id=order_id,
    )
    async with maker() as session:
        session.add(row)
        await session.commit()
    return row.id


def gateway():
    return SimpleNamespace(
        create_order=AsyncMock(return_value={"id": "ORDER-SANDBOX-1", "status": "CREATED"}),
        get_order=AsyncMock(return_value={"id": "ORDER-SANDBOX-1", "status": "APPROVED"}),
        authorize_order=AsyncMock(
            return_value={
                "purchase_units": [
                    {"payments": {"authorizations": [{"id": "AUTH-1", "status": "CREATED"}]}}
                ]
            }
        ),
        capture_order=AsyncMock(
            return_value={
                "purchase_units": [
                    {"payments": {"captures": [{"id": "CAP-1", "status": "COMPLETED"}]}}
                ]
            }
        ),
        capture_authorization=AsyncMock(return_value={"id": "CAP-2", "status": "COMPLETED"}),
        get_authorization=AsyncMock(
            return_value={
                "status": "CREATED",
                "expiration_time": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            }
        ),
        void_authorization=AsyncMock(return_value={"http_status": 204}),
    )


async def test_order_creation_persists_payment_and_attempt_before_return(maker):
    payment_id = await seed(maker)
    paypal = gateway()

    async def create_at_provider(*, intent, amount, currency, request_id):
        assert (intent, amount, currency) == ("CAPTURE", "15.00", "USD")
        async with maker() as independent:
            attempt = await independent.scalar(
                select(PaymentAttempt).where(
                    PaymentAttempt.payment_id == payment_id,
                    PaymentAttempt.operation == "create_order",
                )
            )
            assert attempt is not None
            assert attempt.status == "STARTED"
            assert attempt.request_id == request_id
        return {"id": f"ORDER-{uuid.uuid4().hex}", "status": "CREATED"}

    paypal.create_order.side_effect = create_at_provider
    async with maker() as session:
        result = await create_order(session, paypal, payment_id=payment_id)
    assert result["state"] == "ORDER_CREATED"
    async with maker() as session:
        payment = await session.get(Payment, payment_id)
        attempt = await session.scalar(
            select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id)
        )
        assert payment.paypal_order_id == result["paypal_order_id"]
        assert payment.status == "ORDER_CREATED"
        assert attempt.status == "SUCCEEDED"
        assert attempt.provider_operation_id == result["paypal_order_id"]
        assert attempt.request_id == stable_operation_request_id(payment_id, "create_order")


async def test_verified_buyer_approval_persists_state(maker):
    order_id = f"ORDER-{uuid.uuid4().hex}"
    payment_id = await seed(maker, status="ORDER_CREATED", order_id=order_id)
    paypal = gateway()
    paypal.get_order.return_value = {"id": order_id, "status": "APPROVED"}
    async with maker() as session:
        result = await verify_buyer_approval(session, paypal, payment_id=payment_id)
    assert result["state"] == "APPROVED"
    paypal.get_order.assert_awaited_once_with(order_id)
    async with maker() as session:
        assert (await session.get(Payment, payment_id)).status == "APPROVED"


async def test_unapproved_buyer_cannot_be_captured(maker):
    order_id = f"ORDER-{uuid.uuid4().hex}"
    payment_id = await seed(maker, status="ORDER_CREATED", order_id=order_id)
    paypal = gateway()
    paypal.get_order.return_value = {"id": order_id, "status": "CREATED"}
    async with maker() as session:
        result = await verify_buyer_approval(session, paypal, payment_id=payment_id)
    assert result["state"] == "AWAITING_APPROVAL"
    async with maker() as session:
        with pytest.raises(LifecycleSafetyError):
            await execute_operation(session, paypal, payment_id=payment_id, operation="capture")
    paypal.capture_order.assert_not_awaited()


async def test_order_not_sent_does_not_allow_duplicate_outbound_post(maker):
    payment_id = await seed(maker)
    paypal = gateway()
    paypal.create_order.side_effect = PayPalNotSent("OAuth failed before the operation")
    async with maker() as session:
        with pytest.raises(PayPalNotSent):
            await create_order(session, paypal, payment_id=payment_id)
    async with maker() as session:
        with pytest.raises(OperationAlreadyStarted):
            await create_order(session, paypal, payment_id=payment_id)
    paypal.create_order.assert_awaited_once()
    async with maker() as session:
        attempt = await session.scalar(
            select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id)
        )
        assert attempt.status == "NOT_SENT"


async def test_smartpay_capture_has_committed_claim_and_attempt_before_call(maker):
    order_id = f"ORDER-{uuid.uuid4().hex}"
    payment_id = await seed(maker, status="APPROVED", order_id=order_id)
    paypal = gateway()

    async def capture_at_provider(passed_order_id, *, request_id):
        assert passed_order_id == order_id
        async with maker() as independent:
            payment = await independent.get(Payment, payment_id)
            attempt = await independent.scalar(
                select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id)
            )
            assert payment.status == "CAPTURE_REQUESTED"
            assert attempt.status == "STARTED"
            assert attempt.request_id == request_id
        return {
            "purchase_units": [{"payments": {"captures": [{"id": "CAP-1", "status": "COMPLETED"}]}}]
        }

    paypal.capture_order.side_effect = capture_at_provider
    async with maker() as session:
        result = await execute_operation(
            session, paypal, payment_id=payment_id, operation="capture"
        )
    assert result["state"] == "CAPTURED"
    async with maker() as session:
        payment = await session.get(Payment, payment_id)
        attempt = await session.scalar(
            select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id)
        )
        assert payment.status == "CAPTURED"
        assert attempt.status == "SUCCEEDED"
        assert attempt.provider_operation_id == "CAP-1"


async def test_unknown_capture_outcome_is_persisted_and_not_replayed(maker):
    payment_id = await seed(maker, status="APPROVED", order_id=f"ORDER-{uuid.uuid4().hex}")
    paypal = gateway()
    paypal.capture_order.side_effect = PayPalOutcomeUnknown("simulated timeout")
    async with maker() as session:
        with pytest.raises(PayPalOutcomeUnknown):
            await execute_operation(session, paypal, payment_id=payment_id, operation="capture")
    async with maker() as session:
        payment = await session.get(Payment, payment_id)
        attempt = await session.scalar(
            select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id)
        )
        assert payment.status == "UNCERTAIN"
        assert attempt.status == "UNCERTAIN"
        count = await session.scalar(
            select(func.count())
            .select_from(PaymentAttempt)
            .where(PaymentAttempt.payment_id == payment_id)
        )
        assert count == 1
    async with maker() as session:
        with pytest.raises(LifecycleSafetyError):
            await execute_operation(session, paypal, payment_id=payment_id, operation="capture")
    paypal.capture_order.assert_awaited_once()


async def test_conditional_authorization_persists_authorization_id(maker):
    order_id = f"ORDER-{uuid.uuid4().hex}"
    payment_id = await seed(maker, mode="CONDITIONALPAY", status="APPROVED", order_id=order_id)
    paypal = gateway()
    async with maker() as session:
        result = await execute_operation(
            session, paypal, payment_id=payment_id, operation="authorize"
        )
    assert result["state"] == "AWAITING_CONDITION"
    paypal.authorize_order.assert_awaited_once()
    async with maker() as session:
        payment = await session.get(Payment, payment_id)
        attempt = await session.scalar(
            select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id)
        )
        assert payment.status == "AWAITING_CONDITION"
        assert attempt.operation == "authorize"
        assert attempt.provider_operation_id == "AUTH-1"
        assert attempt.status == "SUCCEEDED"
