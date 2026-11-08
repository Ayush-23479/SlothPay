import json
import os
import uuid

import pytest
from sqlalchemy import select

from backend.models.audit import PaymentAudit
from backend.models.payment import Payment
from backend.webhooks.handler import (
    APPLIED,
    DUPLICATE,
    IGNORED,
    REJECTED,
    WebhookDeps,
    process_webhook,
)
from tests.integration.sim_support import OWNER

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1",
        reason="Set SLOTHPAY_RUN_DB_TESTS=1 for local PostgreSQL tests",
    ),
]


def event(kind, order_id, amount="10.00"):
    body = {
        "id": f"WH-{uuid.uuid4().hex}",
        "event_type": kind,
        "resource": {
            "id": f"RES-{uuid.uuid4().hex[:10]}",
            "status": "COMPLETED",
            "amount": {"value": amount, "currency_code": "USD"},
            "supplementary_data": {"related_ids": {"order_id": order_id}},
        },
    }
    return body, json.dumps(body).encode()


async def fire(sim, raw, *, verified):
    async def verify(headers, body):
        return verified

    return await process_webhook(
        {"paypal-transmission-id": "t"}, raw, WebhookDeps(session_factory=sim.maker, verify=verify)
    )


async def lost_answer_payment(sim):
    """A capture PayPal did, whose answer SlothPay lost: UNCERTAIN, waiting for evidence."""
    view, _ = await sim.pay()
    await sim.approve(view)
    await sim.faults(drop_after=["capture_order"])
    assert (await sim.api.buyer_returned(OWNER, uuid.UUID(view.id))).status == "UNCERTAIN"
    await sim.faults()
    return view, await sim.order_id(view)


async def payment_row(sim, view):
    async with sim.maker() as session:
        return await session.get(Payment, uuid.UUID(view.id), populate_existing=True)


async def transitions(sim, view):
    async with sim.maker() as session:
        rows = await session.scalars(
            select(PaymentAudit).where(
                PaymentAudit.payment_id == uuid.UUID(view.id),
                PaymentAudit.event_type == "STATE_TRANSITION",
            )
        )
        return len(list(rows))


async def test_a_forged_signature_changes_nothing(sim):
    view, order = await lost_answer_payment(sim)
    before, moves = await payment_row(sim, view), await transitions(sim, view)
    _, raw = event("PAYMENT.CAPTURE.COMPLETED", order)
    assert await fire(sim, raw, verified=False) == REJECTED
    after = await payment_row(sim, view)
    assert after.status == before.status == "UNCERTAIN"
    assert after.version == before.version
    assert await transitions(sim, view) == moves


async def test_the_same_event_twice_changes_the_status_once(sim):
    view, order = await lost_answer_payment(sim)
    _, raw = event("PAYMENT.CAPTURE.COMPLETED", order)
    assert await fire(sim, raw, verified=True) == APPLIED
    moves = await transitions(sim, view)
    assert await fire(sim, raw, verified=True) == DUPLICATE
    assert (await payment_row(sim, view)).status == "CAPTURED"
    assert await transitions(sim, view) == moves
    assert (await sim.stats())["captures"] == 1
    await sim.assert_passport_ok(view)


async def test_an_out_of_order_event_is_ignored_after_the_payment_is_final(sim):
    view, _ = await sim.pay()
    await sim.approve(view)
    assert (await sim.api.buyer_returned(OWNER, uuid.UUID(view.id))).status == "CAPTURED"
    order = await sim.order_id(view)
    moves = await transitions(sim, view)
    _, raw = event("PAYMENT.CAPTURE.PENDING", order)
    assert await fire(sim, raw, verified=True) == IGNORED
    assert (await payment_row(sim, view)).status == "CAPTURED"
    assert await transitions(sim, view) == moves
    await sim.assert_passport_ok(view)


async def test_a_forged_event_never_shows_up_as_confirmed_in_the_passport(sim):
    view, order = await lost_answer_payment(sim)
    _, raw = event("PAYMENT.CAPTURE.COMPLETED", order)
    await fire(sim, raw, verified=False)
    passport = await sim.assert_passport_ok(view)
    assert not any(entry["title"].startswith("Payment completed") for entry in passport["timeline"])