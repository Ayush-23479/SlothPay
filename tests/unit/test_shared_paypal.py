from __future__ import annotations

import asyncio

import pytest

from backend.payments import shared as shared_module
from backend.payments.shared import SharedPayPal


class FakeClient:
    def __init__(self) -> None:
        self.left = 0.0
        self.refreshed = 0
        self.closed = 0

    def token_seconds_left(self) -> float:
        return self.left

    async def refresh_token(self) -> None:
        self.refreshed += 1
        self.left = 3000.0

    async def aclose(self) -> None:
        self.closed += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()


async def test_token_is_fetched_once_at_startup_and_the_client_is_shared():
    fake = FakeClient()
    shared = SharedPayPal(lambda: fake)
    await shared.start()
    assert await shared.wait_ready()
    assert fake.refreshed == 1
    async with shared() as first, shared() as second:
        assert first is second is fake
    assert fake.closed == 0  # leaving the block never closes the shared client
    await shared.stop()
    assert fake.closed == 1


async def test_token_is_renewed_in_the_background_before_it_expires(monkeypatch):
    monkeypatch.setattr(shared_module, "MIN_SLEEP_SECONDS", 0.01)
    fake = FakeClient()

    async def short_lived() -> None:  # every token is already inside the refresh margin
        fake.refreshed += 1
        fake.left = 100.0

    fake.refresh_token = short_lived  # type: ignore[method-assign]
    shared = SharedPayPal(lambda: fake)
    await shared.start()
    await shared.wait_ready()
    await asyncio.sleep(0.1)
    assert fake.refreshed >= 2
    await shared.stop()


async def test_without_credentials_it_falls_back_to_a_client_that_raises_as_before():
    def unconfigured():
        raise ValueError("PayPal is not configured")

    shared = SharedPayPal(unconfigured)
    await shared.start()
    assert shared.client is None
    with pytest.raises(ValueError):
        async with shared():
            pass
    await shared.stop()


async def test_a_failing_refresh_does_not_crash_the_keeper(monkeypatch):
    monkeypatch.setattr(shared_module, "RETRY_SECONDS", 0.01)
    fake = FakeClient()

    async def boom() -> None:
        fake.refreshed += 1
        if fake.refreshed < 3:
            raise RuntimeError("secret body")
        fake.left = 3000.0

    fake.refresh_token = boom  # type: ignore[method-assign]
    shared = SharedPayPal(lambda: fake)
    await shared.start()
    assert await shared.wait_ready()
    assert fake.refreshed == 3
    await shared.stop()