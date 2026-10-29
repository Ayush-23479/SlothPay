"""Duplicate protection against REAL PostgreSQL and Redis. Enable with SLOTHPAY_RUN_DB_TESTS=1.

Needs the Compose services running and a migrated database (alembic upgrade head).
Each test uses a fresh random owner id. The tests commit for real (that is the point), and
payment_audit is append-only, so the rows they create are left in the disposable local
database.
"""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config.settings import get_settings
from backend.reliability import idempotency
from backend.reliability.idempotency import IntentBusy, register_intent

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1",
        reason="Opt in to local PostgreSQL + Redis integration tests",
    ),
]

CONCURRENCY = 50


@pytest_asyncio.fixture
async def infra():
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=20, max_overflow=40)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(settings.REDIS_URL)
    try:
        await redis.ping()
    except Exception as exc:  # fail loudly: a skipped test would hide a wrong REDIS_URL
        await engine.dispose()
        pytest.fail(
            f"Redis is not reachable at REDIS_URL={settings.REDIS_URL!r} ({exc}). "
            "Compose publishes Redis on host port 6380: set REDIS_URL=redis://localhost:6380/0"
        )
    yield maker, redis
    await redis.aclose()
    await engine.dispose()


def payload(**overrides):
    base = {"path": "SMARTPAY", "amount": "500.00", "currency": "USD", "description": "Rs 500"}
    base.update(overrides)
    return base


async def call(maker, redis, owner, key, body):
    """One simulated pay click with its own database session. Returns the result or 'busy'."""
    async with maker() as session:
        try:
            return await register_intent(session, redis, owner, key, body)
        except IntentBusy:
            return "busy"


async def count(maker, sql, **params):
    async with maker() as session:
        return (await session.execute(text(sql), params)).scalar_one()


def summarise(results):
    done = [r for r in results if r != "busy"]
    created = [r for r in done if r[1] is False]
    return done, created


async def test_50_simultaneous_requests_with_the_same_key_leave_exactly_one_payment(infra):
    maker, redis = infra
    owner, key = f"itest-{uuid.uuid4().hex}", f"key-{uuid.uuid4().hex}"
    results = await asyncio.gather(
        *[call(maker, redis, owner, key, payload()) for _ in range(CONCURRENCY)]
    )
    done, created = summarise(results)
    assert len(created) == 1, "exactly one request may create the payment"
    assert len({r[0]["id"] for r in done}) == 1, "everyone who got an answer got the same payment"
    assert all(r[1] is True for r in done if r is not created[0])
    assert await count(
        maker,
        "SELECT count(*) FROM payments WHERE merchant_id = :o AND intent_key = :k",
        o=owner, k=key,
    ) == 1
    payment_id = created[0][0]["id"]
    assert await count(
        maker,
        "SELECT count(*) FROM payment_audit "
        "WHERE payment_id = :p AND event_type = 'INTENT_CREATED'",
        p=payment_id,
    ) == 1


async def test_50_taps_with_a_new_key_each_time_still_leave_one_payment(infra):
    maker, redis = infra
    owner = f"itest-{uuid.uuid4().hex}"
    results = await asyncio.gather(
        *[
            call(maker, redis, owner, f"key-{uuid.uuid4().hex}", payload(amount="500.00"))
            for _ in range(CONCURRENCY)
        ]
    )
    done, created = summarise(results)
    assert len(created) == 1
    assert len({r[0]["id"] for r in done}) == 1
    assert await count(
        maker, "SELECT count(*) FROM payments WHERE merchant_id = :o", o=owner
    ) == 1


async def test_database_unique_constraint_still_holds_if_redis_lock_is_lost(infra, monkeypatch):
    """Layer 1's last line of defence: with the lock disabled, 50 racers still make one row."""
    maker, redis = infra

    @asynccontextmanager
    async def no_lock(*args, **kwargs):
        yield "no-lock"

    monkeypatch.setattr(idempotency, "intent_lock", no_lock)
    owner, key = f"itest-{uuid.uuid4().hex}", f"key-{uuid.uuid4().hex}"
    results = await asyncio.gather(
        *[call(maker, redis, owner, key, payload()) for _ in range(CONCURRENCY)]
    )
    done, created = summarise(results)
    assert len(created) == 1
    assert len(done) == CONCURRENCY, "every racer got an answer, none crashed"
    assert len({r[0]["id"] for r in done}) == 1
    assert await count(
        maker,
        "SELECT count(*) FROM payments WHERE merchant_id = :o AND intent_key = :k",
        o=owner, k=key,
    ) == 1


async def test_same_key_with_a_different_amount_is_rejected_by_the_real_database(infra):
    maker, redis = infra
    owner, key = f"itest-{uuid.uuid4().hex}", f"key-{uuid.uuid4().hex}"
    await call(maker, redis, owner, key, payload())
    async with maker() as session:
        with pytest.raises(idempotency.PayloadMismatch):
            await register_intent(session, redis, owner, key, payload(amount="501.00"))


