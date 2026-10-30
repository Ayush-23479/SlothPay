"""Crash and concurrency safety tests using a disposable LOCAL PostgreSQL DB.

These tests commit records and do not remove them. PayPal is mocked.
"""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config.settings import get_settings
from backend.models.attempt import PaymentAttempt
from backend.models.audit import PaymentAudit
from backend.models.payment import Payment
from backend.models.reconciliation import PaymentReconciliation
from backend.orchestration.lifecycle import (
    LifecycleSafetyError,
    OperationAlreadyStarted,
    create_order,
    execute_operation,
    stable_operation_request_id,
)
from backend.orchestration.reconcile import process_due_reconciliations, reconcile_attempt
from backend.orchestration.recovery import (
    RescueActionNotAllowed,
    execute_rescue_action,
    get_rescue_plan,
    inspect_unresolved_attempts,
)
from backend.payments.paypal_client import PayPalNotSent, PayPalOutcomeUnknown
from backend.webhooks.handler import APPLIED, REJECTED, WebhookDeps, process_webhook

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1",
        reason="Set SLOTHPAY_RUN_DB_TESTS=1 for local PostgreSQL tests",
    ),
]


@pytest_asyncio.fixture
async def maker():
    url = get_settings().DATABASE_URL
    if "localhost" not in url and "127.0.0.1" not in url:
        pytest.skip("Refusing to commit test rows to a nonlocal database")
    engine = create_async_engine(url, pool_size=8)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def seed(maker, *, status="INTENT_CREATED", order_id=None, mode="SMARTPAY"):
    token = uuid.uuid4().hex
    payment = Payment(
        merchant_id="task6-recovery-safety-test",
        merchant_order_id=f"order-{token}",
        intent_key=f"intent-{token}",
        amount=Decimal("10.00"),
        currency="USD",
        mode=mode,
        status=status,
        version=0,
        paypal_order_id=order_id,
    )
    async with maker() as session:
        session.add(payment)
        await session.commit()
        return payment.id


async def add_attempt(maker, payment_id, operation, status, *, provider_operation_id=None):
    row = PaymentAttempt(
        payment_id=payment_id,
        provider="paypal",
        operation=operation,
        request_id=stable_operation_request_id(payment_id, operation),
        status=status,
        provider_operation_id=provider_operation_id,
    )
    async with maker() as session:
        session.add(row)
        await session.commit()
        return row.id


def gateway():
    return SimpleNamespace(
        create_order=AsyncMock(
            return_value={"id": f"ORDER-{uuid.uuid4().hex}", "status": "CREATED"}
        ),
        capture_order=AsyncMock(
            return_value={
                "purchase_units": [
                    {"payments": {"captures": [{"id": "CAP-1", "status": "COMPLETED"}]}}
                ]
            }
        ),
    )


async def test_concurrent_order_creation_issues_only_one_paypal_call(maker):
    payment_id = await seed(maker)
    paypal = gateway()

    async def worker():
        async with maker() as session:
            try:
                return await create_order(session, paypal, payment_id=payment_id)
            except (OperationAlreadyStarted, LifecycleSafetyError):
                return "blocked"

    results = await asyncio.gather(worker(), worker())
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("blocked") == 1
    paypal.create_order.assert_awaited_once()
    async with maker() as session:
        attempts = (
            await session.scalars(
                select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id)
            )
        ).all()
        assert len(attempts) == 1
        assert attempts[0].status == "SUCCEEDED"


async def test_committed_started_order_attempt_is_not_replayed(maker):
    payment_id = await seed(maker)
    request_id = stable_operation_request_id(payment_id, "create_order")
    async with maker() as session:
        session.add(
            PaymentAttempt(
                payment_id=payment_id,
                provider="paypal",
                operation="create_order",
                request_id=request_id,
                status="STARTED",
            )
        )
        await session.commit()

    paypal = gateway()
    async with maker() as session:
        with pytest.raises(OperationAlreadyStarted):
            await create_order(session, paypal, payment_id=payment_id)
    paypal.create_order.assert_not_awaited()

    async with maker() as session:
        pending = await inspect_unresolved_attempts(session, payment_id=payment_id)
    assert len(pending) == 1
    assert pending[0].attempt_status == "STARTED"
    assert pending[0].request_id == request_id
    assert pending[0].payment_status == "INTENT_CREATED"


