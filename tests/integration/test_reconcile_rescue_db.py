
import os
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from backend.models.attempt import PaymentAttempt
from backend.models.audit import PaymentAudit
from backend.models.payment import Payment
from backend.models.reconciliation import PaymentReconciliation
from backend.orchestration.lifecycle import execute_operation, stable_operation_request_id
from backend.payments.paypal_client import PayPalOutcomeUnknown
from backend.reliability.reconcile import RECONCILE_DELAYS, StatusReconciler
from backend.reliability.rescue import DbRescueService, build_rescue_plan, execute_rescue_action

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


class Sleeps:
    """Injected sleep: records the waits and never actually waits."""

    def __init__(self):
        self.waits: list[float] = []

    async def __call__(self, seconds):
        self.waits.append(seconds)


async def seed(maker, *, status, order_id, mode="SMARTPAY"):
    token = uuid.uuid4().hex
    payment = Payment(
        merchant_id="task8-reconcile-test",
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


async def add_attempt(maker, payment_id, operation, status, provider_operation_id=None):
    async with maker() as session:
        session.add(
            PaymentAttempt(
                payment_id=payment_id,
                provider="paypal",
                operation=operation,
                request_id=stable_operation_request_id(payment_id, operation),
                status=status,
                provider_operation_id=provider_operation_id,
            )
        )
        await session.commit()


def order_doc(order_id, *, status, captures=None):
    unit = {"amount": {"value": "10.00", "currency_code": "USD"}}
    if captures is not None:
        unit["payments"] = {"captures": captures}
    return {"id": order_id, "status": status, "purchase_units": [unit]}


def completed_capture():
    return [
        {
            "id": f"CAP-{uuid.uuid4().hex[:8]}",
            "status": "COMPLETED",
            "amount": {"value": "10.00", "currency_code": "USD"},
        }
    ]


def fake_paypal():
    return SimpleNamespace(
        get_order=AsyncMock(),
        capture_order=AsyncMock(),
        get_authorization=AsyncMock(),
        void_authorization=AsyncMock(),
    )


async def lost_capture(maker, paypal, order_id):
    payment_id = await seed(maker, status="APPROVED", order_id=order_id)
    paypal.capture_order.side_effect = PayPalOutcomeUnknown("answer was lost")
    async with maker() as session:
        with pytest.raises(PayPalOutcomeUnknown):
            await execute_operation(session, paypal, payment_id=payment_id, operation="capture")
    async with maker() as session:
        assert (await session.get(Payment, payment_id)).status == "UNCERTAIN"
    return payment_id


async def status_of(maker, payment_id):
    async with maker() as session:
        return (await session.get(Payment, payment_id)).status


async def audit_types(maker, payment_id):
    async with maker() as session:
        rows = await session.scalars(
            select(PaymentAudit)
            .where(PaymentAudit.payment_id == payment_id)
            .order_by(PaymentAudit.created_at)
        )
        return [(row.event_type, row.evidence) for row in rows]


async def test_capture_answer_lost_but_processed_ends_captured_with_one_capture(maker):
    order_id = f"ORDER-{uuid.uuid4().hex}"
    paypal = fake_paypal()
    payment_id = await lost_capture(maker, paypal, order_id)
    paypal.get_order.return_value = order_doc(
        order_id, status="COMPLETED", captures=completed_capture()
    )
    sleeps = Sleeps()

    result = await StatusReconciler.for_database(maker, paypal, sleep=sleeps).reconcile_detailed(
        str(payment_id)
    )

    assert result.status == "CAPTURED" and not result.exhausted and not result.retry_allowed
    assert sleeps.waits == [0.3], "first check ~300 ms after the uncertain outcome"
    paypal.capture_order.assert_awaited_once()
    assert paypal.get_order.await_count == 1
    reconciled = [e for t, e in await audit_types(maker, payment_id) if t == "RECONCILED"]
    assert len(reconciled) == 1
    assert reconciled[0]["source"] == "reconcile"
    assert reconciled[0]["pending_operation"] == "capture"
    assert reconciled[0]["confirmed"] is True


async def test_capture_answer_lost_and_not_processed_allows_retry_with_same_request_id(maker):
    order_id = f"ORDER-{uuid.uuid4().hex}"
    paypal = fake_paypal()
    payment_id = await lost_capture(maker, paypal, order_id)
    paypal.get_order.return_value = order_doc(order_id, status="APPROVED")

    result = await StatusReconciler.for_database(maker, paypal, sleep=Sleeps()).reconcile_detailed(
        str(payment_id)
    )
    assert result.status == "APPROVED" and result.retry_allowed and not result.exhausted
    paypal.capture_order.assert_awaited_once()

    # Retry through the guarded rescue path: same request id, one more capture call.
    paypal.capture_order.side_effect = None
    paypal.capture_order.return_value = order_doc(
        order_id, status="COMPLETED", captures=completed_capture()
    )
    async with maker() as session:
        service = DbRescueService(session, paypal)
        intent = await service.load_intent(str(payment_id))
        assert intent["retry_allowed"] is True
        plan = build_rescue_plan(intent, None, None)
        assert "RETRY_SAME_REQUEST" in plan["allowed_actions"]
        outcome = await execute_rescue_action(service, str(payment_id), "RETRY_SAME_REQUEST")
    assert "error" not in outcome, outcome
    assert paypal.capture_order.await_count == 2
    first, second = paypal.capture_order.await_args_list
    assert first.kwargs["request_id"] == second.kwargs["request_id"]
    assert second.kwargs["request_id"] == stable_operation_request_id(payment_id, "capture")
    assert await status_of(maker, payment_id) == "CAPTURED"
    types = [t for t, _ in await audit_types(maker, payment_id)]
    assert "RESCUE_PLAN_BUILT" in types and "RESCUE_ACTION_EXECUTED" in types


async def test_void_answer_lost_but_processed_ends_voided(maker):
    order_id = f"ORDER-{uuid.uuid4().hex}"
    payment_id = await seed(
        maker, status="AWAITING_CONDITION", order_id=order_id, mode="CONDITIONALPAY"
    )
    await add_attempt(maker, payment_id, "authorize", "SUCCEEDED", provider_operation_id="AUTH-T8")
    paypal = fake_paypal()
    paypal.get_authorization.return_value = {
        "id": "AUTH-T8",
        "status": "CREATED",
        "amount": {"value": "10.00", "currency_code": "USD"},
    }
    paypal.void_authorization.side_effect = PayPalOutcomeUnknown("answer was lost")
    async with maker() as session:
        with pytest.raises(PayPalOutcomeUnknown):
            await execute_operation(session, paypal, payment_id=payment_id, operation="void")
    assert await status_of(maker, payment_id) == "UNCERTAIN"

    paypal.get_authorization.return_value = {
        "id": "AUTH-T8",
        "status": "VOIDED",
        "amount": {"value": "10.00", "currency_code": "USD"},
    }
    status = await StatusReconciler.for_database(maker, paypal, sleep=Sleeps()).reconcile(
        str(payment_id)
    )
    assert status == "VOIDED"
    paypal.void_authorization.assert_awaited_once()


async def test_five_failed_reads_exhaust_and_leave_the_status_unchanged(maker):
    order_id = f"ORDER-{uuid.uuid4().hex}"
    paypal = fake_paypal()
    payment_id = await lost_capture(maker, paypal, order_id)
    paypal.get_order.side_effect = PayPalOutcomeUnknown("PayPal read timed out")
    sleeps = Sleeps()

    result = await StatusReconciler.for_database(maker, paypal, sleep=sleeps).reconcile_detailed(
        str(payment_id)
    )

    assert result.status == "UNCERTAIN", "a failed read never changes the status"
    assert result.exhausted and result.checks == 5
    assert sleeps.waits == list(RECONCILE_DELAYS) == [0.3, 1.0, 2.0, 4.0, 8.0]
    assert paypal.get_order.await_count == 5
    paypal.capture_order.assert_awaited_once()

    async with maker() as session:
        service = DbRescueService(session, paypal)
        intent = await service.load_intent(str(payment_id))
        assert intent["reconcile_exhausted"] is True
        plan = build_rescue_plan(intent, None, None)
        assert plan["display_status"] == "ACTION_NEEDED"
        assert plan["allowed_actions"] == ["CONTACT_SUPPORT"]
        assert plan["needs_human"] is True
        # A sixth round does not read PayPal again.
        again = await StatusReconciler.for_database(
            maker, paypal, sleep=Sleeps()
        ).reconcile_detailed(str(payment_id))
    assert again.exhausted and paypal.get_order.await_count == 5
    async with maker() as session:
        schedule = (await session.scalars(select(PaymentReconciliation))).all()
        assert any(item.payment_id == payment_id and item.check_count == 5 for item in schedule)


async def test_retry_is_rejected_while_the_payment_is_still_uncertain(maker):
    order_id = f"ORDER-{uuid.uuid4().hex}"
    paypal = fake_paypal()
    payment_id = await lost_capture(maker, paypal, order_id)

    async with maker() as session:
        outcome = await execute_rescue_action(
            DbRescueService(session, paypal), str(payment_id), "RETRY_SAME_REQUEST"
        )
    assert outcome == {"error": "Action not permitted"}
    paypal.capture_order.assert_awaited_once()
    assert await status_of(maker, payment_id) == "UNCERTAIN"


async def test_retry_is_refused_if_paypal_changed_its_answer_after_the_check(maker):
    order_id = f"ORDER-{uuid.uuid4().hex}"
    paypal = fake_paypal()
    payment_id = await lost_capture(maker, paypal, order_id)
    paypal.get_order.return_value = order_doc(order_id, status="APPROVED")
    await StatusReconciler.for_database(maker, paypal, sleep=Sleeps()).reconcile(str(payment_id))
    assert await status_of(maker, payment_id) == "APPROVED"

    # Just before the retry PayPal now shows the capture: the retry must not go out.
    paypal.get_order.return_value = order_doc(
        order_id, status="COMPLETED", captures=completed_capture()
    )
    async with maker() as session:
        outcome = await execute_rescue_action(
            DbRescueService(session, paypal), str(payment_id), "RETRY_SAME_REQUEST"
        )
    assert outcome == {"error": "Action not permitted"}
    paypal.capture_order.assert_awaited_once()