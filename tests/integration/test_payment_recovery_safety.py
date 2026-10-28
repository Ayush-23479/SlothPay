"""Crash and concurrency safety tests using a disposable LOCAL PostgreSQL DB.

These tests commit records and do not remove them. PayPal is mocked.
"""

import asyncio
import os
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
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
)
from backend.orchestration.recovery import inspect_unresolved_attempts

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1",
        reason="Set SLOTHPAY_RUN_DB_TESTS=1 for local PostgreSQL tests",
    ),
]


@pytest_asyncio.fixture
async def maker():
    url = get_settings().DATABASE_URL
    if "localhost" not in url and "127.0.0.1" not in url:
        pytest.skip("Refusing to commit test rows to a nonlocal database")
    engine = create_async_engine(url, pool_size=8)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def seed(maker, *, status="INTENT_CREATED", order_id=None):
    token = uuid.uuid4().hex
    payment = Payment(
        merchant_id="task6-recovery-safety-test",
        merchant_order_id=f"order-{token}",
        intent_key=f"intent-{token}",
        amount=Decimal("10.00"),
        currency="USD",
        mode="SMARTPAY",
        status=status,
        version=0,
        paypal_order_id=order_id,
    )
    async with maker() as session:
        session.add(payment)
        await session.commit()
    return payment.id


def gateway():
    return SimpleNamespace(
        create_order=AsyncMock(
            return_value={"id": f"ORDER-{uuid.uuid4().hex}", "status": "CREATED"}
        ),
        capture_order=AsyncMock(
            return_value={
                "purchase_units": [
                    {"payments": {"captures": [{"id": "CAP-1", "status": "COMPLETED"}]}}
                ]
            }
        ),
    )


async def test_concurrent_order_creation_issues_only_one_paypal_call(maker):
    payment_id = await seed(maker)
    paypal = gateway()

    async def worker():
        async with maker() as session:
            try:
                return await create_order(session, paypal, payment_id=payment_id)
            except (OperationAlreadyStarted, LifecycleSafetyError):
                return "blocked"

    results = await asyncio.gather(worker(), worker())
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("blocked") == 1
    paypal.create_order.assert_awaited_once()
    async with maker() as session:
        attempts = (
            await session.scalars(
                select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id)
            )
        ).all()
        assert len(attempts) == 1
        assert attempts[0].status == "SUCCEEDED"


async def test_committed_started_order_attempt_is_not_replayed(maker):
    payment_id = await seed(maker)
    request_id = stable_operation_request_id(payment_id, "create_order")
    async with maker() as session:
        session.add(
            PaymentAttempt(
                payment_id=payment_id,
                provider="paypal",
                operation="create_order",
                request_id=request_id,
                status="STARTED",
            )
        )
        await session.commit()

    paypal = gateway()
    async with maker() as session:
        with pytest.raises(OperationAlreadyStarted):
            await create_order(session, paypal, payment_id=payment_id)
    paypal.create_order.assert_not_awaited()

    async with maker() as session:
        pending = await inspect_unresolved_attempts(session, payment_id=payment_id)
    assert len(pending) == 1
    assert pending[0].attempt_status == "STARTED"
    assert pending[0].request_id == request_id
    assert pending[0].payment_status == "INTENT_CREATED"


async def test_committed_started_capture_claim_is_not_replayed(maker):
    order_id = f"ORDER-{uuid.uuid4().hex}"
    payment_id = await seed(maker, status="CAPTURE_REQUESTED", order_id=order_id)
    request_id = stable_operation_request_id(payment_id, "capture")
    async with maker() as session:
        session.add(
            PaymentAttempt(
                payment_id=payment_id,
                provider="paypal",
                operation="capture",
                request_id=request_id,
                status="STARTED",
            )
        )
        await session.commit()

    paypal = gateway()
    async with maker() as session:
        with pytest.raises(LifecycleSafetyError):
            await execute_operation(session, paypal, payment_id=payment_id, operation="capture")
    paypal.capture_order.assert_not_awaited()

    async with maker() as session:
        pending = await inspect_unresolved_attempts(session, payment_id=payment_id)
    assert len(pending) == 1
    assert pending[0].operation == "capture"
    assert pending[0].attempt_status == "STARTED"
    assert pending[0].payment_status == "CAPTURE_REQUESTED"


async def test_inspection_does_not_modify_payment_or_attempt(maker):
    payment_id = await seed(maker)
    request_id = stable_operation_request_id(payment_id, "create_order")
    async with maker() as session:
        session.add(
            PaymentAttempt(
                payment_id=payment_id,
                provider="paypal",
                operation="create_order",
                request_id=request_id,
                status="UNCERTAIN",
            )
        )
        await session.commit()

    async with maker() as session:
        pending = await inspect_unresolved_attempts(session, payment_id=payment_id)
        await session.rollback()
    assert len(pending) == 1
    assert pending[0].attempt_status == "UNCERTAIN"
    async with maker() as session:
        attempt = await session.scalar(
            select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id)
        )
        payment = await session.get(Payment, payment_id)
    assert attempt.status == "UNCERTAIN"
    assert payment.status == "INTENT_CREATED"
