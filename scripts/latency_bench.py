from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from stress_fake import drop_database, make_database  # noqa: E402

from backend.reliability import timing  # noqa: E402

LABEL = "SIMULATOR numbers (fake PayPal); NOT real PayPal sandbox numbers"
OVERHEAD_TARGET_MS = 100
CONDITION_TARGET_MS = 1000
FIRST_CHECK_TARGET_MS = 500


def fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:8.1f}"


async def run(args: argparse.Namespace) -> int:
    from fakeredis import FakeAsyncRedis
    from fastapi import FastAPI

    from backend.api.deps import install_error_handlers
    from backend.api.routes_payments import router as payments_router
    from tests.integration.sim_support import SMART, SimWorld

    name, url, admin = await make_database()
    engine = create_async_engine(url, pool_size=20, max_overflow=30)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    world = SimWorld(maker, FakeAsyncRedis(decode_responses=True))
    world.api._quick_reconcile = True  # the app turns this on at startup
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(payments_router)
    app.state.payment_api = world.api
    app.state.session_factory = maker
    headers = {"X-Owner-Id": "latency-bench"}
    try:
        if args.drop_rate > 0:
            await world.faults(
                seed=args.seed, drop_after=["create_order"], drop_rate=args.drop_rate
            )
        gate = asyncio.Semaphore(args.concurrency)
        wall: list[float] = []
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://bench") as http:

            async def one(index: int) -> None:
                async with gate:
                    body = {**SMART, "amount": f"{5 + index}.{index % 100:02d}"}
                    began = time.perf_counter()
                    created = await http.post(
                        "/api/payments",
                        json=body,
                        headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
                    )
                    if created.status_code not in (200, 201):
                        return
                    view = created.json()
                    if view["status"] != "AWAITING_APPROVAL":
                        return  # a lost answer: reconciled in the background
                    await world.approve(type("V", (), {"id": view["id"]}))
                    await http.post(f"/api/payments/{view['id']}/return", headers=headers)
                    wall.append((time.perf_counter() - began) * 1000)

            await asyncio.gather(*(one(i) for i in range(args.payments)))
        await timing.wait_background(timeout=20.0)

        async with maker() as session:
            result = await session.execute(
                text(
                    "SELECT intent_id::text AS intent_id, step, kind, ms::float AS ms "
                    "FROM payment_timings"
                )
            )
            rows = [dict(r) for r in result.mappings()]
    finally:
        await engine.dispose()
        await world.redis.aclose()
        await drop_database(name, admin)

    summary = timing.summarize_rows(rows)
    print(f"\nLatency benchmark: {args.payments} payments, {args.concurrency} at a time")
    print(f"({LABEL})\n")
    print(f"{'':28}{'p50 ms':>10}{'p95 ms':>10}")
    for label, key in (
        ("SlothPay overhead (ours)", "overhead"),
        ("PayPal (provider)", "provider"),
        ("Buyer (human)", "human"),
    ):
        print(f"{label:28}{fmt(summary[key]['p50_ms'])}{fmt(summary[key]['p95_ms'])}")
    for step in ("condition_to_capture", "reconcile_wait", "reconcile_read"):
        stats = summary["steps"].get(step)
        if stats:
            print(f"{step:28}{fmt(stats['p50_ms'])}{fmt(stats['p95_ms'])}   (n={stats['count']})")
    print("\nEvery step (p50 / p95 ms):")
    for step_name, stats in sorted(
        summary["steps"].items(), key=lambda item: -(item[1]["p95_ms"] or 0)
    ):
        print(
            f"  {step_name:26}{stats['kind']:9}{fmt(stats['p50_ms'])}{fmt(stats['p95_ms'])}"
            f"   (n={stats['count']})"
        )
    waits = [r["ms"] for r in rows if r["step"] == "reconcile_wait"]
    print(f"\nPayments measured: {summary['payments']}; reconcile reads: {len(waits)}")

    overhead_p95 = summary["overhead"]["p95_ms"]
    ok = overhead_p95 is not None and overhead_p95 < OVERHEAD_TARGET_MS
    print(f"Target overhead p95 < {OVERHEAD_TARGET_MS} ms: {'MET' if ok else 'MISSED'}")
    cond = summary["steps"].get("condition_to_capture")
    if cond:
        met = cond["p95_ms"] < CONDITION_TARGET_MS
        print(
            f"Target condition-to-capture < {CONDITION_TARGET_MS} ms: {'MET' if met else 'MISSED'}"
        )
    if waits:
        first = min(waits)
        print(
            f"Target first reconcile check <= {FIRST_CHECK_TARGET_MS} ms: "
            f"{'MET' if first <= FIRST_CHECK_TARGET_MS else 'MISSED'} (fastest {first:.0f} ms)"
        )
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Latency benchmark against the local PayPal simulator"
    )
    parser.add_argument("--payments", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--drop-rate", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=1919)
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()