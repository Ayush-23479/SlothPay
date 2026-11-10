from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from backend.payments.paypal_client import PayPalError, PayPalNotSent, PayPalOutcomeUnknown
from backend.reliability import timing

MAX_ATTEMPTS = 2  # the first request and one blind retry


def _masked(order_id: str | None) -> str | None:
    if not order_id:
        return None
    return f"{order_id[:4]}…{order_id[-4:]}" if len(order_id) > 8 else "…"


async def run_naive(
    gateway: Any,
    *,
    amount: str,
    currency: str,
    timeout_seconds: float,
    return_url: str | None = None,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> dict[str, Any]:
    timer = timing.StepTimer()
    token = timing.activate(timer)
    began = time.perf_counter()
    requests_sent = 0
    waited_ms = 0.0
    order_ids: list[str] = []
    outcome = "failed"
    try:
        for _attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                requests_sent += 1
                response = await gateway.create_order_naive(
                    amount=amount,
                    currency=currency,
                    return_url=return_url,
                    timeout=timeout_seconds,
                )
            except PayPalNotSent:
                requests_sent -= 1  # it never left, so PayPal never saw it
                outcome = "not_sent"
                continue
            except PayPalOutcomeUnknown:
                # A bare client cannot tell "lost" from "slow": it waits out its timeout and
                # sends the same request again, now with nothing PayPal can match it to.
                outcome = "answer_lost"
                waited_from = time.perf_counter()
                await sleep(timeout_seconds)
                waited_ms += (time.perf_counter() - waited_from) * 1000
                continue
            except PayPalError:
                outcome = "rejected"
                break
            order_id = response.get("id") if isinstance(response, dict) else None
            if isinstance(order_id, str):
                order_ids.append(order_id)
            outcome = "ok"
            break
    finally:
        timing.deactivate(token)
    total_ms = (time.perf_counter() - began) * 1000
    provider_ms = sum(s.ms for s in timer.steps if s.kind == timing.PROVIDER)
    ours_ms = max(0.0, total_ms - provider_ms - waited_ms)
    # Requests that reached PayPal. Without a request id each one can be a separate order.
    possible_orders = requests_sent
    return {
        "arm": "baseline",
        "label": timing.LABEL,
        "outcome": outcome,
        "attempts": requests_sent,
        "paypal_orders_possible": possible_orders,
        "order_ids": [_masked(o) for o in order_ids],
        "waited_ms": round(waited_ms, 1),
        "provider_ms": round(provider_ms, 1),
        "ours_ms": round(ours_ms, 1),
        "total_ms": round(total_ms, 1),
        "timeout_seconds": timeout_seconds,
        "request_id_used": False,
        "note": _note(outcome, possible_orders, timeout_seconds),
    }


def _note(outcome: str, orders: int, timeout: float) -> str:
    if outcome == "ok" and orders == 1:
        return "One order. With nothing going wrong a bare checkout works fine."
    if outcome == "ok":
        return (
            f"The first answer was lost. It waited {timeout:g} s, sent the request again with no "
            f"request id, and {orders} requests reached PayPal. If the first one was processed, "
            "that is two orders for one payment."
        )
    if outcome == "answer_lost":
        return "Both answers were lost. It cannot tell what PayPal did."
    if outcome == "not_sent":
        return "The request never left. Nothing was created."
    return "PayPal refused the request."