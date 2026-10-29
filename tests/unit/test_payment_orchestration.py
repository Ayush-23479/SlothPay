"""Offline tests for Task 6 claim-before-execute starter."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models.repository import StalePaymentVersion
from backend.orchestration import service
from backend.payments.paypal_client import PayPalOutcomeUnknown
from backend.state_machine.guards import OperationNotPermitted

PAYMENT_ID = uuid.uuid4()


class Session:
    def __init__(self, *, mode="SMARTPAY", status="APPROVED"):
        self.payment = SimpleNamespace(id=PAYMENT_ID, mode=mode, status=status, version=0)
        self.events = []

    async def get(self, _model, _id):
        return self.payment

    async def commit(self):
        self.events.append("commit")

    async def rollback(self):
        self.events.append("rollback")


def install_claim(monkeypatch, events, *, stale=False):
    async def transition(_session, **kwargs):
        events.append(("claim", kwargs["new_status"]))
        if stale:
            raise StalePaymentVersion("stale")
        return kwargs["expected_version"] + 1

    monkeypatch.setattr(service, "transition_payment", transition)


def gateway(events):
    paypal = SimpleNamespace()

    async def capture_order(_id, *, request_id):
        events.append("paypal_capture")
        return {"id": "capture-1", "status": "COMPLETED"}

    paypal.capture_order = AsyncMock(side_effect=capture_order)
    paypal.capture_authorization = AsyncMock(
        return_value={"id": "capture-2", "status": "COMPLETED"}
    )
    paypal.get_authorization = AsyncMock(
        return_value={
            "status": "CREATED",
            "expiration_time": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        }
    )
    paypal.void_authorization = AsyncMock(return_value={"http_status": 204})
    paypal.authorize_order = AsyncMock(return_value={"status": "COMPLETED"})
    return paypal


@pytest.mark.asyncio
async def test_smartpay_claim_is_committed_before_capture(monkeypatch):
    events = []
    session = Session()
    install_claim(monkeypatch, events)
    paypal = gateway(events)
    original_commit = session.commit

    async def commit():
        events.append("commit")
        await original_commit()

    session.commit = commit
    result = await service.execute_payment_operation(
        session,
        paypal,
        payment_id=PAYMENT_ID,
        operation="capture",
        provider_id="ORDER1",
        request_id="request-1",
        buyer_approved=True,
    )
    assert result["state"] == "CAPTURED"
    assert events[:3] == [("claim", "CAPTURE_REQUESTED"), "commit", "paypal_capture"]
    assert paypal.capture_order.await_count == 1


@pytest.mark.asyncio
async def test_losing_database_claim_cannot_call_paypal(monkeypatch):
    events = []
    session = Session()
    install_claim(monkeypatch, events, stale=True)
    paypal = gateway(events)
    with pytest.raises(StalePaymentVersion):
        await service.execute_payment_operation(
            session,
            paypal,
            payment_id=PAYMENT_ID,
            operation="capture",
            provider_id="ORDER1",
            request_id="request-1",
            buyer_approved=True,
        )
    assert paypal.capture_order.await_count == 0
    assert "rollback" in session.events


@pytest.mark.asyncio
async def test_unverified_condition_cannot_capture(monkeypatch):
    events = []
    install_claim(monkeypatch, events)
    session = Session(mode="CONDITIONALPAY", status="AWAITING_CONDITION")
    paypal = gateway(events)
    with pytest.raises(OperationNotPermitted):
        await service.execute_payment_operation(
            session,
            paypal,
            payment_id=PAYMENT_ID,
            operation="capture",
            provider_id="AUTH1",
            request_id="request-2",
            condition_verified=False,
        )
    assert paypal.capture_authorization.await_count == 0
    assert events == []


@pytest.mark.asyncio
async def test_expired_authorization_cannot_capture(monkeypatch):
    events = []
    install_claim(monkeypatch, events)
    session = Session(mode="CONDITIONALPAY", status="AWAITING_CONDITION")
    paypal = gateway(events)
    paypal.get_authorization.return_value = {
        "status": "CREATED",
        "expiration_time": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    }
    with pytest.raises(OperationNotPermitted):
        await service.execute_payment_operation(
            session,
            paypal,
            payment_id=PAYMENT_ID,
            operation="capture",
            provider_id="AUTH1",
            request_id="request-2",
            condition_verified=True,
        )
    paypal.get_authorization.assert_awaited_once()
    assert paypal.capture_authorization.await_count == 0
    assert events == []


@pytest.mark.asyncio
async def test_unknown_capture_result_is_persisted_for_reconciliation(monkeypatch):
    events = []
    install_claim(monkeypatch, events)
    session = Session()
    paypal = gateway(events)
    paypal.capture_order.side_effect = PayPalOutcomeUnknown("network timeout")
    with pytest.raises(PayPalOutcomeUnknown):
        await service.execute_payment_operation(
            session,
            paypal,
            payment_id=PAYMENT_ID,
            operation="capture",
            provider_id="ORDER1",
            request_id="request-1",
            buyer_approved=True,
        )
    assert session.payment.status == "UNCERTAIN"
    assert events == [("claim", "CAPTURE_REQUESTED"), ("claim", "UNCERTAIN")]
    assert paypal.capture_order.await_count == 1


@pytest.mark.asyncio
async def test_conditional_void(monkeypatch):
    events = []
    install_claim(monkeypatch, events)
    session = Session(mode="CONDITIONALPAY", status="AWAITING_CONDITION")
    paypal = gateway(events)
    result = await service.execute_payment_operation(
        session,
        paypal,
        payment_id=PAYMENT_ID,
        operation="void",
        provider_id="AUTH1",
        request_id="request-3",
    )
    assert result["state"] == "VOIDED"
    assert events == [("claim", "VOID_REQUESTED"), ("claim", "VOIDED")]
    paypal.void_authorization.assert_awaited_once()
