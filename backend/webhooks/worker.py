"""Explicit, one-shot processing of verified PayPal webhook inbox events."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence

from sqlalchemy import column, select, table
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config.settings import get_settings
from backend.models.event import ProviderEvent
from backend.webhooks.processing import process_webhook_event


async def process_batch(limit: int = 50) -> list[dict[str, str | None]]:
    """Process at most ``limit`` verified events and return their dispositions."""
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    engine = create_async_engine(get_settings().DATABASE_URL)
    results: list[dict[str, str | None]] = []
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            pending = await session.scalars(
                select(ProviderEvent.event_id)
                .where(ProviderEvent.provider == "paypal", ProviderEvent.verified.is_(True))
                .where(
                    ~ProviderEvent.id.in_(
                        select(column("provider_event_id")).select_from(
                            table("webhook_processing")
                        )
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.webhooks.worker",
        description="Process verified PayPal inbox events only with --run.",
    )
    parser.add_argument("--run", action="store_true", help="Explicitly process a batch")
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        metavar="N",
        help="Maximum events per run (1–500; default: 50)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.run:
        parser.print_help()
        return 0
    if not 1 <= args.limit <= 500:
        parser.error("--limit must be between 1 and 500")
    print(json.dumps(asyncio.run(process_batch(limit=args.limit)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