async def test_different_owners_can_use_the_same_key(infra):
    maker, redis = infra
    key = f"key-{uuid.uuid4().hex}"
    first = await call(maker, redis, f"itest-{uuid.uuid4().hex}", key, payload())
    second = await call(maker, redis, f"itest-{uuid.uuid4().hex}", key, payload())
    assert first[1] is False and second[1] is False
    assert first[0]["id"] != second[0]["id"]


async def test_a_closed_payment_does_not_block_a_new_one_with_the_same_amount(infra):
    maker, redis = infra
    owner = f"itest-{uuid.uuid4().hex}"
    first, _ = await call(maker, redis, owner, f"key-{uuid.uuid4().hex}", payload())
    async with maker() as session:  # close the first payment (status is final)
        await session.execute(
            text("UPDATE payments SET status = 'CAPTURED' WHERE id = :i"), {"i": first["id"]}
        )
        await session.commit()
    second, replay = await call(maker, redis, owner, f"key-{uuid.uuid4().hex}", payload())
    assert replay is False
    assert second["id"] != first["id"]


async def test_50_legitimate_purchases_with_different_order_ids_are_all_created(infra):
    """Same buyer, same amount, same moment, but 50 different orders: none may be merged."""
    maker, redis = infra
    owner = f"itest-{uuid.uuid4().hex}"
    results = await asyncio.gather(
        *[
            call(
                maker, redis, owner, f"key-{uuid.uuid4().hex}",
                payload(merchant_order_id=f"order-{uuid.uuid4().hex}"),
            )
            for _ in range(CONCURRENCY)
        ]
    )
    done, created = summarise(results)
    assert len(done) == CONCURRENCY, "no purchase was blocked or turned into a replay"
    assert len(created) == CONCURRENCY
    assert len({r[0]["id"] for r in done}) == CONCURRENCY
    assert await count(
        maker, "SELECT count(*) FROM payments WHERE merchant_id = :o", o=owner
    ) == CONCURRENCY


async def test_50_requests_for_one_order_id_with_different_keys_leave_one_payment(infra):
    """The order id is the purchase: new keys for the same order must not create a second."""
    maker, redis = infra
    owner, order = f"itest-{uuid.uuid4().hex}", f"order-{uuid.uuid4().hex}"
    results = await asyncio.gather(
        *[
            call(maker, redis, owner, f"key-{uuid.uuid4().hex}", payload(merchant_order_id=order))
            for _ in range(CONCURRENCY)
        ]
    )
    done, created = summarise(results)
    assert len(done) == CONCURRENCY, "every racer got an answer, none crashed"
    assert len(created) == 1
    assert len({r[0]["id"] for r in done}) == 1
    assert await count(
        maker,
        "SELECT count(*) FROM payments WHERE merchant_id = :o AND merchant_order_id = :m",
        o=owner, m=order,
    ) == 1


async def test_same_key_with_a_different_order_id_is_rejected_by_the_real_database(infra):
    maker, redis = infra
    owner, key = f"itest-{uuid.uuid4().hex}", f"key-{uuid.uuid4().hex}"
    await call(maker, redis, owner, key, payload(merchant_order_id="order-A"))
    async with maker() as session:
        with pytest.raises(idempotency.PayloadMismatch):
            await register_intent(
                session, redis, owner, key, payload(merchant_order_id="order-B")
            )


async def test_same_order_id_with_a_different_amount_is_rejected_by_the_real_database(infra):
    maker, redis = infra
    owner, order = f"itest-{uuid.uuid4().hex}", f"order-{uuid.uuid4().hex}"
    await call(maker, redis, owner, f"key-{uuid.uuid4().hex}", payload(merchant_order_id=order))
    async with maker() as session:
        with pytest.raises(idempotency.PayloadMismatch):
            await register_intent(
                session, redis, owner, f"key-{uuid.uuid4().hex}",
                payload(merchant_order_id=order, amount="501.00"),
            )


async def test_condition_is_compared_after_a_round_trip_through_the_database(infra):
    maker, redis = infra
    owner, key = f"itest-{uuid.uuid4().hex}", f"key-{uuid.uuid4().hex}"
    stored = {"kind": "manual", "description": "Parcel delivered", "tags": ["a", "b"]}
    first, _ = await call(
        maker, redis, owner, key, payload(path="CONDITIONALPAY", condition=stored)
    )
    # Same condition with the keys in another order: still the same request.
    reordered = {"tags": ["a", "b"], "description": "Parcel delivered", "kind": "manual"}
    again, replay = await call(
        maker, redis, owner, key, payload(path="CONDITIONALPAY", condition=reordered)
    )
    assert replay is True and again["id"] == first["id"]
    # A different condition under the same key is a conflict.
    for conflicting in (
        {"kind": "manual", "description": "Something else", "tags": ["a", "b"]},
        None,
    ):
        async with maker() as session:
            with pytest.raises(idempotency.PayloadMismatch):
                await register_intent(
                    session, redis, owner, key,
                    payload(path="CONDITIONALPAY", condition=conflicting),
                )