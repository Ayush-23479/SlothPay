from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
import uuid

import httpx

MODES = ("none", "delay", "drop_before_send", "drop_after_send", "mock_error")
OWNER = "stress-run"
# Past order creation: the payment is waiting for the buyer or already moved on.
DONE = {"AWAITING_APPROVAL", "APPROVED", "CAPTURED", "AUTHORIZED", "AWAITING_CONDITION"}
STUCK_WORDS = {"UNCERTAIN"}
FAILED = {"FAILED", "CANCELLED", "VOIDED"}


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))
    return ordered[index]


async def set_chaos(http: httpx.AsyncClient, admin_key: str, args: argparse.Namespace) -> None:
    body: dict = {"mode": args.mode, "probability": args.probability}
    if args.mode == "delay":
        body["delay_ms"] = args.delay_ms
    if args.mode == "mock_error":
        body["code"] = args.code
    response = await http.post("/api/chaos", json=body, headers={"X-Admin-Api-Key": admin_key})
    if response.status_code != 200:
        sys.exit(f"Could not set chaos ({response.status_code}): {response.text[:200]}")


async def one_payment(http: httpx.AsyncClient, index: int, run: str, args) -> dict:
    key = f"stress-{run}-{index:04d}-{uuid.uuid4().hex[:8]}"
    body = {
        "path": "SMARTPAY",
        "amount": "10.00",
        "currency": "USD",
        "description": f"stress {run} #{index}",
        "merchant_order_id": f"stress-{run}-{index}",
    }
    headers = {"Idempotency-Key": key, "X-Owner-Id": OWNER}
    started = time.monotonic()
    first, second = await asyncio.gather(
        http.post("/api/payments", json=body, headers=headers),
        http.post("/api/payments", json=body, headers=headers),
        return_exceptions=True,
    )
    ids: set[str] = set()
    orders: set[str] = set()
    for response in (first, second):
        if isinstance(response, BaseException) or response.status_code not in (200, 201):
            continue
        data = response.json()
        ids.add(data["id"])
        order = (data.get("paypal_ids") or {}).get("order_id")
        if order:
            orders.add(order)
    result = {"ids": ids, "orders": orders, "status": "NOT_CREATED", "seconds": None}
    if not ids:
        return result
    payment_id = next(iter(ids))
    deadline = started + args.timeout
    while time.monotonic() < deadline:
        response = await http.get(f"/api/payments/{payment_id}", headers={"X-Owner-Id": OWNER})
        if response.status_code == 200:
            data = response.json()
            result["status"] = data["status"]
            order = (data.get("paypal_ids") or {}).get("order_id")
            if order:
                orders.add(order)
            if data["status"] in DONE:
                result["seconds"] = time.monotonic() - started
                return result
            if data["status"] in FAILED:
                return result
        await asyncio.sleep(0.5)
    return result


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", default=os.getenv("SLOTHPAY_API", "http://localhost:8000"))
    parser.add_argument("--n", type=int, default=10, help="number of payments (1-200)")
    parser.add_argument("--mode", choices=MODES, default="none")
    parser.add_argument("--probability", type=float, default=0.5)
    parser.add_argument("--delay-ms", type=int, default=1500)
    parser.add_argument("--code", default="INSTRUMENT_DECLINED")
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds to wait per payment")
    args = parser.parse_args()
    if not 1 <= args.n <= 200:
        sys.exit("--n must be between 1 and 200")
    admin_key = os.getenv("ADMIN_API_KEY", "")
    if args.mode != "none" and not admin_key:
        sys.exit("Set ADMIN_API_KEY to switch a failure mode on")

    run = uuid.uuid4().hex[:6]
    limit = asyncio.Semaphore(max(1, args.concurrency))
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0) as http:
        if admin_key:
            await set_chaos(http, admin_key, args)
        try:

            async def guarded(i: int) -> dict:
                async with limit:
                    return await one_payment(http, i, run, args)

            results = await asyncio.gather(*(guarded(i) for i in range(args.n)))
            metrics = await http.get("/api/metrics")
        finally:
            if admin_key:
                await http.post(
                    "/api/chaos", json={"mode": "none"}, headers={"X-Admin-Api-Key": admin_key}
                )

    duplicate_intents = sum(1 for r in results if len(r["ids"]) > 1)
    all_orders = [o for r in results for o in r["orders"]]
    duplicate_orders = sum(1 for r in results if len(r["orders"]) > 1) + (
        len(all_orders) - len(set(all_orders))
    )
    completed = [r for r in results if r["status"] in DONE]
    uncertain = [r for r in results if r["status"] in STUCK_WORDS]
    times = [r["seconds"] for r in completed if r["seconds"] is not None]

    print(f"\nStress run {run}: {args.n} payments, mode={args.mode}, p={args.probability}")
    print("-" * 52)
    print(f"{'duplicate intents (must be 0)':<34}{duplicate_intents:>6}")
    print(f"{'duplicate PayPal orders (must be 0)':<34}{duplicate_orders:>6}")
    print(f"{'completed':<34}{len(completed):>6}  ({len(completed) / args.n:.0%})")
    print(f"{'still UNCERTAIN':<34}{len(uncertain):>6}")
    print(f"{'other / failed':<34}{args.n - len(completed) - len(uncertain):>6}")
    if times:
        print(f"{'latency avg (s, sandbox)':<34}{statistics.mean(times):>6.2f}")
        print(f"{'latency p95 (s, sandbox)':<34}{percentile(times, 95):>6.2f}")
    if metrics.status_code == 200:
        m = metrics.json()
        print(
            f"{'metrics: recovered / uncertain_now':<34}{m['recovered']:>3} / {m['uncertain_now']}"
        )
    print("-" * 52)
    ok = duplicate_intents == 0 and duplicate_orders == 0
    print("RESULT:", "PASS (no duplicates)" if ok else "FAIL (duplicates found)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))