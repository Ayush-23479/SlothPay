

import asyncio
import json
import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config.settings import get_settings
from backend.models.attempt import PaymentAttempt
from backend.models.audit import PaymentAudit
from backend.models.event import ProviderEvent
from backend.models.payment import Payment
from backend.webhooks import receiver
from backend.webhooks.handler import (
    APPLIED,
    DUPLICATE,
    IGNORED,
    REJECTED,
    VERIFICATION_UNAVAILABLE,
    WebhookDeps,
    process_webhook,
)
from backend.webhooks.verification import WebhookVerificationUnavailable

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1",
        reason="Opt in to disposable LOCAL PostgreSQL integration tests",
    ),
]

HEADERS = {"paypal-transmission-id": "t"}


@pytest_asyncio.fixture
async def maker():
    url = get_settings().DATABASE_URL
    if "localhost" not in url and "127.0.0.1" not in url:
        pytest.skip("Refusing to run committed integration tests against a remote database")
    engine = create_async_engine(url, pool_size=20, max_overflow=40)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def make_deps(maker, *, verified=True, publish=None):
    async def verify(headers, raw):
        if verified is None:
            raise WebhookVerificationUnavailable("down")
        return verified

    kwargs = {"publish": publish} if publish else {}
    return WebhookDeps(session_factory=maker, verify=verify, **kwargs)


async def seed(maker, *, mode="SMARTPAY", status="CAPTURE_REQUESTED", amount="15.00"):
    token = uuid.uuid4().hex
    async with maker() as session:
        row = Payment(
            merchant_id="task7-webhook-db-tests",
            merchant_order_id=f"order-{token}",
            intent_key=f"intent-{token}",
            amount=Decimal(amount),
            currency="USD",
            mode=mode,
            status=status,
            version=0,
            paypal_order_id=f"ORD-{token}",
        )
        session.add(row)
        await session.commit()
        return row.id, row.paypal_order_id


async def add_attempt(maker, payment_id, operation, status, operation_id=None):
    async with maker() as session:
        session.add(
            PaymentAttempt(
                payment_id=payment_id,
                provider="paypal",
                operation=operation,
                request_id=f"req-{uuid.uuid4().hex}",
                status=status,
                provider_operation_id=operation_id,
            )
        )
        await session.commit()


def webhook(event_type, order_id, *, resource_id=None, amount="15.00", event_id=None, **extra):
    rid = resource_id or f"RES-{uuid.uuid4().hex[:12]}"
    resource = {
        "id": rid,
        "status": "COMPLETED",
        "amount": {"value": amount, "currency_code": "USD"},
        "supplementary_data": {"related_ids": {"order_id": order_id}},
        "payer": {"email_address": "buyer@example.com", "name": {"given_name": "Ada"}},
    }
    resource.update(extra)
    if event_type.startswith("CHECKOUT.ORDER."):
        resource = {"id": order_id, "purchase_units": [{"amount": resource["amount"]}]}
    event = {"id": event_id or f"WH-{uuid.uuid4().hex}", "event_type": event_type}
    event["resource"] = resource
    return event, json.dumps(event).encode()


async def fire(maker, raw, **deps_kwargs):
    return await process_webhook(HEADERS, raw, make_deps(maker, **deps_kwargs))


async def get_payment(maker, payment_id):
    async with maker() as session:
        return await session.get(Payment, payment_id)


async def audit_types(maker, payment_id):
    async with maker() as session:
        rows = await session.scalars(
            select(PaymentAudit.event_type).where(PaymentAudit.payment_id == payment_id)
        )
        return list(rows)


async def events_for(maker, event_id):
    async with maker() as session:
        return list(
            await session.scalars(select(ProviderEvent).where(ProviderEvent.event_id == event_id))
        )


# ----------------------------------------------------------- the four "done when" rules
async def test_a_forged_webhook_is_stored_unverified_and_changes_nothing(maker):
    pid, order = await seed(maker)
    event, raw = webhook("PAYMENT.CAPTURE.COMPLETED", order)
    assert await fire(maker, raw, verified=False) == REJECTED
    payment = await get_payment(maker, pid)
    assert payment.status == "CAPTURE_REQUESTED" and payment.version == 0
    assert await audit_types(maker, pid) == []
    async with maker() as session:
        stored = (
            await session.scalars(
                select(ProviderEvent).where(
                    ProviderEvent.payload["claimed_event_id"].as_string() == event["id"]
                )
            )
        ).all()
    assert len(stored) == 1
    assert stored[0].verified is False and stored[0].event_id.startswith("unverified:")


