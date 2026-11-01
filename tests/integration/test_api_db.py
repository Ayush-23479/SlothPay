import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fakeredis import FakeAsyncRedis
from sqlalchemy import select

from backend.api.deps import Caller, NotAllowed
from backend.api.payments_service import DbPaymentApi
from backend.models.attempt import PaymentAttempt
from backend.models.audit import PaymentAudit
from backend.models.payment import Payment
from backend.notifications.hub import InMemoryHub
from backend.payments.paypal_client import PayPalNotSent, PayPalOutcomeUnknown
from backend.reliability.idempotency import PayloadMismatch

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1",
        reason="Set SLOTHPAY_RUN_DB_TESTS=1 for local PostgreSQL tests",
    ),
]

OWNER = "owner-db-a"
OTHER = "owner-db-b"
SMART = {"path": "SMARTPAY", "amount": "10.00", "currency": "USD", "description": None}
CONDITIONAL = {
    "path": "CONDITIONALPAY",
    "amount": "20.00",
    "currency": "USD",
    "description": None,
    "condition": {"kind": "delivery", "description": "Parcel arrives", "deadline_minutes": 90},
}


def fake_paypal(order_id="ORDER-ABCDEFGH-1"):
    state = SimpleNamespace(order_status="CREATED")

    async def get_order(requested):
        doc = {"id": requested, "status": state.order_status}
        if state.order_status == "COMPLETED":
            doc["purchase_units"] = [
                {
                    "amount": {"value": "10.00", "currency_code": "USD"},
                    "payments": {
                        "captures": [
                            {
                                "id": "CAP-REAL-1",
                                "status": "COMPLETED",
                                "amount": {"value": "10.00", "currency_code": "USD"},
                            }
                        ]
                    },
                }
            ]
        return doc

    paypal = SimpleNamespace(
        state=state,
        create_order=AsyncMock(
            side_effect=lambda **_: {
                "id": f"{order_id}-{uuid.uuid4().hex[:6]}",
                "status": "CREATED",
            }
        ),
        get_order=AsyncMock(side_effect=get_order),
        authorize_order=AsyncMock(
            return_value={
                "purchase_units": [
                    {
                        "payments": {
                            "authorizations": [{"id": "AUTH-XYZ-12345", "status": "CREATED"}]
                        }
                    }
                ]
            }
        ),
        capture_order=AsyncMock(
            return_value={
                "purchase_units": [
                    {"payments": {"captures": [{"id": "CAP-REAL-1", "status": "COMPLETED"}]}}
                ]
            }
        ),
        capture_authorization=AsyncMock(return_value={"id": "CAP-AUTH-1", "status": "COMPLETED"}),
        get_authorization=AsyncMock(
            return_value={
                "status": "CREATED",
                "expiration_time": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            }
        ),
        void_authorization=AsyncMock(return_value={"http_status": 204}),
    )
    return paypal


@asynccontextmanager
async def _gateway_context(paypal):
    yield paypal


@pytest_asyncio.fixture
async def world(isolated_maker):
    paypal = fake_paypal()
    hub = InMemoryHub()
    starter = SimpleNamespace(start=AsyncMock(return_value=True))
    redis = FakeAsyncRedis(decode_responses=True)
    api = DbPaymentApi(
        session_factory=isolated_maker,
        redis=redis,
        hub=hub,
        paypal_factory=lambda: _gateway_context(paypal),
        starter=starter,
    )
    yield SimpleNamespace(api=api, paypal=paypal, hub=hub, starter=starter, maker=isolated_maker)
    await redis.aclose()


def pid(view):
    return uuid.UUID(view.id)


async def status_in_db(world, view):
    async with world.maker() as session:
        return (await session.get(Payment, pid(view))).status


async def audit_rows(world, view):
    async with world.maker() as session:
        rows = await session.scalars(
            select(PaymentAudit).where(PaymentAudit.payment_id == pid(view))
        )
        return list(rows)


async def checkout(world, key, payload=None, owner=OWNER):
    return await world.api.create_payment(owner, key, dict(payload or SMART))


