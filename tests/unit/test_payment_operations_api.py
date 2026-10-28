"""Offline checks: admin protection, no blind retry, no condition bypass."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.payment_operations import get_paypal, get_session, router
from backend.config.settings import get_settings


def setup_client(monkeypatch, payment=None):
    monkeypatch.setenv("ADMIN_API_KEY", "test-only-admin-key")
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(router)
    session = AsyncMock()
    session.get.return_value = payment
    gateway = SimpleNamespace()

    async def session_override():
        yield session

    async def paypal_override():
        yield gateway

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_paypal] = paypal_override
    return TestClient(app), session, gateway


def test_missing_admin_key_is_denied(monkeypatch):
    client, _, _ = setup_client(monkeypatch)
    response = client.post(f"/internal/payments/{uuid.uuid4()}/create-order")
    assert response.status_code == 403
    get_settings.cache_clear()


def test_valid_admin_order_creation(monkeypatch):
    client, _, gateway = setup_client(monkeypatch)
    gateway.create_order = AsyncMock(
        return_value={"state": "ORDER_CREATED", "paypal_order_id": "O1"}
    )
    # Patch actual orchestration function, not the gateway's unrelated method.
    from backend.api import payment_operations

    action = AsyncMock(return_value={"state": "ORDER_CREATED", "paypal_order_id": "O1"})
    monkeypatch.setattr(payment_operations, "create_order", action)
    response = client.post(
        f"/internal/payments/{uuid.uuid4()}/create-order",
        headers={"X-Admin-Api-Key": "test-only-admin-key"},
    )
    assert response.status_code == 200
    assert response.json()["paypal_order_id"] == "O1"
    action.assert_awaited_once()
    get_settings.cache_clear()


def test_conditional_capture_is_blocked(monkeypatch):
    payment = SimpleNamespace(mode="CONDITIONALPAY")
    client, _, _ = setup_client(monkeypatch, payment)
    response = client.post(
        f"/internal/payments/{uuid.uuid4()}/operations/capture",
        headers={"X-Admin-Api-Key": "test-only-admin-key"},
    )
    assert response.status_code == 409
    get_settings.cache_clear()


def test_uncertain_gateway_does_not_retry(monkeypatch):
    from backend.api import payment_operations
    from backend.payments.paypal_client import PayPalOutcomeUnknown

    client, _, _ = setup_client(monkeypatch)
    action = AsyncMock(side_effect=PayPalOutcomeUnknown("timeout"))
    monkeypatch.setattr(payment_operations, "create_order", action)
    response = client.post(
        f"/internal/payments/{uuid.uuid4()}/create-order",
        headers={"X-Admin-Api-Key": "test-only-admin-key"},
    )
    assert response.status_code == 503
    action.assert_awaited_once()
    get_settings.cache_clear()