async def test_a_forged_event_reusing_a_real_event_id_does_not_block_the_real_one(maker):
    pid, order = await seed(maker)
    event, raw = webhook("PAYMENT.CAPTURE.COMPLETED", order)
    assert await fire(maker, raw, verified=False) == REJECTED  # attacker goes first
    assert await fire(maker, raw, verified=True) == APPLIED  # the genuine delivery still works
    assert (await get_payment(maker, pid)).status == "CAPTURED"


async def test_the_same_event_id_twice_changes_the_status_once(maker):
    pid, order = await seed(maker)
    event, raw = webhook("PAYMENT.CAPTURE.COMPLETED", order)
    assert await fire(maker, raw) == APPLIED
    assert await fire(maker, raw) == DUPLICATE
    payment = await get_payment(maker, pid)
    assert payment.status == "CAPTURED" and payment.version == 1
    types = await audit_types(maker, pid)
    assert types.count("STATE_TRANSITION") == 1
    assert types.count("DUPLICATE_SUPPRESSED") == 1
    assert len(await events_for(maker, event["id"])) == 1


async def test_an_out_of_order_event_is_ignored_and_audited(maker):
    pid, order = await seed(maker, status="CAPTURED")
    _, raw = webhook("PAYMENT.CAPTURE.COMPLETED", order)
    assert await fire(maker, raw) == IGNORED
    _, pending = webhook("PAYMENT.CAPTURE.PENDING", order)
    assert await fire(maker, pending) == IGNORED
    payment = await get_payment(maker, pid)
    assert payment.status == "CAPTURED" and payment.version == 0
    assert (await audit_types(maker, pid)).count("WEBHOOK_IGNORED") == 2


# ------------------------------------------------------------------ the other rules
async def test_an_unknown_event_type_is_stored_and_ignored(maker):
    pid, order = await seed(maker)
    event, raw = webhook("BILLING.PLAN.CREATED", order)
    assert await fire(maker, raw) == IGNORED
    assert (await get_payment(maker, pid)).status == "CAPTURE_REQUESTED"
    assert len(await events_for(maker, event["id"])) == 1


async def test_a_completed_event_resolves_an_uncertain_capture(maker):
    pid, order = await seed(maker, status="UNCERTAIN")
    await add_attempt(maker, pid, "capture", "UNCERTAIN")
    _, raw = webhook("PAYMENT.CAPTURE.COMPLETED", order, resource_id="CAP-RESOLVED")
    assert await fire(maker, raw) == APPLIED
    assert (await get_payment(maker, pid)).status == "CAPTURED"
    async with maker() as session:
        attempt = await session.scalar(
            select(PaymentAttempt).where(PaymentAttempt.payment_id == pid)
        )
    assert attempt.status == "SUCCEEDED" and attempt.provider_operation_id == "CAP-RESOLVED"


async def test_an_uncertain_payment_is_not_resolved_by_the_wrong_kind_of_event(maker):
    pid, order = await seed(maker, status="UNCERTAIN")
    await add_attempt(maker, pid, "capture", "UNCERTAIN")
    _, raw = webhook("PAYMENT.AUTHORIZATION.CREATED", order)
    assert await fire(maker, raw) == IGNORED
    assert (await get_payment(maker, pid)).status == "UNCERTAIN"


async def test_an_uncertain_payment_with_two_open_attempts_is_left_for_a_human(maker):
    pid, order = await seed(maker, status="UNCERTAIN")
    await add_attempt(maker, pid, "capture", "UNCERTAIN")
    await add_attempt(maker, pid, "void", "STARTED")
    _, raw = webhook("PAYMENT.CAPTURE.COMPLETED", order)
    assert await fire(maker, raw) == IGNORED
    assert (await get_payment(maker, pid)).status == "UNCERTAIN"


async def test_a_denied_capture_fails_the_payment_and_pending_then_completed_works(maker):
    pid, order = await seed(maker)
    _, denied = webhook("PAYMENT.CAPTURE.DENIED", order)
    assert await fire(maker, denied) == APPLIED
    assert (await get_payment(maker, pid)).status == "FAILED"

    pid2, order2 = await seed(maker)
    _, pending = webhook("PAYMENT.CAPTURE.PENDING", order2)
    _, done = webhook("PAYMENT.CAPTURE.COMPLETED", order2)
    assert await fire(maker, pending) == APPLIED
    assert (await get_payment(maker, pid2)).status == "CAPTURE_PENDING"
    assert await fire(maker, done) == APPLIED
    assert (await get_payment(maker, pid2)).status == "CAPTURED"


