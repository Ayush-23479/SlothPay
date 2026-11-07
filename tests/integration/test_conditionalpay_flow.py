import os
import uuid

import pytest

from tests.integration.sim_support import CONDITIONAL, OWNER

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1",
        reason="Set SLOTHPAY_RUN_DB_TESTS=1 for local PostgreSQL tests",
    ),
]


async def test_conditionalpay_authorizes_without_capturing_then_captures_once_verified(sim):
    view, _ = await sim.pay(payload=CONDITIONAL)
    await sim.approve(view)
    held = await sim.api.buyer_returned(OWNER, uuid.UUID(view.id))

    assert held.status == "AWAITING_CONDITION"
    stats = await sim.stats()
    assert stats["authorizations"] == 1 and stats["captures"] == 0
    sim.starter.start.assert_awaited_once()  # the deadline timer was started

    from backend.api.deps import Caller

    done = await sim.api.verify_condition(
        Caller(owner_id=OWNER, is_admin=False), uuid.UUID(view.id), True, {"checked": "delivery"}
    )
    assert done.status == "CAPTURED"
    stats = await sim.stats()
    assert stats["captures"] == 1 and stats["capture_request_ids"] == 1 and stats["voids"] == 0

    passport = await sim.assert_passport_ok(view)
    assert passport["condition"] is not None
    assert passport["condition"]["status"] == "VERIFIED"


async def test_verifying_twice_still_captures_only_once(sim):
    from backend.api.deps import Caller

    view, _ = await sim.pay(payload=CONDITIONAL)
    await sim.approve(view)
    await sim.api.buyer_returned(OWNER, uuid.UUID(view.id))
    caller = Caller(owner_id=OWNER, is_admin=False)
    await sim.api.verify_condition(caller, uuid.UUID(view.id), True, {})
    again = await sim.api.verify_condition(caller, uuid.UUID(view.id), True, {})
    assert again.status == "CAPTURED"
    assert (await sim.stats())["captures"] == 1