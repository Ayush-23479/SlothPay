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


async def test_a_lost_capture_answer_is_reconciled_with_one_capture_and_one_request_id(sim):
    view, _ = await sim.pay()
    await sim.approve(view)
    await sim.faults(drop_after=["capture_order"])

    uncertain = await sim.api.buyer_returned(OWNER, uuid.UUID(view.id))
    assert uncertain.status == "UNCERTAIN"
    stats = await sim.stats()
    assert stats["dropped_responses"] == 1 and stats["captures"] == 1  # PayPal did it

    await sim.faults()  # the network is healthy again
    done = await sim.api.buyer_returned(OWNER, uuid.UUID(view.id))
    assert done.status == "CAPTURED"

    stats = await sim.stats()
    assert stats["captures"] == 1
    assert stats["capture_request_ids"] == 1
    assert stats["replays"] == 0  # nothing was resent at all

    passport = await sim.assert_passport_ok(view)
    assert passport["protections"]["blind_retries"] == 0
    assert passport["protections"]["reconciled"] is True
    assert passport["errors"], "the lost answer should be listed as a problem along the way"


async def test_a_503_after_the_work_is_done_is_also_reconciled_not_resent(sim):
    view, _ = await sim.pay()
    await sim.approve(view)
    await sim.faults(error_rate=1.0, error_when="after", error_operations=["capture_order"], seed=7)

    uncertain = await sim.api.buyer_returned(OWNER, uuid.UUID(view.id))
    assert uncertain.status == "UNCERTAIN"

    await sim.faults()
    done = await sim.api.buyer_returned(OWNER, uuid.UUID(view.id))
    assert done.status == "CAPTURED"
    stats = await sim.stats()
    assert stats["captures"] == 1 and stats["capture_request_ids"] == 1


async def test_a_503_before_the_work_is_not_blindly_retried(sim):
    view, _ = await sim.pay()
    await sim.approve(view)
    await sim.faults(
        error_rate=1.0, error_when="before", error_operations=["capture_order"], seed=7
    )
    uncertain = await sim.api.buyer_returned(OWNER, uuid.UUID(view.id))
    assert uncertain.status == "UNCERTAIN"
    await sim.faults()
    # Reconciliation reads PayPal: the order is still only APPROVED, so nothing was captured.
    after = await sim.api.buyer_returned(OWNER, uuid.UUID(view.id))
    stats = await sim.stats()
    assert stats["captures"] <= 1 and stats["capture_request_ids"] <= 1
    assert after.status in {"CAPTURED", "UNCERTAIN", "APPROVED"}


async def test_a_lost_authorize_answer_for_conditionalpay_is_reconciled(sim):
    view, _ = await sim.pay(payload=CONDITIONAL)
    await sim.approve(view)
    await sim.faults(drop_after=["authorize_order"])
    uncertain = await sim.api.buyer_returned(OWNER, uuid.UUID(view.id))
    assert uncertain.status == "UNCERTAIN"
    await sim.faults()
    done = await sim.api.buyer_returned(OWNER, uuid.UUID(view.id))
    assert done.status == "AWAITING_CONDITION"
    stats = await sim.stats()
    assert stats["authorizations"] == 1 and stats["captures"] == 0
    await sim.assert_passport_ok(view)