# ---------------------------------------------------------- buyer approval and conditional
@pytest.mark.parametrize("start", ["AWAITING_APPROVAL", "ORDER_CREATED"])
async def test_order_approved_moves_the_payment_to_approved(maker, start):
    pid, order = await seed(maker, status=start)
    _, raw = webhook("CHECKOUT.ORDER.APPROVED", order)
    assert await fire(maker, raw) == APPLIED
    assert (await get_payment(maker, pid)).status == "APPROVED"


async def test_order_approved_after_approval_is_ignored(maker):
    pid, order = await seed(maker, status="APPROVED")
    _, raw = webhook("CHECKOUT.ORDER.APPROVED", order)
    assert await fire(maker, raw) == IGNORED
    assert (await get_payment(maker, pid)).status == "APPROVED"


async def test_authorization_created_waits_for_the_condition_and_stores_the_auth_id(maker):
    pid, order = await seed(maker, mode="CONDITIONALPAY", status="AUTHORIZE_REQUESTED")
    await add_attempt(maker, pid, "authorize", "STARTED")
    _, raw = webhook("PAYMENT.AUTHORIZATION.CREATED", order, resource_id="AUTH-1")
    assert await fire(maker, raw) == APPLIED
    payment = await get_payment(maker, pid)
    assert payment.status == "AWAITING_CONDITION" and payment.version == 2
    async with maker() as session:
        attempt = await session.scalar(
            select(PaymentAttempt).where(PaymentAttempt.payment_id == pid)
        )
    assert attempt.status == "SUCCEEDED" and attempt.provider_operation_id == "AUTH-1"


async def test_authorization_created_resolves_an_uncertain_authorize(maker):
    pid, order = await seed(maker, mode="CONDITIONALPAY", status="UNCERTAIN")
    await add_attempt(maker, pid, "authorize", "UNCERTAIN")
    _, raw = webhook("PAYMENT.AUTHORIZATION.CREATED", order, resource_id="AUTH-2")
    assert await fire(maker, raw) == APPLIED
    assert (await get_payment(maker, pid)).status == "AWAITING_CONDITION"


async def test_authorization_voided_completes_a_requested_void(maker):
    pid, order = await seed(maker, mode="CONDITIONALPAY", status="VOID_REQUESTED")
    await add_attempt(maker, pid, "void", "STARTED")
    _, raw = webhook("PAYMENT.AUTHORIZATION.VOIDED", order, resource_id="AUTH-3")
    assert await fire(maker, raw) == APPLIED
    assert (await get_payment(maker, pid)).status == "VOIDED"


async def test_authorization_events_on_a_smartpay_payment_are_ignored(maker):
    pid, order = await seed(maker, mode="SMARTPAY", status="CAPTURE_REQUESTED")
    _, raw = webhook("PAYMENT.AUTHORIZATION.CREATED", order)
    assert await fire(maker, raw) == IGNORED
    assert (await get_payment(maker, pid)).status == "CAPTURE_REQUESTED"


# ---------------------------------------------------------------- identity and amounts
async def test_a_different_amount_is_never_applied(maker):
    pid, order = await seed(maker, amount="15.00")
    _, raw = webhook("PAYMENT.CAPTURE.COMPLETED", order, amount="1.00")
    assert await fire(maker, raw) == IGNORED
    assert (await get_payment(maker, pid)).status == "CAPTURE_REQUESTED"


async def test_a_capture_event_without_an_amount_is_never_applied(maker):
    pid, order = await seed(maker)
    event, _ = webhook("PAYMENT.CAPTURE.COMPLETED", order)
    del event["resource"]["amount"]
    assert await fire(maker, json.dumps(event).encode()) == IGNORED
    assert (await get_payment(maker, pid)).status == "CAPTURE_REQUESTED"


async def test_an_event_for_an_unknown_order_changes_nothing(maker):
    pid, _ = await seed(maker)
    _, raw = webhook("PAYMENT.CAPTURE.COMPLETED", "ORD-DOES-NOT-EXIST")
    assert await fire(maker, raw) == IGNORED
    assert (await get_payment(maker, pid)).status == "CAPTURE_REQUESTED"


async def test_the_payment_is_found_by_capture_id_when_the_event_has_no_order_id(maker):
    pid, _ = await seed(maker, status="CAPTURE_PENDING")
    capture_id = f"CAP-{uuid.uuid4().hex}"
    await add_attempt(maker, pid, "capture", "SUCCEEDED", operation_id=capture_id)
    event, _ = webhook("PAYMENT.CAPTURE.COMPLETED", "x", resource_id=capture_id)
    del event["resource"]["supplementary_data"]
    assert await fire(maker, json.dumps(event).encode()) == APPLIED
    assert (await get_payment(maker, pid)).status == "CAPTURED"


