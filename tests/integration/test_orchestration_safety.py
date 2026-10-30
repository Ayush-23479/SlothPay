"""Task 6 PostgreSQL safety tests. PayPal is always mocked; no live money calls.

Run only with a dedicated LOCAL test database and SLOTHPAY_RUN_DB_TESTS=1.
"""

import asyncio
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
from backend.models import Payment, PaymentAudit
from backend.models.repository import StalePaymentVersion
from backend.orchestration.service import execute_payment_operation
from backend.payments.paypal_client import PayPalOutcomeUnknown
from backend.state_machine.guards import OperationNotPermitted

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1",
        reason="Opt in to local PostgreSQL integration tests",
    ),
]


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine(get_settings().DATABASE_URL, pool_size=8)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def create_payment(maker, mode="SMARTPAY", status="APPROVED"):
    token = uuid.uuid4().hex
    row = Payment(
        merchant_id="task6-integration-test",
        merchant_order_id=f"order-{token}",
        intent_key=f"intent-{token}",
        amount=Decimal("15.00"),
        currency="USD",
        mode=mode,
        status=status,
        version=0,
    )
    async with maker() as session:
        session.add(row)
        await session.commit()
    return row.id


def fake_gateway():
    return SimpleNamespace(
        capture_order=AsyncMock(return_value={"id": "CAPTURE1", "status": "COMPLETED"}),
        capture_authorization=AsyncMock(
            return_value={"id": "CAPTURE2", "status": "COMPLETED"}
        ),
        void_authorization=AsyncMock(return_value={"http_status": 204}),
        authorize_order=AsyncMock(return_value={"id": "ORDER1", "status": "COMPLETED"}),
        get_authorization=AsyncMock(
            return_value={
                "status": "CREATED",
                "expiration_time": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            }
        ),
    )


async def read_payment(maker, payment_id):
    async with maker() as session:
        return await session.get(Payment, payment_id)


async def test_smartpay_commits_claim_before_gateway_is_invoked(maker):
    payment_id = await create_payment(maker)
    gateway = fake_gateway()
    observed = []

    async def capture(_order_id, *, request_id):
        # Separate DB session can see only committed changes.
        row = await read_payment(maker, payment_id)
        observed.append((row.status, row.version, request_id))
        assert row.status == "CAPTURE_REQUESTED"
        assert row.version == 1
        return {"id": "CAPTURE1", "status": "COMPLETED"}

    gateway.capture_order.side_effect = capture
    async with maker() as session:
        result = await execute_payment_operation(
            session,
            gateway,
            payment_id=payment_id,
            operation="capture",
            provider_id="ORDER1",
            request_id=f"req-{uuid.uuid4().hex}",
            buyer_approved=True,
        )

    assert result["state"] == "CAPTURED"
    assert len(observed) == 1
    final = await read_payment(maker, payment_id)
    assert (final.status, final.version) == ("CAPTURED", 2)
    async with maker() as session:
        count = await session.scalar(
            select(func.count()).select_from(PaymentAudit).where(
                PaymentAudit.payment_id == payment_id,
                PaymentAudit.event_type == "STATE_TRANSITION",
            )
        )
    assert count == 2


async def test_concurrent_duplicate_capture_makes_at_most_one_paypal_call(maker):
    payment_id = await create_payment(maker)
    gateway = fake_gateway()

    async def worker():
        async with maker() as session:
            try:
                return await execute_payment_operation(
                    session,
                    gateway,
                    payment_id=payment_id,
                    operation="capture",
                    provider_id="ORDER1",
                    request_id=f"req-{uuid.uuid4().hex}",
                    buyer_approved=True,
                )
            except (StalePaymentVersion, OperationNotPermitted):
                return "rejected"

    outcomes = await asyncio.gather(worker(), worker())
    assert sum(isinstance(item, dict) for item in outcomes) == 1
    assert outcomes.count("rejected") == 1
    gateway.capture_order.assert_awaited_once()
    assert (await read_payment(maker, payment_id)).status == "CAPTURED"


async def test_expired_conditional_authorization_never_captures(maker):
    payment_id = await create_payment(
        maker, mode="CONDITIONALPAY", status="AWAITING_CONDITION"
    )
    gateway = fake_gateway()
    gateway.get_authorization.return_value = {
        "status": "CREATED",
        "expiration_time": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    }
    async with maker() as session:
        with pytest.raises(OperationNotPermitted):
            await execute_payment_operation(
                session,
                gateway,
                payment_id=payment_id,
                operation="capture",
                provider_id="AUTH1",
                request_id=f"req-{uuid.uuid4().hex}",
                condition_verified=True,
            )
    gateway.get_authorization.assert_awaited_once()
    gateway.capture_authorization.assert_not_awaited()
    assert (await read_payment(maker, payment_id)).status == "AWAITING_CONDITION"


async def test_uncertain_capture_is_persisted_in_database(maker):
    payment_id = await create_payment(maker)
    gateway = fake_gateway()
    gateway.capture_order.side_effect = PayPalOutcomeUnknown("simulated network timeout")
    async with maker() as session:
        with pytest.raises(PayPalOutcomeUnknown):
            await execute_payment_operation(
                session,
                gateway,
                payment_id=payment_id,
                operation="capture",
                provider_id="ORDER1",
                request_id=f"req-{uuid.uuid4().hex}",
                buyer_approved=True,
            )
    gateway.capture_order.assert_awaited_once()
    assert (await read_payment(maker, payment_id)).status == "UNCERTAIN"
