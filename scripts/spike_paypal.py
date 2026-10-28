"""Manual PayPal SANDBOX spike (Task 4 "done when").

Gets a token, creates ONE order, prints the approval link, reads the order back.
It never captures, authorizes or voids anything, so no money moves: the order
stays CREATED unless a buyer approves it in the browser.

Credentials come from the environment / .env only (PAYPAL_CLIENT_ID,
PAYPAL_CLIENT_SECRET). Nothing secret is ever printed.

Run from the project root:
    python scripts/spike_paypal.py                 # USD
    python scripts/spike_paypal.py --currency INR  # day-one check: does INR work?
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

# Allow `python scripts/spike_paypal.py` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.payments.paypal_client import (  # noqa: E402
    PayPalClient,
    PayPalError,
    PayPalNotSent,
    PayPalOutcomeUnknown,
)


def approval_link(order: dict) -> str | None:
    for link in order.get("links", []):
        if isinstance(link, dict) and link.get("rel") in {"payer-action", "approve"}:
            return link.get("href")
    return None


async def run(paypal: PayPalClient, *, amount: str, currency: str, intent: str) -> int:
    """Return a process exit code: 0 if the order was created and read back."""
    await paypal.check_authentication()
    print("OAuth: OK")

    request_id = f"spike-{uuid.uuid4().hex}"
    try:
        order = await paypal.create_order(
            intent=intent, amount=amount, currency=currency, request_id=request_id
        )
    except PayPalError as exc:
        print(f"Create order REJECTED by PayPal: HTTP {exc.status_code}")
        print(f"  issue={exc.issue} name={exc.error_name} debug_id={exc.debug_id}")
        if currency != "USD":
            print(f"  -> {currency} is NOT usable in the sandbox as sent. Use USD in the demo.")
        return 1
    except (PayPalOutcomeUnknown, PayPalNotSent) as exc:
        print(f"Create order failed ({type(exc).__name__}): {exc}")
        return 1

    order_id = order["id"]
    print(f"Created order: id={order_id} status={order.get('status')} ({currency} {amount})")
    if currency != "USD":
        print(f"  -> {currency} WORKS in the sandbox.")
    print(f"Approval link: {approval_link(order)}")

    fetched = await paypal.get_order(order_id)
    ok = fetched.get("id") == order_id
    print(f"Read back: id={fetched.get('id')} status={fetched.get('status')} match={ok}")
    return 0 if ok else 1


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--amount", default="10.00")
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--intent", default="CAPTURE", choices=["CAPTURE", "AUTHORIZE"])
    args = parser.parse_args()
    try:
        async with PayPalClient() as paypal:
            return await run(
                paypal,
                amount=args.amount,
                currency=args.currency.upper(),
                intent=args.intent,
            )
    except ValueError as exc:
        print(f"Config/input error: {exc}")
        return 2
    except (PayPalError, PayPalNotSent, PayPalOutcomeUnknown) as exc:
        print(f"PayPal call failed ({type(exc).__name__}): {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