# ------------------------------------------------------------------------------ checkout
async def test_checkout_ends_awaiting_approval_with_a_link(world):
    view, replay = await checkout(world, "key-db-0001")
    assert replay is False
    assert view.status == "AWAITING_APPROVAL" and view.display_status == "ACTION_NEEDED"
    assert view.approval_url.startswith("https://www.sandbox.paypal.com/checkoutnow?token=ORDER-")
    assert (
        view.paypal_ids.order_id.startswith("ORDE…")
        and "ORDER-ABCDEFGH" not in view.paypal_ids.model_dump_json()
    )
    assert view.amount == "10.00" and view.path == "SMARTPAY"
    assert view.allowed_actions == ["ASK_BUYER_TO_APPROVE"]
    world.paypal.create_order.assert_awaited_once()
    steps = [
        (row.from_state, row.to_state)
        for row in await audit_rows(world, view)
        if row.event_type == "STATE_TRANSITION"
    ]
    assert ("CREATED", "INTENT_CREATED") in steps and (
        "ORDER_CREATED",
        "AWAITING_APPROVAL",
    ) in steps


async def test_a_replay_returns_the_same_payment_and_creates_no_second_order(world):
    first, _ = await checkout(world, "key-db-0002")
    second, replay = await checkout(world, "key-db-0002")
    assert replay is True and second.id == first.id
    assert second.approval_url == first.approval_url
    world.paypal.create_order.assert_awaited_once()


async def test_a_different_payload_with_the_same_key_is_refused(world):
    await checkout(world, "key-db-0003")
    with pytest.raises(PayloadMismatch):
        await checkout(world, "key-db-0003", {**SMART, "amount": "11.00"})


async def test_a_bad_currency_is_a_value_error(world):
    with pytest.raises(ValueError):
        await checkout(world, "key-db-0004", {**SMART, "currency": "XXX"})


async def test_a_checkout_that_could_not_reach_paypal_is_saved_and_never_resent(world):
    world.paypal.create_order.side_effect = PayPalNotSent("connection refused")
    first, _ = await checkout(world, "key-db-0005")
    assert first.status == "INTENT_CREATED" and first.approval_url is None
    world.paypal.create_order.side_effect = None
    again, replay = await checkout(world, "key-db-0005")
    assert replay is True and again.id == first.id and again.approval_url is None
    assert world.paypal.create_order.await_count == 1, "the saved attempt is never replayed"


# ------------------------------------------------------------------------------- return
async def test_return_before_approval_changes_nothing(world):
    view, _ = await checkout(world, "key-db-0006")
    after = await world.api.buyer_returned(OWNER, pid(view))
    assert after.status == "AWAITING_APPROVAL"
    world.paypal.capture_order.assert_not_awaited()


async def test_return_after_approval_captures_exactly_once(world):
    view, _ = await checkout(world, "key-db-0007")
    world.paypal.state.order_status = "APPROVED"
    done = await world.api.buyer_returned(OWNER, pid(view))
    assert done.status == "CAPTURED" and done.display_status == "COMPLETED"
    assert done.paypal_ids.capture_id == "CAP-…AL-1" and done.approval_url is None
    again = await world.api.buyer_returned(OWNER, pid(view))
    assert again.status == "CAPTURED"
    world.paypal.capture_order.assert_awaited_once()


async def test_a_lost_capture_answer_is_reconciled_not_resent(world):
    view, _ = await checkout(world, "key-db-0008")
    world.paypal.state.order_status = "APPROVED"
    world.paypal.capture_order.side_effect = PayPalOutcomeUnknown("answer was lost")
    lost = await world.api.buyer_returned(OWNER, pid(view))
    assert lost.status == "UNCERTAIN" and lost.display_status == "PENDING"
    world.paypal.state.order_status = "COMPLETED"  # PayPal did take the payment
    settled = await world.api.buyer_returned(OWNER, pid(view))
    assert settled.status == "CAPTURED"
    world.paypal.capture_order.assert_awaited_once()


async def test_two_returns_at_the_same_moment_capture_once(world):
    import asyncio

    view, _ = await checkout(world, "key-db-0009")
    world.paypal.state.order_status = "APPROVED"
    await asyncio.gather(*(world.api.buyer_returned(OWNER, pid(view)) for _ in range(4)))
    assert await status_in_db(world, view) == "CAPTURED"
    world.paypal.capture_order.assert_awaited_once()


# ---------------------------------------------------------------------------- conditional
async def held(world, key):
    view, _ = await checkout(world, key, CONDITIONAL)
    world.paypal.state.order_status = "APPROVED"
    return await world.api.buyer_returned(OWNER, pid(view))


async def test_conditional_return_holds_the_funds_and_starts_the_timer(world):
    view = await held(world, "key-db-0010")
    assert view.status == "AWAITING_CONDITION" and view.path == "CONDITIONALPAY"
    assert view.paypal_ids.authorization_id == "AUTH…2345"
    world.paypal.capture_authorization.assert_not_awaited()
    world.starter.start.assert_awaited_once()
    started_id, deadline = world.starter.start.await_args.args
    assert started_id == view.id
    minutes = (datetime.fromisoformat(deadline) - datetime.now(UTC)).total_seconds() / 60
    assert 85 < minutes <= 90, "the deadline is the condition's own deadline_minutes"


