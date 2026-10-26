"""Real PostgreSQL integration tests. Enable with SLOTHPAY_RUN_DB_TESTS=1.

Requires migrated disposable local database; tests roll back their own inserts.
"""

import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config.settings import get_settings
from backend.models import Payment, PaymentAudit
from backend.models.repository import StalePaymentVersion, transition_payment

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1", reason="Opt in to local PostgreSQL integration tests"),
]


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(get_settings().DATABASE_URL)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        maker = async_sessionmaker(bind=connection, expire_on_commit=False)
        async with maker() as session:
            yield session
        await transaction.rollback()
    await engine.dispose()


def payment(merchant_order_id: str, intent_key: str) -> Payment:
    return Payment(
        merchant_id="test-merchant",
        merchant_order_id=merchant_order_id,
        intent_key=intent_key,
        amount=Decimal("500.00"),
        currency="USD",
        mode="SMARTPAY",
        status="CREATED",
        version=0,
    )


async def test_duplicate_intent_rejected(db_session):
    db_session.add(payment("order-one", "same-key"))
    await db_session.flush()
    # Savepoint lets us assert duplicate rejection without invalidating outer transaction.
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(payment("order-two", "same-key"))
            await db_session.flush()


async def test_stale_version_rejected(db_session):
    p = payment(str(uuid.uuid4()), str(uuid.uuid4()))
    db_session.add(p)
    await db_session.flush()
    await transition_payment(
        db_session, payment_id=p.id, expected_version=0,
        expected_status="CREATED", new_status="APPROVED",
        actor="test", evidence={"source": "test"},
    )
    with pytest.raises(StalePaymentVersion):
        await transition_payment(
            db_session, payment_id=p.id, expected_version=0,
            expected_status="CREATED", new_status="APPROVED",
            actor="test", evidence={"source": "test"},
        )


async def test_audit_update_delete_blocked(db_session):
    p = payment(str(uuid.uuid4()), str(uuid.uuid4()))
    db_session.add(p)
    await db_session.flush()
    audit = PaymentAudit(
        payment_id=p.id, event_type="TEST", actor="test", evidence={"test": True}
    )
    db_session.add(audit)
    await db_session.flush()
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE payment_audit SET actor='tampered' WHERE id=:id"),
                {"id": audit.id},
            )
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await db_session.execute(
                text("DELETE FROM payment_audit WHERE id=:id"), {"id": audit.id}
            )