# ------------------------------------------------------- storage, notify, availability
async def test_the_stored_payload_is_masked_and_verify_ms_is_kept(maker):
    pid, order = await seed(maker)
    event, raw = webhook("PAYMENT.CAPTURE.COMPLETED", order)
    assert await fire(maker, raw) == APPLIED
    (row,) = await events_for(maker, event["id"])
    stored = json.dumps(row.payload)
    assert "buyer@example.com" not in stored and "Ada" not in stored
    assert row.verified is True and row.verify_ms is not None and row.verify_ms >= 0
    assert row.payment_id == pid


async def test_a_notification_is_published_only_when_something_changed(maker):
    published = []
    pid, order = await seed(maker)
    _, raw = webhook("PAYMENT.CAPTURE.COMPLETED", order)
    await fire(maker, raw, publish=lambda i, e: published.append((i, e)))
    await fire(maker, raw, publish=lambda i, e: published.append((i, e)))  # duplicate
    assert len(published) == 1
    assert published[0][0] == str(pid) and published[0][1]["status"] == "CAPTURED"


async def test_a_failing_notifier_does_not_undo_the_payment_fact(maker):
    async def broken(intent_id, event):
        raise RuntimeError("hub down")

    pid, order = await seed(maker)
    _, raw = webhook("PAYMENT.CAPTURE.COMPLETED", order)
    assert await fire(maker, raw, publish=broken) == APPLIED
    assert (await get_payment(maker, pid)).status == "CAPTURED"


async def test_paypal_verification_being_down_is_recorded_and_changes_nothing(maker):
    pid, order = await seed(maker)
    _, raw = webhook("PAYMENT.CAPTURE.COMPLETED", order)
    assert await fire(maker, raw, verified=None) == VERIFICATION_UNAVAILABLE
    assert (await get_payment(maker, pid)).status == "CAPTURE_REQUESTED"


async def test_garbage_bodies_are_survived(maker):
    assert await fire(maker, b"", verified=False) == REJECTED
    assert await fire(maker, b"\xff\xfe not json", verified=False) == REJECTED
    assert await fire(maker, b"[1,2,3]", verified=False) == REJECTED


async def test_50_simultaneous_deliveries_of_one_event_apply_it_once(maker):
    pid, order = await seed(maker)
    event, raw = webhook("PAYMENT.CAPTURE.COMPLETED", order)
    results = await asyncio.gather(*[fire(maker, raw) for _ in range(50)])
    assert results.count(APPLIED) == 1
    assert results.count(DUPLICATE) == 49
    payment = await get_payment(maker, pid)
    assert payment.status == "CAPTURED" and payment.version == 1
    assert len(await events_for(maker, event["id"])) == 1
    async with maker() as session:
        transitions = await session.scalar(
            select(func.count())
            .select_from(PaymentAudit)
            .where(PaymentAudit.payment_id == pid, PaymentAudit.event_type == "STATE_TRANSITION")
        )
    assert transitions == 1


async def test_two_different_events_racing_for_one_payment_leave_one_final_state(maker):
    pid, order = await seed(maker)
    _, done = webhook("PAYMENT.CAPTURE.COMPLETED", order)
    _, denied = webhook("PAYMENT.CAPTURE.DENIED", order)
    results = await asyncio.gather(fire(maker, done), fire(maker, denied))
    assert sorted(results) == [APPLIED, IGNORED]
    assert (await get_payment(maker, pid)).status in {"CAPTURED", "FAILED"}


# ------------------------------------------------------------------- through HTTP
async def test_a_real_http_post_moves_a_payment(maker):
    pid, order = await seed(maker)
    _, raw = webhook("PAYMENT.CAPTURE.COMPLETED", order)
    app = FastAPI()
    app.include_router(receiver.router)
    app.dependency_overrides[receiver.get_webhook_deps] = lambda: make_deps(maker)
    # The sync TestClient runs the background task on its own loop; give the pool a
    # fresh engine for that loop instead of sharing this test's loop-bound connections.
    engine = create_async_engine(get_settings().DATABASE_URL)
    fresh = async_sessionmaker(engine, expire_on_commit=False)
    app.dependency_overrides[receiver.get_webhook_deps] = lambda: make_deps(fresh)
    try:
        response = await asyncio.to_thread(
            lambda: TestClient(app).post("/webhooks/paypal", content=raw, headers=HEADERS)
        )
    finally:
        await engine.dispose()
    assert response.status_code == 200 and response.json() == {"status": "received"}
    assert (await get_payment(maker, pid)).status == "CAPTURED"