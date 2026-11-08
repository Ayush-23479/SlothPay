import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from backend.api.deps import install_error_handlers
from backend.api.routes_metrics import router as metrics_router
from backend.models.attempt import PaymentAttempt
from backend.models.audit import PaymentAudit
from backend.models.event import ProviderEvent
from backend.models.payment import Payment
from backend.orchestration.lifecycle import create_order
from backend.payments.hooks import TransportLost, note_chaos_mode, take_chaos_mode
from backend.payments.paypal_client import PayPalOutcomeUnknown

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1",
        reason="Set SLOTHPAY_RUN_DB_TESTS=1 for local PostgreSQL tests",
    ),
]


async def seed(maker, status="INTENT_CREATED", created_at=None):
    token = uuid.uuid4().hex
    async with maker() as session:
        row = Payment(
            merchant_id="chaos-db",
            merchant_order_id=f"order-{token}",
            intent_key=f"intent-{token}",
            amount=Decimal("10.00"),
            currency="USD",
            mode="SMARTPAY",
            status=status,
            version=0,
        )
        if created_at:
            row.created_at = created_at
        session.add(row)
        await session.commit()
        return row.id


async def audit(maker, payment_id, event_type, *, to_state=None, at=None):
    async with maker() as session:
        row = PaymentAudit(
            payment_id=payment_id,
            event_type=event_type,
            to_state=to_state,
            actor="test",
            evidence={},
        )
        if at:
            row.created_at = at
        session.add(row)
        await session.commit()


async def test_a_dropped_answer_leaves_uncertain_and_records_the_chaos_mode(isolated_maker):
    payment_id = await seed(isolated_maker)
    take_chaos_mode()

    async def lost_answer(**_):
        note_chaos_mode("drop_after_send")  # what the chaos controller does when it fires
        raise PayPalOutcomeUnknown("answer lost") from TransportLost("chaos")

    paypal = SimpleNamespace(create_order=AsyncMock(side_effect=lost_answer))
    async with isolated_maker() as session:
        with pytest.raises(PayPalOutcomeUnknown):
            await create_order(session, paypal, payment_id=payment_id)
    async with isolated_maker() as session:
        payment = await session.get(Payment, payment_id)
        attempt = (
            await session.execute(
                select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id)
            )
        ).scalar_one()
    assert payment.status == "UNCERTAIN"
    assert attempt.status == "UNCERTAIN"
    assert attempt.chaos_mode == "drop_after_send"


async def test_a_normal_attempt_has_no_chaos_mode(isolated_maker):
    payment_id = await seed(isolated_maker)
    take_chaos_mode()
    paypal = SimpleNamespace(
        create_order=AsyncMock(return_value={"id": "ORDER-ABCDEFGH-1", "status": "CREATED"})
    )
    async with isolated_maker() as session:
        await create_order(session, paypal, payment_id=payment_id)
    async with isolated_maker() as session:
        attempt = (
            await session.execute(
                select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id)
            )
        ).scalar_one()
    assert attempt.chaos_mode is None


def metrics_app(maker) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(metrics_router)
    app.state.session_factory = maker
    return app


async def get_metrics(maker) -> dict:
    transport = httpx.ASGITransport(app=metrics_app(maker))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/api/metrics", headers={"X-Owner-Id": "metrics-viewer"})
    assert response.status_code == 200
    return response.json()


async def test_metrics_on_an_empty_database(isolated_maker):
    body = await get_metrics(isolated_maker)
    assert body["total"] == 0
    assert body["completed"] == 0
    assert body["capture_seconds_avg"] is None
    assert body["webhook_verify_ms_avg"] is None
    assert body["capture_latency_label"] == "sandbox"


async def test_metrics_count_the_demo_story(isolated_maker):
    now = datetime.now(UTC)
    # 1. Captured after an uncertain moment that reconciliation resolved (recovered).
    recovered = await seed(isolated_maker, "CAPTURED", now - timedelta(seconds=10))
    await audit(isolated_maker, recovered, "PROVIDER_OPERATION_OUTCOME_UNKNOWN")
    await audit(isolated_maker, recovered, "RECONCILED")
    await audit(isolated_maker, recovered, "STATE_TRANSITION", to_state="CAPTURED", at=now)
    # 2. Captured cleanly, 20 seconds after the click.
    clean = await seed(isolated_maker, "CAPTURED", now - timedelta(seconds=20))
    await audit(isolated_maker, clean, "STATE_TRANSITION", to_state="CAPTURED", at=now)
    # 3. Still uncertain.
    stuck = await seed(isolated_maker, "UNCERTAIN")
    await audit(isolated_maker, stuck, "PROVIDER_OPERATION_OUTCOME_UNKNOWN")
    # Duplicates and a rescue retry.
    await audit(isolated_maker, clean, "DUPLICATE_SUPPRESSED")
    await audit(isolated_maker, clean, "DUPLICATE_SUPPRESSED")
    await audit(isolated_maker, stuck, "RESCUE_RETRY_CLAIMED")
    # Webhook verification timings.
    async with isolated_maker() as session:
        for index, ms in enumerate((100, 200, 300, 400)):
            session.add(
                ProviderEvent(
                    event_id=f"WH-{index}-{uuid.uuid4().hex[:6]}",
                    event_type="PAYMENT.CAPTURE.COMPLETED",
                    verified=True,
                    payload={},
                    verify_ms=ms,
                )
            )
        await session.commit()

    body = await get_metrics(isolated_maker)
    assert body["total"] == 3
    assert body["completed"] == 2
    assert body["uncertain_now"] == 1
    assert body["recovered"] == 1
    assert body["duplicates_suppressed"] == 2
    assert body["retries"] == 1
    assert body["capture_seconds_avg"] == pytest.approx(15, abs=1)
    assert 10 <= body["capture_seconds_p95"] <= 20.5
    assert body["webhook_verify_ms_avg"] == 250
    assert body["webhook_verify_ms_p95"] == pytest.approx(385, abs=1)