from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from backend.api.schemas import EventOut, PaymentOut
from backend.notifications.hub import EventHub

log = logging.getLogger("slothpay.stream")

POLL_SECONDS = 5.0
BATCH = 100

FetchEvents = Callable[[str | None, int], Awaitable[list[EventOut]]]
FetchPayment = Callable[[], Awaitable[PaymentOut | None]]


async def payment_event_stream(
    *,
    hub: EventHub,
    intent_id: str,
    fetch_events: FetchEvents,
    fetch_payment: FetchPayment,
    cursor: str | None,
    poll_seconds: float = POLL_SECONDS,
) -> AsyncIterator[dict[str, Any]]:
    """Yield SSE messages: `payment` (one audit event, id = cursor) and `snapshot` (the
    payment as it is now, sent after a batch of events so a tab can redraw without a GET)."""
    async with hub.subscription(intent_id) as sub:
        while True:
            sent = False
            while True:
                events = await fetch_events(cursor, BATCH)
                for event in events:
                    cursor = event.id
                    sent = True
                    yield {"id": event.id, "event": "payment", "data": event.model_dump_json()}
                if len(events) < BATCH:
                    break
            if sent:
                payment = await fetch_payment()
                if payment is not None:
                    yield {"event": "snapshot", "data": payment.model_dump_json()}
            try:
                await sub.next(poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Redis hiccup: keep serving from the database by polling.
                log.warning("live-update subscription failed; polling instead", exc_info=True)
                await asyncio.sleep(poll_seconds)