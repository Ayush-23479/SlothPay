import os
import uuid

import pytest

from tests.integration.sim_support import SMART

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1",
        reason="Set SLOTHPAY_RUN_DB_TESTS=1 for local PostgreSQL tests",
    ),
]


async def test_smartpay_approve_then_capture_makes_exactly_one_capture(sim):
    view, replay = await sim.pay()
    assert replay is False and view.status == "AWAITING_APPROVAL"
    assert (await sim.stats())["captures"] == 0

    await sim.approve(view)
    done = await sim.api.buyer_returned("owner-sim-a", uuid.UUID(view.id))

    assert done.status == "CAPTURED"
    stats = await sim.stats()
    assert stats["orders"] == 1 and stats["captures"] == 1 and stats["capture_request_ids"] == 1
    await sim.assert_passport_ok(view)


async def test_returning_twice_never_captures_twice(sim):
    view, _ = await sim.pay()
    await sim.approve(view)
    for _ in range(3):
        done = await sim.api.buyer_returned("owner-sim-a", uuid.UUID(view.id))
    assert done.status == "CAPTURED"
    assert (await sim.stats())["captures"] == 1


async def test_returning_before_the_buyer_approves_does_not_capture(sim):
    view, _ = await sim.pay()
    still = await sim.api.buyer_returned("owner-sim-a", uuid.UUID(view.id))
    assert still.status == "AWAITING_APPROVAL"
    assert (await sim.stats())["captures"] == 0


# ------------------------------------------------------------------ scenario 7: decline
async def test_a_declined_capture_is_never_retried_automatically(sim):
    """PayPal refuses the capture (INSTRUMENT_DECLINED). SlothPay must not resend it on its own,
    however many times the buyer returns, and no money must move."""
    view, _ = await sim.pay(payload={**SMART, "amount": "12.50"})
    await sim.approve(view)
    await sim.faults(decline_next_capture=1)

    first = await sim.api.buyer_returned("owner-sim-a", uuid.UUID(view.id))
    assert first.status == "UNCERTAIN"  # a PayPal error is never assumed to mean "not done"
    for _ in range(3):
        await sim.api.buyer_returned("owner-sim-a", uuid.UUID(view.id))

    stats = await sim.stats()
    assert stats["declines"] == 1
    assert stats["captures"] == 0
    assert stats["capture_request_ids"] == 1  # one capture request ever reached PayPal
    await sim.assert_passport_ok(view)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known gap, not fixed by Task 15: the lifecycle stores a PayPal 4xx only as the class "
        "name 'PayPalError' and drops the issue code, so reconciliation returns the payment to "
        "APPROVED and the rescue plan offers CAPTURE / RETRY_SAME_REQUEST. The plan wants a "
        "decline to offer only ASK_BUYER_NEW_METHOD. Remove this marker once that is built."
    ),
)
async def test_a_declined_capture_offers_only_ask_buyer_new_method(sim):
    view, _ = await sim.pay(payload={**SMART, "amount": "12.50"})
    await sim.approve(view)
    await sim.faults(decline_next_capture=1)
    await sim.api.buyer_returned("owner-sim-a", uuid.UUID(view.id))
    await sim.api.buyer_returned("owner-sim-a", uuid.UUID(view.id))
    plan = await sim.api.rescue_plan("owner-sim-a", uuid.UUID(view.id))
    assert plan.allowed_actions == ["ASK_BUYER_NEW_METHOD"]