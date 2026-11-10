from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from backend.payments.paypal_client import PayPalClient

log = logging.getLogger("slothpay.paypal")

REFRESH_MARGIN_SECONDS = 120.0
RETRY_SECONDS = 30.0
MIN_SLEEP_SECONDS = 5.0


class SharedPayPal:
    def __init__(self, factory: Callable[[], PayPalClient] = PayPalClient) -> None:
        self._factory = factory
        self.client: PayPalClient | None = None
        self._keeper: asyncio.Task | None = None
        self._warmed = asyncio.Event()
        self.refreshes = 0

    async def start(self) -> None:
        try:
            self.client = self._factory()
        except ValueError:
            # No PayPal credentials: the API still starts, and a payment says it is unavailable.
            log.info("PayPal is not configured: no shared client was created")
            return
        self._keeper = asyncio.get_running_loop().create_task(
            self._keep_token_fresh(), name="paypal-token-keeper"
        )

    async def wait_ready(self, timeout: float = 5.0) -> bool:
        """True once the first token has been fetched (used by tests and a readiness probe)."""
        try:
            await asyncio.wait_for(self._warmed.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def _keep_token_fresh(self) -> None:
        assert self.client is not None
        while True:
            try:
                if self.client.token_seconds_left() <= REFRESH_MARGIN_SECONDS:
                    await self.client.refresh_token()
                    self.refreshes += 1
                self._warmed.set()
                wait = self.client.token_seconds_left() - REFRESH_MARGIN_SECONDS
                await asyncio.sleep(max(MIN_SLEEP_SECONDS, wait))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never log the exception text: it could carry a response body.
                log.warning("PayPal token refresh failed (%s); trying again", type(exc).__name__)
                await asyncio.sleep(RETRY_SECONDS)

    async def stop(self) -> None:
        keeper, self._keeper = self._keeper, None
        if keeper is not None:
            keeper.cancel()
            try:
                await keeper
            except (asyncio.CancelledError, Exception):
                pass
        client, self.client = self.client, None
        if client is not None:
            await client.aclose()

    # The paypal_factory the API uses: `async with shared() as gateway:`
    def __call__(self) -> AbstractAsyncContextManager[Any]:
        return self._lease()

    @asynccontextmanager
    async def _lease(self) -> AsyncIterator[Any]:
        if self.client is not None:
            yield self.client  # shared: never closed here
            return
        fallback = self._factory()  # raises ValueError when PayPal is not configured
        async with fallback as client:
            yield client