async def test_committed_started_capture_claim_is_not_replayed(maker):
    order_id = f"ORDER-{uuid.uuid4().hex}"
    payment_id = await seed(maker, status="CAPTURE_REQUESTED", order_id=order_id)
    request_id = stable_operation_request_id(payment_id, "capture")
    async with maker() as session:
        session.add(
            PaymentAttempt(
                payment_id=payment_id,
                provider="paypal",
                operation="capture",
                request_id=request_id,
                status="STARTED",
            )
        )
        await session.commit()

    paypal = gateway()
    async with maker() as session:
        with pytest.raises(LifecycleSafetyError):
            await execute_operation(session, paypal, payment_id=payment_id, operation="capture")
    paypal.capture_order.assert_not_awaited()

    async with maker() as session:
        pending = await inspect_unresolved_attempts(session, payment_id=payment_id)
    assert len(pending) == 1
    assert pending[0].operation == "capture"
    assert pending[0].attempt_status == "STARTED"
    assert pending[0].payment_status == "CAPTURE_REQUESTED"


async def test_inspection_does_not_modify_payment_or_attempt(maker):
    payment_id = await seed(maker)
    request_id = stable_operation_request_id(payment_id, "create_order")
    async with maker() as session:
        session.add(
            PaymentAttempt(
                payment_id=payment_id,
                provider="paypal",
                operation="create_order",
                request_id=request_id,
                status="UNCERTAIN",
            )
        )
        await session.commit()

    async with maker() as session:
        pending = await inspect_unresolved_attempts(session, payment_id=payment_id)
        await session.rollback()
    assert len(pending) == 1
    assert pending[0].attempt_status == "UNCERTAIN"
    async with maker() as session:
        attempt = await session.scalar(
            select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id)
        )
        payment = await session.get(Payment, payment_id)
    assert attempt.status == "UNCERTAIN"
    assert payment.status == "INTENT_CREATED"


def completed_order(order_id, *, status="COMPLETED", capture_status="COMPLETED"):
    return {
        "id": order_id,
        "status": status,
        "purchase_units": [
            {
                "amount": {"value": "10.00", "currency_code": "USD"},
                "payments": {
                    "captures": [
                        {
                            "id": "CAP-TASK8",
                            "status": capture_status,
                            "amount": {"value": "10.00", "currency_code": "USD"},
                        }
                    ]
                },
            }
        ],
    }


async def start_uncertain_capture(maker, paypal, *, order_id):
    payment_id = await seed(maker, status="APPROVED", order_id=order_id)
    paypal.capture_order.side_effect = PayPalOutcomeUnknown("response was lost")
    async with maker() as session:
        with pytest.raises(PayPalOutcomeUnknown):
            await execute_operation(session, paypal, payment_id=payment_id, operation="capture")
    return payment_id


