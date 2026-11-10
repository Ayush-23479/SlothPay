import pytest
from fakeredis import FakeAsyncRedis

from backend.extensions.replay_guard import (
    ReplayGuard,
    ReplayRejected,
)


@pytest.mark.asyncio
async def test_first_event_is_accepted():
    redis = FakeAsyncRedis()
    guard = ReplayGuard(redis)

    await guard.claim("delivery-00000001")
    await guard.complete("delivery-00000001")

    assert await redis.get(guard._key("delivery-00000001")) == b"processed"


@pytest.mark.asyncio
async def test_repeated_event_is_rejected():
    redis = FakeAsyncRedis()
    guard = ReplayGuard(redis)

    await guard.claim("delivery-00000001")

    with pytest.raises(ReplayRejected):
        await guard.claim("delivery-00000001")


@pytest.mark.asyncio
async def test_completed_event_cannot_be_replayed():
    redis = FakeAsyncRedis()
    guard = ReplayGuard(redis)

    await guard.claim("delivery-00000001")
    await guard.complete("delivery-00000001")

    with pytest.raises(ReplayRejected):
        await guard.claim("delivery-00000001")


@pytest.mark.asyncio
async def test_different_events_are_independent():
    redis = FakeAsyncRedis()
    guard = ReplayGuard(redis)

    await guard.claim("delivery-00000001")
    await guard.claim("delivery-00000002")

    assert await redis.get(guard._key("delivery-00000001"))
    assert await redis.get(guard._key("delivery-00000002"))


@pytest.mark.asyncio
async def test_complete_requires_existing_claim():
    redis = FakeAsyncRedis()
    guard = ReplayGuard(redis)

    with pytest.raises(ReplayRejected):
        await guard.complete("delivery-00000001")