async def test_a_timer_that_cannot_start_is_recorded_for_operators(world):
    world.starter.start.side_effect = ConnectionError("temporal is down")
    view = await held(world, "key-db-0011")
    assert view.status == "AWAITING_CONDITION"
    types = [row.event_type for row in await audit_rows(world, view)]
    assert "CONDITION_WORKFLOW_NOT_STARTED" in types


async def test_verify_true_captures_once(world):
    view = await held(world, "key-db-0012")
    done = await world.api.verify_condition(Caller(OWNER), pid(view), True, {"note": "delivered"})
    assert done.status == "CAPTURED"
    await world.api.verify_condition(Caller(OWNER), pid(view), True, {})
    world.paypal.capture_authorization.assert_awaited_once()
    types = [row.event_type for row in await audit_rows(world, view)]
    assert types.count("CONDITION_VERIFY_REQUESTED") == 2


async def test_verify_false_voids_and_never_captures(world):
    view = await held(world, "key-db-0013")
    done = await world.api.verify_condition(Caller(OWNER), pid(view), False, {})
    assert done.status == "VOIDED" and done.display_status == "CLOSED"
    world.paypal.void_authorization.assert_awaited_once()
    world.paypal.capture_authorization.assert_not_awaited()


async def test_the_first_answer_stands(world):
    view = await held(world, "key-db-0014")
    await world.api.verify_condition(Caller(OWNER), pid(view), False, {})
    later = await world.api.verify_condition(Caller(OWNER), pid(view), True, {})
    assert later.status == "VOIDED"
    world.paypal.capture_authorization.assert_not_awaited()


async def test_an_admin_may_verify_but_a_stranger_may_not(world):
    view = await held(world, "key-db-0015")
    assert await world.api.verify_condition(Caller(OTHER), pid(view), True, {}) is None
    assert await status_in_db(world, view) == "AWAITING_CONDITION"
    admin = await world.api.verify_condition(Caller(None, True), pid(view), True, {})
    assert admin.status == "CAPTURED"


async def test_verifying_a_smartpay_payment_is_refused(world):
    view, _ = await checkout(world, "key-db-0016")
    with pytest.raises(NotAllowed) as refused:
        await world.api.verify_condition(Caller(OWNER), pid(view), True, {})
    assert refused.value.error == "not_conditional"


async def test_verifying_before_the_funds_are_held_is_refused_not_guessed(world):
    view, _ = await checkout(world, "key-db-0017", CONDITIONAL)
    with pytest.raises(NotAllowed) as refused:
        await world.api.verify_condition(Caller(OWNER), pid(view), True, {})
    assert refused.value.error == "condition_not_ready"
    world.paypal.capture_authorization.assert_not_awaited()
    world.paypal.capture_order.assert_not_awaited()
    assert await status_in_db(world, view) == "AWAITING_APPROVAL"


# ------------------------------------------------------------------------------- cancel
async def test_cancel_before_approval(world):
    view, _ = await checkout(world, "key-db-0018")
    done = await world.api.cancel(OWNER, pid(view))
    assert done.status == "CANCELLED" and done.display_status == "CLOSED"
    assert done.approval_url is None


async def test_cancel_after_capture_is_refused(world):
    view, _ = await checkout(world, "key-db-0019")
    world.paypal.state.order_status = "APPROVED"
    await world.api.buyer_returned(OWNER, pid(view))
    with pytest.raises(NotAllowed):
        await world.api.cancel(OWNER, pid(view))
    assert await status_in_db(world, view) == "CAPTURED"


async def test_cancelling_held_funds_voids_them(world):
    view = await held(world, "key-db-0020")
    done = await world.api.cancel(OWNER, pid(view))
    assert done.status == "VOIDED"
    world.paypal.void_authorization.assert_awaited_once()
    world.paypal.capture_authorization.assert_not_awaited()


async def test_cancel_of_someone_elses_payment_is_none(world):
    view, _ = await checkout(world, "key-db-0021")
    assert await world.api.cancel(OTHER, pid(view)) is None
    assert await status_in_db(world, view) == "AWAITING_APPROVAL"