async def test_lost_capture_response_reconciles_to_captured_without_second_capture(maker):
    order_id = f"ORDER-{uuid.uuid4().hex}"
    paypal = gateway()
    payment_id = await start_uncertain_capture(maker, paypal, order_id=order_id)
    paypal.get_order = AsyncMock(return_value=completed_order(order_id))

    async with maker() as session:
        results = await process_due_reconciliations(
            session, paypal, now=datetime.now(UTC) + timedelta(seconds=2)
        )
    assert len(results) == 1
    assert results[0].decision == "EVIDENCE_APPLIED"
    paypal.capture_order.assert_awaited_once()

    async with maker() as session:
        payment = await session.get(Payment, payment_id)
        attempt = await session.scalar(
            select(PaymentAttempt).where(
                PaymentAttempt.payment_id == payment_id,
                PaymentAttempt.operation == "capture",
            )
        )
        audit = list(
            await session.scalars(
                select(PaymentAudit).where(PaymentAudit.payment_id == payment_id)
            )
        )
    assert payment.status == "CAPTURED"
    assert attempt.status == "SUCCEEDED" and attempt.provider_operation_id == "CAP-TASK8"
    assert any(
        row.event_type == "OPERATION_ATTEMPT_STARTED"
        and row.evidence["source"] == "internal_record"
        and row.evidence["confirmed"] is False
        for row in audit
    )
    assert any(
        row.event_type == "PROVIDER_EVIDENCE_CONFIRMED"
        and row.evidence["source"] == "paypal_get"
        and row.evidence["confirmed"] is True
        for row in audit
    )


async def test_not_sent_operation_is_not_replayed_automatically_and_can_use_same_request_id(maker):
    payment_id = await seed(maker)
    paypal = gateway()
    successful_order = {"id": f"ORDER-{uuid.uuid4().hex}", "status": "CREATED"}
    paypal.create_order.side_effect = [
        PayPalNotSent("OAuth token unavailable"),
        successful_order,
    ]
    async with maker() as session:
        with pytest.raises(PayPalNotSent):
            await create_order(session, paypal, payment_id=payment_id)
    paypal.create_order.assert_awaited_once()

    async with maker() as session:
        plan = await get_rescue_plan(session, payment_id=payment_id)
        assert plan["recommended_action"] == "RETRY_SAME_REQUEST"
        assert plan["buyer_facing_state"] == "Pending"
        results = await process_due_reconciliations(
            session, paypal, now=datetime.now(UTC) + timedelta(days=1)
        )
        assert results == []
    paypal.create_order.assert_awaited_once()

    async with maker() as session:
        result = await execute_rescue_action(
            session,
            paypal,
            payment_id=payment_id,
            action="RETRY_SAME_REQUEST",
        )
    assert result["result"]["state"] == "ORDER_CREATED"
    paypal.create_order.assert_awaited_once()
    first_request_id = paypal.create_order.await_args_list[0].kwargs["request_id"]
    retry_request_id = paypal.create_order.await_args_list[1].kwargs["request_id"]
    assert retry_request_id == first_request_id == stable_operation_request_id(
        payment_id, "create_order"
    )


async def test_five_lookup_timeouts_end_in_action_needed_without_marking_failure(maker):
    order_id = f"ORDER-{uuid.uuid4().hex}"
    payment_id = await seed(maker, status="UNCERTAIN", order_id=order_id)
    attempt_id = await add_attempt(maker, payment_id, "capture", "UNCERTAIN")
    async with maker() as session:
        session.add(
            PaymentReconciliation(
                attempt_id=attempt_id,
                payment_id=payment_id,
                check_count=0,
                next_check_at=datetime.now(UTC),
            )
        )
        await session.commit()
    paypal = gateway()
    paypal.get_order = AsyncMock(side_effect=PayPalOutcomeUnknown("lookup timeout"))

    for _ in range(5):
        async with maker() as session:
            observation = await reconcile_attempt(
                session, paypal, attempt_id=attempt_id, force=True
            )
    assert observation.decision == "EXHAUSTED"
    paypal.get_order.assert_awaited_once_with(order_id)
    assert paypal.get_order.await_count == 5

    async with maker() as session:
        payment = await session.get(Payment, payment_id)
        run = await session.get(PaymentReconciliation, attempt_id)
        plan = await get_rescue_plan(session, payment_id=payment_id)
    assert payment.status == "UNCERTAIN"
    assert run.check_count == 5 and run.completed_at is not None
    assert plan["buyer_facing_state"] == "Action needed"
    assert plan["allowed_actions"] == ["CONTACT_SUPPORT"]
    assert plan["recommended_action"] == "CONTACT_SUPPORT"


