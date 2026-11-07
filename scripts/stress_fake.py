"""Stress run against a LOCAL SIMULATOR (fake PayPal). Not the real PayPal sandbox.

    python scripts/stress_fake.py                 # 100 payments, seed 1515
    python scripts/stress_fake.py --payments 300 --seed 7

What it does: SlothPay's real API code, a throw-away local PostgreSQL database and the fake
PayPal in tests/fake_paypal. Random faults (503s before the work, lost answers after the work,
a little delay) are drawn from a fixed seed, so the faults repeat for the same seed. Each payment
is double-clicked (same Idempotency-Key twice at once), approved, and returned until it settles.

It prints duplicates (must be 0), completion rate, retries survived and p95 latency, and exits
with status 1 if any duplicate was found. Every number is a simulator number. Real sandbox
numbers are measured separately (scripts/stress_run.py and tests/e2e/test_sandbox_manual.py).

Needs a local PostgreSQL (DATABASE_URL on localhost) and the dev requirements (fakeredis).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import statistics
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from backend.config.settings import get_settings  # noqa: E402
from backend.models.payment import Payment  # noqa: E402

LABEL = "against a local simulator (fake PayPal); NOT real PayPal sandbox numbers"


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))]


async def make_database() -> tuple[str, str, object]:
    base = make_url(get_settings().DATABASE_URL)
    if base.host not in ("localhost", "127.0.0.1"):
        sys.exit("Refusing to run: DATABASE_URL must point at a local PostgreSQL.")
    name = f"slothpay_stress_{uuid.uuid4().hex[:10]}"
    admin_url = base.set(database="postgres").render_as_string(hide_password=False)
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name).render_as_string(hide_password=False)
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=ROOT,
    )
    return name, url, admin


async def drop_database(name: str, admin) -> None:
    async with admin.connect() as connection:
        await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    await admin.dispose()


async def run(args: argparse.Namespace) -> int:
    from fakeredis import FakeAsyncRedis

    from tests.integration.sim_support import SMART, SimWorld

    name, url, admin = await make_database()
    engine = create_async_engine(url, pool_size=20, max_overflow=30)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    world = SimWorld(maker, FakeAsyncRedis(decode_responses=True))
    try:
        await world.faults(
            seed=args.seed,
            delay_ms=args.delay_ms,
            error_rate=args.error_rate,
            error_when="before",
            drop_after=["capture_order"],
            drop_rate=args.drop_rate,
        )
        rng = random.Random(args.seed)
        owner = "stress-fake"
        gate = asyncio.Semaphore(args.concurrency)
        latencies: list[float] = []
        outcomes: Counter[str] = Counter()
        uncertain_then_done = 0
        create_errors: Counter[str] = Counter()

        async def one(index: int) -> None:
            nonlocal uncertain_then_done
            # unique per payment: the same amount twice within 2 minutes counts as one double click
            amount = f"{5 + index}.{rng.randint(0, 99):02d}"
            payload = {**SMART, "amount": amount}
            key = f"stress-{index}-{uuid.uuid4().hex}"
            async with gate:
                started = time.perf_counter()
                # a double click: the same key twice at once
                results = await asyncio.gather(
                    world.api.create_payment(owner, key, payload),
                    world.api.create_payment(owner, key, payload),
                    return_exceptions=True,
                )
                views = [r[0] for r in results if not isinstance(r, BaseException)]
                if not views:
                    outcomes["CREATE_FAILED"] += 1
                    create_errors.update(type(r).__name__ + ": " + str(r)[:90] for r in results)
                    latencies.append((time.perf_counter() - started) * 1000)
                    return
                view = views[0]
                status = view.status
                if status == "AWAITING_APPROVAL":
                    await world.approve(view)
                hit_uncertain = False
                for _ in range(args.max_returns):
                    if status in {"CAPTURED", "FAILED", "VOIDED", "CANCELLED"}:
                        break
                    if not await _has_order(maker, view):
                        break  # the order never existed: nothing to return to
                    done = await world.api.buyer_returned(owner, uuid.UUID(view.id))
                    status = done.status
                    hit_uncertain = hit_uncertain or status == "UNCERTAIN"
                if status == "CAPTURED" and hit_uncertain:
                    uncertain_then_done += 1
                outcomes[status] += 1
                latencies.append((time.perf_counter() - started) * 1000)

        await asyncio.gather(*[one(i) for i in range(args.payments)])

        stats = await world.stats()
        async with maker() as session:
            payments = await session.scalar(
                select(func.count(Payment.id)).where(Payment.merchant_id == owner)
            )
            with_order = await session.scalar(
                select(func.count(Payment.id)).where(
                    Payment.merchant_id == owner, Payment.paypal_order_id.is_not(None)
                )
            )
            captured = await session.scalar(
                select(func.count(Payment.id)).where(
                    Payment.merchant_id == owner, Payment.status == "CAPTURED"
                )
            )
        duplicate_payments = max(0, payments - args.payments)
        duplicate_orders = max(0, stats["orders"] - with_order)
        duplicate_captures = stats["orders_with_more_than_one_capture_request_id"] + max(
            0, stats["captures"] - captured
        )
        duplicates = duplicate_payments + duplicate_orders + duplicate_captures

        print(f"STRESS RUN {LABEL}")
        print(f"seed={args.seed} payments={args.payments} concurrency={args.concurrency}")
        print(
            f"faults: {args.error_rate:.0%} 503 before the work, {args.drop_rate:.0%} lost "
            f"capture answers, {args.delay_ms} ms delay"
        )
        for error, count in create_errors.items():
            print(f"create error x{count}: {error}")
        print(f"payments in the database       : {payments}")
        print(f"orders at the simulator        : {stats['orders']}")
        print(f"captures at the simulator      : {stats['captures']}")
        print(f"final states                   : {dict(sorted(outcomes.items()))}")
        print(
            f"completion rate                : {captured / args.payments:.1%} "
            f"({captured}/{args.payments})"
        )
        print(
            f"retries survived               : {uncertain_then_done} payments went UNCERTAIN "
            f"and still ended CAPTURED"
        )
        print(
            f"lost answers / 503s injected   : {stats['dropped_responses']} / "
            f"{stats['errors_returned']}"
        )
        print(
            f"latency p50 / p95 (ms)         : {statistics.median(latencies):.0f} / "
            f"{percentile(latencies, 95):.0f}  (SlothPay's own time; simulator answers instantly)"
        )
        print(f"DUPLICATE PAYMENTS             : {duplicate_payments}")
        print(f"DUPLICATE ORDERS               : {duplicate_orders}")
        print(f"DUPLICATE CAPTURES             : {duplicate_captures}")
        print(f"TOTAL DUPLICATES               : {duplicates}")
        print("Result:", "PASS, 0 duplicates" if duplicates == 0 else "FAIL, duplicates found")
        print(
            "Remember: these are simulator results. Real sandbox numbers are reported separately."
        )
        return 0 if duplicates == 0 else 1
    finally:
        await engine.dispose()
        await world.redis.aclose()
        await drop_database(name, admin)


async def _has_order(maker, view) -> bool:
    async with maker() as session:
        payment = await session.get(Payment, uuid.UUID(view.id), populate_existing=True)
        return bool(payment and payment.paypal_order_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--payments", type=int, default=100)
    parser.add_argument("--seed", type=int, default=1515)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--error-rate", type=float, default=0.10)
    parser.add_argument("--drop-rate", type=float, default=0.25)
    parser.add_argument("--delay-ms", type=int, default=2)
    parser.add_argument("--max-returns", type=int, default=4)
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()