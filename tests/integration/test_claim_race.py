import asyncio
import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config.settings import get_settings
from backend.models import Payment, PaymentAudit
from backend.models.repository import StalePaymentVersion, transition_payment
from backend.state_machine.guards import claim_status_for

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1",
        reason="Opt in to local PostgreSQL integration tests",
    ),
]

PATH = "CONDITIONALPAY"
READY = {"condition_verified": True, "authorization_valid": True}
ROUNDS = 20


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine(get_settings().DATABASE_URL, pool_size=10)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


async def new_waiting_payment(maker) -> tuple[uuid.UUID, int]:
    token = uuid.uuid4().hex
    row = Payment(
        merchant_id="race-test",
        merchant_order_id=f"order-{token}",
        intent_key=f"key-{token}",
        amount=Decimal("500.00"),
        currency="USD",
        mode=PATH,
        status="AWAITING_CONDITION",
        version=3,
    )
    async with maker() as session:
        session.add(row)
        await session.commit()
    return row.id, 3


async def claim(maker, barrier, payment_id, operation) -> tuple[str, str | None]:
    """Read, wait for the other worker, then claim exactly like Task 6 must."""
    async with maker() as session:
        current = (
            await session.execute(select(Payment).where(Payment.id == payment_id))
        ).scalar_one()
        status, version = current.status, current.version
        await barrier.wait()  # both workers now hold the same (status, version)
        try:
            new_status = claim_status_for(PATH, status, operation, READY)
            await transition_payment(
                session,
                payment_id=payment_id,
                expected_version=version,
                expected_status=status,
                new_status=new_status,
                actor=f"worker-{operation}",
                evidence={"operation": operation},
            )
            await session.commit()  # the claim is durable BEFORE any PayPal call
            return "won", new_status
        except StalePaymentVersion:
            await session.rollback()
            return "stale", None  # the loser must not call PayPal


async def test_capture_and_void_cannot_both_win(maker):
    for round_no in range(ROUNDS):
        payment_id, version = await new_waiting_payment(maker)
        barrier = asyncio.Barrier(2)
        order = ("capture", "void") if round_no % 2 == 0 else ("void", "capture")
        results = await asyncio.gather(*(claim(maker, barrier, payment_id, op) for op in order))

        outcomes = sorted(r[0] for r in results)
        assert outcomes == ["stale", "won"], f"round {round_no}: {results}"
        winner = next(r[1] for r in results if r[0] == "won")

        async with maker() as session:
            row = (
                await session.execute(select(Payment).where(Payment.id == payment_id))
            ).scalar_one()
            audits = (
                await session.execute(
                    select(func.count())
                    .select_from(PaymentAudit)
                    .where(
                        PaymentAudit.payment_id == payment_id,
                        PaymentAudit.event_type == "STATE_TRANSITION",
                    )
                )
            ).scalar_one()
        assert row.status == winner
        assert row.version == version + 1
        assert audits == 1, "exactly one persisted claim, so at most one PayPal money call"


async def test_two_capture_requests_cannot_both_claim(maker):
    payment_id, _ = await new_waiting_payment(maker)
    barrier = asyncio.Barrier(2)
    results = await asyncio.gather(
        claim(maker, barrier, payment_id, "capture"), claim(maker, barrier, payment_id, "capture")
    )
    assert sorted(r[0] for r in results) == ["stale", "won"]