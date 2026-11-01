import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from temporalio.worker import Worker

from backend.models.attempt import PaymentAttempt
from backend.models.condition import PaymentCondition
from backend.models.payment import Payment
from backend.orchestration.conditions import PaymentService
from backend.orchestration.lifecycle import stable_operation_request_id
from backend.reliability.reconcile import DbReconcileStep
from backend.workflows.activities import PaymentActivities
from backend.workflows.client import TemporalWorkflowStarter
from backend.workflows.common import conditional_workflow_id
from backend.workflows.conditional_pay import ConditionalPayWorkflow
from backend.workflows.reconcile_workflow import ReconcileWorkflow, SilenceWatcherWorkflow

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1",
        reason="Set SLOTHPAY_RUN_DB_TESTS=1 for local PostgreSQL tests",
    ),
]


@pytest_asyncio.fixture
async def maker(isolated_maker):
    return isolated_maker


async def seed_waiting(maker):
    """A ConditionalPay payment holding funds, waiting for its condition."""
    token = uuid.uuid4().hex
    payment = Payment(
        merchant_id="task9-workflow-test",
        merchant_order_id=f"order-{token}",
        intent_key=f"intent-{token}",
        amount=Decimal("10.00"),
        currency="USD",
        mode="CONDITIONALPAY",
        status="AWAITING_CONDITION",
        version=0,
        paypal_order_id=f"ORDER-{token}",
    )
    async with maker() as session:
        session.add(payment)
        await session.flush()
        session.add(
            PaymentAttempt(
                payment_id=payment.id,
                provider="paypal",
                operation="authorize",
                request_id=stable_operation_request_id(payment.id, "authorize"),
                status="SUCCEEDED",
                provider_operation_id="AUTH-T9",
            )
        )
        await session.commit()
        return payment.id


def capturable_authorization():
    return {
        "id": "AUTH-T9",
        "status": "CREATED",
        "amount": {"value": "10.00", "currency_code": "USD"},
        "expiration_time": (datetime.now(UTC) + timedelta(days=20)).isoformat(),
    }


def fake_paypal(authorization=None):
    return SimpleNamespace(
        get_authorization=AsyncMock(return_value=authorization or capturable_authorization()),
        capture_authorization=AsyncMock(
            return_value={"id": "CAP-T9", "status": "COMPLETED", "final_capture": True}
        ),
        void_authorization=AsyncMock(return_value={"http_status": 204}),
        get_order=AsyncMock(return_value=None),
    )


async def run(temporal, maker, paypal, act):
    env, skipping = temporal
    queue = f"t9-{uuid.uuid4().hex}"
    activities = PaymentActivities(PaymentService(maker, paypal), DbReconcileStep(maker, paypal))
    starter = TemporalWorkflowStarter(env.client, task_queue=queue, max_hours=48)
    payment_id = await seed_waiting(maker)
    async with Worker(
        env.client,
        task_queue=queue,
        workflows=[ConditionalPayWorkflow, ReconcileWorkflow, SilenceWatcherWorkflow],
        activities=activities.all(),
    ):
        result = await act(starter, str(payment_id), skipping, env)
    return payment_id, result


async def state_of(maker, payment_id):
    async with maker() as session:
        payment = await session.get(Payment, payment_id)
        condition = await session.scalar(
            select(PaymentCondition).where(PaymentCondition.payment_id == payment_id)
        )
        return payment.status, condition.status if condition else None


def deadline(skipping, near=2):
    return (datetime.now(UTC) + timedelta(seconds=3600 if skipping else near)).isoformat()


async def test_an_expiring_condition_voids_the_authorization(temporal, maker):
    paypal = fake_paypal()

    async def act(starter, intent_id, skipping, env):
        assert await starter.start(intent_id, deadline(skipping))
        return await env.client.get_workflow_handle(conditional_workflow_id(intent_id)).result()

    payment_id, result = await run(temporal, maker, paypal, act)
    assert result["outcome"] == "VOID" and result["reason"] == "deadline"
    assert await state_of(maker, payment_id) == ("VOIDED", "EXPIRED")
    paypal.void_authorization.assert_awaited_once()
    paypal.capture_authorization.assert_not_awaited()


async def test_a_verified_condition_captures_exactly_once(temporal, maker):
    paypal = fake_paypal()

    async def act(starter, intent_id, skipping, env):
        await starter.start(intent_id, deadline(skipping))
        await starter.signal(intent_id, True, {"proof": "delivered"})
        return await env.client.get_workflow_handle(conditional_workflow_id(intent_id)).result()

    payment_id, result = await run(temporal, maker, paypal, act)
    assert result["outcome"] == "CAPTURE" and result["state"] == "CAPTURED"
    assert await state_of(maker, payment_id) == ("CAPTURED", "VERIFIED")
    paypal.capture_authorization.assert_awaited_once()
    paypal.void_authorization.assert_not_awaited()


async def test_a_failed_condition_voids(temporal, maker):
    paypal = fake_paypal()

    async def act(starter, intent_id, skipping, env):
        await starter.start(intent_id, deadline(skipping))
        await starter.signal(intent_id, False, {"proof": "not delivered"})
        return await env.client.get_workflow_handle(conditional_workflow_id(intent_id)).result()

    payment_id, result = await run(temporal, maker, paypal, act)
    assert result["outcome"] == "VOID"
    assert await state_of(maker, payment_id) == ("VOIDED", "FAILED")
    paypal.capture_authorization.assert_not_awaited()


