"""One-shot runner for due PayPal reconciliation checks.

Invoke periodically; each run performs due checks only and never sleeps in checkout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config.settings import get_settings
from backend.orchestration.reconcile import process_due_reconciliations
from backend.payments.paypal_client import PayPalClient


async def process_batch(limit: int = 50) -> list[dict]:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    engine = create_async_engine(get_settings().DATABASE_URL)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with PayPalClient() as paypal:
            async with maker() as session:
                observations = await process_due_reconciliations(
                    session, paypal, limit=limit
                )
                return [item.to_dict() for item in observations]
    finally:
        await engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.orchestration.recovery_worker",
        description="Process due PayPal reconciliation checks only with --run.",
    )
    parser.add_argument("--run", action="store_true", help="Run one due-check batch")
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        metavar="N",
        help="Maximum checks per run (1–500; default: 50)",
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