async def test_pending_capture_is_not_reported_successful_or_retryable(maker):
    order_id = f"ORDER-{uuid.uuid4().hex}"
    paypal = gateway()
    payment_id = await start_uncertain_capture(maker, paypal, order_id=order_id)
    paypal.get_order = AsyncMock(
        return_value=completed_order(order_id, status="APPROVED", capture_status="PENDING")
    )

    async with maker() as session:
        observation = await reconcile_attempt(
            session,
            paypal,
            attempt_id=(
                await session.scalar(
                    select(PaymentAttempt.id).where(
                        PaymentAttempt.payment_id == payment_id,
                        PaymentAttempt.operation == "capture",
                    )
                )
            ),
            force=True,
        )
        payment = await session.get(Payment, payment_id)
        plan = await get_rescue_plan(session, payment_id=payment_id)
    assert observation.decision == "EVIDENCE_APPLIED"
    assert payment.status == "CAPTURE_PENDING"
    assert plan["buyer_facing_state"] == "Pending"
    assert "RETRY_SAME_REQUEST" not in plan["allowed_actions"]
    assert "CAPTURE" not in plan["allowed_actions"]
    paypal.capture_order.assert_awaited_once()


async def test_concurrent_reconciliation_workers_apply_one_consistent_transition(maker):
    order_id = f"ORDER-{uuid.uuid4().hex}"
    paypal = gateway()
    payment_id = await start_uncertain_capture(maker, paypal, order_id=order_id)
    paypal.get_order = AsyncMock(return_value=completed_order(order_id))

    async def worker():
        async with maker() as session:
            return await process_due_reconciliations(
                session, paypal, now=datetime.now(UTC) + timedelta(seconds=2)
            )

    results = await asyncio.gather(worker(), worker())
    paypal.get_order.assert_awaited_once_with(order_id)
    async with maker() as session:
        payment = await session.get(Payment, payment_id)
        transitions = await session.scalar(
            select(func.count())
            .select_from(PaymentAudit)
            .where(
                PaymentAudit.payment_id == payment_id,
                PaymentAudit.event_type == "STATE_TRANSITION",
                PaymentAudit.to_state == "CAPTURED",
            )
        )
    assert payment.status == "CAPTURED"
    assert transitions == 1
    assert sum(len(batch) for batch in results) >= 1


async def test_verified_webhook_winning_during_lookup_blocks_stale_reconciliation(maker):
    order_id = f"ORDER-{uuid.uuid4().hex}"
    paypal = gateway()
    payment_id = await seed(maker, status="UNCERTAIN", order_id=order_id)
    attempt_id = await add_attempt(maker, payment_id, "capture", "UNCERTAIN")
    async with maker() as session:
        session.add(
            PaymentReconciliation(
                attempt_id=attempt_id,
                payment_id=payment_id,
                check_count=0,
                next_check_at=datetime.now(UTC),
            )
        )
        await session.commit()

    event = {
        "id": f"WH-{uuid.uuid4().hex}",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": "CAP-WEBHOOK",
            "status": "COMPLETED",
            "amount": {"value": "10.00", "currency_code": "USD"},
            "supplementary_data": {"related_ids": {"order_id": order_id}},
        },
    }
    raw_body = json.dumps(event).encode()

    async def webhook_then_stale_order(_order_id):
        async def verify(_headers, _raw):
            return True

        async with maker():
            assert (
                await process_webhook(
                    {"paypal-transmission-id": "test"},
                    raw_body,
                    WebhookDeps(session_factory=maker, verify=verify),
                )
                == APPLIED
            )
        return completed_order(order_id, status="APPROVED", capture_status="PENDING")

    paypal.get_order = AsyncMock(side_effect=webhook_then_stale_order)
    async with maker() as session:
        observation = await reconcile_attempt(
            session, paypal, attempt_id=attempt_id, force=True
        )
    async with maker() as session:
        payment = await session.get(Payment, payment_id)
        attempt = await session.get(PaymentAttempt, attempt_id)
    assert observation.decision == "STALE_EVIDENCE"
    assert payment.status == "CAPTURED"
    assert attempt.status == "SUCCEEDED" and attempt.provider_operation_id == "CAP-WEBHOOK"
    assert paypal.get_order.await_count == 1