async def test_a_verified_condition_with_an_expired_authorization_voids_instead(temporal, maker):
    expired = {**capturable_authorization(), "status": "EXPIRED"}
    paypal = fake_paypal(expired)

    async def act(starter, intent_id, skipping, env):
        await starter.start(intent_id, deadline(skipping))
        await starter.signal(intent_id, True, {})
        return await env.client.get_workflow_handle(conditional_workflow_id(intent_id)).result()

    payment_id, result = await run(temporal, maker, paypal, act)
    paypal.capture_authorization.assert_not_awaited()
    paypal.void_authorization.assert_awaited_once()
    assert result["outcome"] == "VOID"
    assert (await state_of(maker, payment_id))[0] == "VOIDED"


async def test_a_lost_capture_answer_is_reconciled_not_retried(temporal, maker):
    from backend.payments.paypal_client import PayPalOutcomeUnknown

    paypal = fake_paypal()
    paypal.capture_authorization.side_effect = PayPalOutcomeUnknown("answer lost")
    captured = {
        "id": "AUTH-T9",
        "status": "CAPTURED",
        "amount": {"value": "10.00", "currency_code": "USD"},
    }
    paypal.get_authorization.side_effect = [
        capturable_authorization(),  # pre-capture freshness read
        captured,  # reconcile read
        captured,
    ]

    async def act(starter, intent_id, skipping, env):
        await starter.start(intent_id, deadline(skipping))
        await starter.signal(intent_id, True, {})
        outer = await env.client.get_workflow_handle(conditional_workflow_id(intent_id)).result()
        return outer

    payment_id, result = await run(temporal, maker, paypal, act)
    assert result["needs_reconciliation"] is True and result["reconcile_started"] is True
    paypal.capture_authorization.assert_awaited_once()  # never replayed
    assert (await state_of(maker, payment_id))[0] in {"UNCERTAIN", "CAPTURED"}


# ------------------------------------------------- PaymentService on its own (no Temporal)
async def set_status(maker, payment_id, status):
    async with maker() as session:
        payment = await session.get(Payment, payment_id)
        payment.status = status
        await session.commit()


async def test_resolving_twice_makes_one_paypal_call(maker):
    paypal = fake_paypal()
    service = PaymentService(maker, paypal)
    payment_id = await seed_waiting(maker)
    first = await service.resolve_condition(str(payment_id), True, {"proof": 1}, "verified")
    second = await service.resolve_condition(str(payment_id), True, {"proof": 1}, "verified")
    assert first["state"] == "CAPTURED" and first["action"] == "CAPTURE"
    assert second["already_final"] is True and second["action"] == "NONE"
    paypal.capture_authorization.assert_awaited_once()


async def test_a_final_or_in_flight_payment_is_never_operated_on_again(maker):
    paypal = fake_paypal()
    service = PaymentService(maker, paypal)
    for status, expect_reconcile in (
        ("VOIDED", False),
        ("UNCERTAIN", True),
        ("CAPTURE_REQUESTED", True),
    ):
        payment_id = await seed_waiting(maker)
        await set_status(maker, payment_id, status)
        result = await service.resolve_condition(str(payment_id), False, {}, "deadline")
        assert result["action"] == "NONE" and result["needs_reconciliation"] is expect_reconcile
    paypal.void_authorization.assert_not_awaited()
    paypal.capture_authorization.assert_not_awaited()


async def test_before_any_authorization_exists_there_is_nothing_to_void(maker):
    from backend.orchestration.conditions import ConditionNotReady

    paypal = fake_paypal()
    service = PaymentService(maker, paypal)
    payment_id = await seed_waiting(maker)
    await set_status(maker, payment_id, "APPROVED")
    result = await service.resolve_condition(str(payment_id), False, {}, "deadline")
    assert result["nothing_to_void"] is True and result["action"] == "NONE"
    # A verified result before the authorization exists must wait, never skip ahead.
    with pytest.raises(ConditionNotReady):
        await service.resolve_condition(str(payment_id), True, {}, "verified")
    await set_status(maker, payment_id, "AUTHORIZE_REQUESTED")
    with pytest.raises(ConditionNotReady):
        await service.resolve_condition(str(payment_id), False, {}, "deadline")
    paypal.void_authorization.assert_not_awaited()
    paypal.capture_authorization.assert_not_awaited()


async def test_the_first_condition_decision_stands(maker):
    paypal = fake_paypal()
    service = PaymentService(maker, paypal)
    payment_id = await seed_waiting(maker)
    await service.mark_condition_expired(str(payment_id), "deadline")
    again = await service.mark_condition_expired(str(payment_id), "deadline")
    assert again["condition_status"] == "EXPIRED"
    await service.resolve_condition(str(payment_id), False, {}, "failed")
    assert await state_of(maker, payment_id) == ("VOIDED", "EXPIRED")


async def test_an_unknown_payment_is_a_clear_error(maker):
    from backend.orchestration.conditions import PaymentMissing

    service = PaymentService(maker, fake_paypal())
    with pytest.raises(PaymentMissing):
        await service.resolve_condition(str(uuid.uuid4()), False, {}, "deadline")
    with pytest.raises(PaymentMissing):
        await service.resolve_condition("not-a-uuid", False, {}, "deadline")


async def test_smartpay_payments_are_left_alone(maker):
    paypal = fake_paypal()
    service = PaymentService(maker, paypal)
    payment_id = await seed_waiting(maker)
    async with maker() as session:
        (await session.get(Payment, payment_id)).mode = "SMARTPAY"
        await session.commit()
    result = await service.resolve_condition(str(payment_id), True, {}, "verified")
    assert result["skipped"] == "not_conditional"
    paypal.capture_authorization.assert_not_awaited()