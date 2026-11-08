import os
import uuid

import pytest

from backend.api.deps import Caller
from tests.integration.sim_support import CONDITIONAL, OWNER

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1",
        reason="Set SLOTHPAY_RUN_DB_TESTS=1 for local PostgreSQL tests",
    ),
]


async def held_payment(sim):
    view, _ = await sim.pay(payload=CONDITIONAL)  # deadline_minutes = 1, the shortest allowed
    await sim.approve(view)
    held = await sim.api.buyer_returned(OWNER, uuid.UUID(view.id))
    assert held.status == "AWAITING_CONDITION"
    return view


async def test_a_failed_condition_ends_voided_with_zero_captures(sim):
    view = await held_payment(sim)
    done = await sim.api.verify_condition(
        Caller(owner_id=OWNER, is_admin=False), uuid.UUID(view.id), False, {"checked": "no parcel"}
    )
    assert done.status == "VOIDED"
    stats = await sim.stats()
    assert stats["captures"] == 0 and stats["voids"] == 1
    passport = await sim.assert_passport_ok(view)
    assert passport["condition"]["status"] == "FAILED"


async def test_no_signal_until_the_deadline_ends_voided_with_zero_captures(sim):
    view = await held_payment(sim)
    await sim.finish_condition(view, verified=False, reason="deadline")
    assert await sim.status(view) == "VOIDED"
    stats = await sim.stats()
    assert stats["captures"] == 0 and stats["voids"] == 1
    passport = await sim.assert_passport_ok(view)
    assert passport["condition"]["status"] == "EXPIRED"


async def test_the_timer_firing_twice_voids_only_once(sim):
    view = await held_payment(sim)
    await sim.finish_condition(view, verified=False, reason="deadline")
    await sim.finish_condition(view, verified=False, reason="deadline")
    assert (await sim.stats())["voids"] == 1


async def test_a_late_verification_after_expiry_cannot_capture(sim):
    view = await held_payment(sim)
    await sim.finish_condition(view, verified=False, reason="deadline")
    await sim.finish_condition(view, verified=True)
    assert await sim.status(view) == "VOIDED"
    assert (await sim.stats())["captures"] == 0