"""One-shot, operator-invoked processor for verified durable PayPal webhook events.

Run on a disposable/test DB first: python -m backend.webhooks.worker
Never sends money operations. Events needing review remain recorded for triage.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import column, select, table
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config.settings import get_settings
from backend.models.event import ProviderEvent
from backend.webhooks.processing import process_webhook_event


async def process_batch(limit: int = 50) -> list[dict[str, str | None]]:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    engine = create_async_engine(get_settings().DATABASE_URL)
    results = []
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            # Candidate query intentionally does not lock; the individual event
            # processing transaction locks and ensures exactly-once bookkeeping.
            pending = await session.scalars(
                select(ProviderEvent.event_id)
                .where(ProviderEvent.provider == "paypal", ProviderEvent.verified.is_(True))
                .where(
                    ~ProviderEvent.id.in_(
                        select(column("provider_event_id")).select_from(table("webhook_processing"))
                    )
                )
                .order_by(ProviderEvent.received_at, ProviderEvent.id)
                .limit(limit)
            )
            event_ids = list(pending)
        for event_id in event_ids:
            async with maker() as session:
                result = await process_webhook_event(session, event_id=event_id)
                await session.commit()
                results.append(
                    {
                        "event_id": result.event_id,
                        "disposition": result.disposition,
                        "payment_id": result.payment_id,
                        "reason": result.reason,
                    }
                )
    finally:
        await engine.dispose()
    return results


def main() -> None:
    import json

    print(json.dumps(asyncio.run(process_batch()), indent=2))


if __name__ == "__main__":
    main()
