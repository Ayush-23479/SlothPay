"""REAL PayPal sandbox check with a human in the loop. Skipped unless you opt in.

A real buyer has to approve the payment in a browser, so this test prints the approval link and
waits for you to press Enter. It talks only to PayPal's sandbox (PayPalClient refuses any other
host), uses sandbox credentials from your .env, and never touches your database.

    SLOTHPAY_RUN_SANDBOX=1 python -m pytest tests/e2e/test_sandbox_manual.py -s -p no:cacheprovider

Steps it performs: create one 1.00 USD order, print the link, wait for Enter after you approve
with a sandbox buyer account, read the order back from PayPal, capture it once with a stable
request id, repeat the same capture request, then read the capture back and compare.

What it proves: the same capture request id returns the same capture (PayPal does not capture
twice) and the amounts match. What it does not prove: anything about the SlothPay database or
workflows. Report results from this test as REAL SANDBOX results, separate from simulator runs.
"""

from __future__ import annotations

import os
import uuid

import pytest

from backend.config.settings import get_settings
from backend.payments.paypal_client import PayPalClient

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_SANDBOX") != "1",
        reason="Real sandbox test: set SLOTHPAY_RUN_SANDBOX=1 and sandbox credentials",
    ),
]


async def test_real_sandbox_one_order_one_capture_with_human_approval():
    settings = get_settings()
    if not settings.PAYPAL_CLIENT_ID or settings.PAYPAL_CLIENT_ID.startswith("your-"):
        pytest.skip("Put real PayPal SANDBOX credentials in .env first")

    request_prefix = f"manual-{uuid.uuid4().hex[:12]}"
    async with PayPalClient() as paypal:
        assert await paypal.check_authentication() is True

        order = await paypal.create_order(
            intent="CAPTURE",
            amount="1.00",
            currency="USD",
            request_id=f"{request_prefix}-order",
            return_url="https://example.com/return",
        )
        order_id = order["id"]
        link = next(
            (
                item["href"]
                for item in order.get("links", [])
                if item["rel"] in {"payer-action", "approve"}
            ),
            f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}",
        )
        print("\n" + "=" * 70)
        print("REAL PAYPAL SANDBOX. Open this link and approve with a SANDBOX buyer account:")
        print(link)
        print("=" * 70)
        input("Press Enter here after you have approved the payment... ")

        approved = await paypal.get_order(order_id)
        assert approved["status"] == "APPROVED", f"order is {approved['status']}, not APPROVED"

        capture_request = f"{request_prefix}-capture"
        first = await paypal.capture_order(order_id, request_id=capture_request)
        again = await paypal.capture_order(order_id, request_id=capture_request)

        def capture_of(document):
            return document["purchase_units"][0]["payments"]["captures"][0]

        assert capture_of(first)["id"] == capture_of(again)["id"], "same request id, two captures!"
        final = await paypal.get_order(order_id)
        assert final["status"] == "COMPLETED"
        assert len(final["purchase_units"][0]["payments"]["captures"]) == 1
        assert capture_of(final)["amount"]["value"] == "1.00"
        print(f"REAL SANDBOX RESULT: order {order_id} captured once as {capture_of(final)['id']}")