async def test_unverified_webhook_and_forbidden_action_cannot_authorize_retry(maker):
    order_id = f"ORDER-{uuid.uuid4().hex}"
    payment_id = await seed(maker, status="UNCERTAIN", order_id=order_id)
    await add_attempt(maker, payment_id, "capture", "UNCERTAIN")
    event = {
        "id": f"WH-{uuid.uuid4().hex}",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {"id": "CAP-FORGED", "status": "COMPLETED"},
    }
    async def verify(_headers, _raw):
        return False

    async with maker() as session:
        result = await process_webhook(
            {},
            json.dumps(event).encode(),
            WebhookDeps(session_factory=maker, verify=verify),
        )
        plan = await get_rescue_plan(session, payment_id=payment_id)
    assert result == REJECTED
    assert "RETRY_SAME_REQUEST" not in plan["allowed_actions"]
    paypal = gateway()
    async with maker() as session:
        with pytest.raises(RescueActionNotAllowed):
            await execute_rescue_action(
                session,
                paypal,
                payment_id=payment_id,
                action="CAPTURE",
            )
    paypal.capture_order.assert_not_awaited()


async def test_stale_not_occurred_evidence_cannot_authorize_void_retry(maker):
    payment_id = await seed(
        maker, status="AWAITING_CONDITION", mode="CONDITIONALPAY", order_id="ORDER-VOID"
    )
    await add_attempt(
        maker, payment_id, "authorize", "SUCCEEDED", provider_operation_id="AUTH-VOID"
    )
    void_attempt_id = await add_attempt(maker, payment_id, "void", "NOT_OCCURRED")
    async with maker() as session:
        session.add(
            PaymentReconciliation(
                attempt_id=void_attempt_id,
                payment_id=payment_id,
                check_count=1,
                next_check_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                last_reason="Authorization was active during previous check",
            )
        )
        plan = await get_rescue_plan(session, payment_id=payment_id)
    assert plan["recommended_action"] == "RETRY_SAME_REQUEST"
    paypal = gateway()
    paypal.get_authorization = AsyncMock(
        return_value={
            "id": "AUTH-VOID",
            "status": "PENDING",
            "amount": {"value": "10.00", "currency_code": "USD"},
        }
    )
    paypal.void_authorization = AsyncMock()
    async with maker() as session:
        with pytest.raises(RescueActionNotAllowed):
            await execute_rescue_action(
                session,
                paypal,
                payment_id=payment_id,
                action="RETRY_SAME_REQUEST",
            )
    paypal.get_authorization.assert_awaited_once_with("AUTH-VOID")
    paypal.void_authorization.assert_not_awaited()


async def test_final_payment_state_is_not_reopened_by_reconciliation(maker):
    order_id = f"ORDER-{uuid.uuid4().hex}"
    payment_id = await seed(maker, status="CAPTURED", order_id=order_id)
    attempt_id = await add_attempt(maker, payment_id, "capture", "UNCERTAIN")
    async with maker() as session:
        session.add(
            PaymentReconciliation(
                attempt_id=attempt_id,
                payment_id=payment_id,
                check_count=0,
                next_check_at=datetime.now(UTC),
            )
        )
        await session.commit()
    paypal = gateway()
    paypal.get_order = AsyncMock(return_value=completed_order(order_id, capture_status="PENDING"))

    async with maker() as session:
        observation = await reconcile_attempt(
            session, paypal, attempt_id=attempt_id, force=True
        )
        payment = await session.get(Payment, payment_id)
    assert observation.decision == "FINAL_STATE"
    assert payment.status == "CAPTURED"