# ------------------------------------------------------------- reads: ownership and paging
async def test_reads_are_owner_only(world):
    view, _ = await checkout(world, "key-db-0022")
    assert (await world.api.get_payment(OWNER, pid(view))).id == view.id
    assert await world.api.get_payment(OTHER, pid(view)) is None
    assert await world.api.list_events(OTHER, pid(view), None, 10) is None
    assert await world.api.latest_event_cursor(OTHER, pid(view)) is None
    assert await world.api.rescue_plan(OTHER, pid(view)) is None
    assert await world.api.rescue_action(OTHER, pid(view), "WAIT") is None
    assert await world.api.buyer_returned(OTHER, pid(view)) is None


async def test_the_list_pages_newest_first_without_gaps_or_repeats(world):
    def order(n):
        return {**SMART, "merchant_order_id": f"order-page-{n}"}

    created = [(await checkout(world, f"key-db-page-{n}", order(n)))[0].id for n in range(5)]
    await checkout(world, "key-db-page-other", order(99), owner=OTHER)
    seen: list[str] = []
    cursor = None
    while True:
        items, cursor = await world.api.list_payments(OWNER, 2, cursor)
        seen.extend(item.id for item in items)
        if cursor is None:
            break
    assert sorted(seen) == sorted(created) and len(seen) == len(set(seen)) == 5
    assert seen == list(reversed(created)), "newest first"


async def test_events_follow_the_cursor_in_order(world):
    view, _ = await checkout(world, "key-db-0023")
    events = await world.api.list_events(OWNER, pid(view), None, 100)
    assert [e.created_at for e in events] == sorted(e.created_at for e in events)
    assert events[0].type == "INTENT_CREATED"
    assert {e.title for e in events} >= {"Payment started", "PayPal order created"}
    assert await world.api.list_events(OWNER, pid(view), events[-1].id, 100) == []
    rest = await world.api.list_events(OWNER, pid(view), events[0].id, 100)
    assert [e.id for e in rest] == [e.id for e in events[1:]]
    assert await world.api.latest_event_cursor(OWNER, pid(view)) == events[-1].id
    for event in events:
        assert "ORDER-ABCDEFGH" not in event.model_dump_json()


async def test_events_with_the_same_timestamp_are_not_skipped(world):
    view, _ = await checkout(world, "key-db-0024")
    async with world.maker() as session:
        stamp = datetime.now(UTC)
        for _ in range(3):
            session.add(
                PaymentAudit(
                    payment_id=pid(view),
                    event_type="SAME_INSTANT",
                    actor="test",
                    evidence={},
                    created_at=stamp,
                )
            )
        await session.commit()
    everything = await world.api.list_events(OWNER, pid(view), None, 100)
    tied = [e for e in everything if e.type == "SAME_INSTANT"]
    assert len(tied) == 3
    # Walking one event at a time through the cursor must visit every one exactly once.
    walked, cursor = [], None
    while True:
        batch = await world.api.list_events(OWNER, pid(view), cursor, 1)
        if not batch:
            break
        walked.append(batch[0].id)
        cursor = batch[0].id
    assert walked == [e.id for e in everything]


# ---------------------------------------------------------------------- rescue + live
async def test_rescue_plan_and_a_harmless_action(world):
    view, _ = await checkout(world, "key-db-0025")
    plan = await world.api.rescue_plan(OWNER, pid(view))
    assert plan.payment_id == view.id and plan.allowed_actions
    action = plan.allowed_actions[0]
    outcome = await world.api.rescue_action(OWNER, pid(view), action)
    assert outcome["action"] == action and isinstance(outcome["result"], dict)
    with pytest.raises(NotAllowed):
        await world.api.rescue_action(OWNER, pid(view), "CAPTURE")
    world.paypal.capture_order.assert_not_awaited()


async def test_changes_wake_the_live_subscribers(world):
    view, _ = await checkout(world, "key-db-0026")
    async with world.hub.subscription(view.id) as sub:
        world.paypal.state.order_status = "APPROVED"
        await world.api.buyer_returned(OWNER, pid(view))
        assert await sub.next(1) == {"type": "payment.updated"}


async def test_money_attempts_all_carry_stable_request_ids(world):
    view = await held(world, "key-db-0027")
    await world.api.verify_condition(Caller(OWNER), pid(view), True, {})
    async with world.maker() as session:
        attempts = list(
            await session.scalars(
                select(PaymentAttempt).where(PaymentAttempt.payment_id == pid(view))
            )
        )
    operations = sorted(a.operation for a in attempts)
    assert operations == ["authorize", "capture", "create_order"]
    assert len({a.request_id for a in attempts}) == 3