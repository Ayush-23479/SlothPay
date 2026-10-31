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


# ---------------------------------------------------------------- Task 8: rescue endpoints
HEADERS = {"X-Admin-Api-Key": "test-only-admin-key"}


def test_rescue_endpoints_require_the_admin_key(monkeypatch):
    client, _, _ = setup_client(monkeypatch)
    payment_id = uuid.uuid4()
    assert client.get(f"/internal/payments/{payment_id}/rescue-plan").status_code == 403
    response = client.post(
        f"/internal/payments/{payment_id}/rescue-action", json={"action": "RECONCILE"}
    )
    assert response.status_code == 403
    get_settings.cache_clear()


def test_rescue_plan_returns_the_plan_with_retry_allowed(monkeypatch):
    from backend.api import payment_operations

    client, _, _ = setup_client(monkeypatch)
    plan = {
        "payment_id": "p1",
        "payment_state": "APPROVED",
        "buyer_facing_state": "Pending",
        "allowed_actions": ["RETRY_SAME_REQUEST"],
        "recommended_action": "RETRY_SAME_REQUEST",
        "evidence_reason": "PayPal confirms the operation did not happen",
        "retry_allowed": True,
    }
    monkeypatch.setattr(payment_operations, "get_rescue_plan", AsyncMock(return_value=plan))
    response = client.get(f"/internal/payments/{uuid.uuid4()}/rescue-plan", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["retry_allowed"] is True
    get_settings.cache_clear()


def test_rescue_plan_for_an_unknown_payment_is_404(monkeypatch):
    from backend.api import payment_operations

    client, _, _ = setup_client(monkeypatch)
    monkeypatch.setattr(
        payment_operations, "get_rescue_plan", AsyncMock(side_effect=ValueError("missing"))
    )
    response = client.get(f"/internal/payments/{uuid.uuid4()}/rescue-plan", headers=HEADERS)
    assert response.status_code == 404
    get_settings.cache_clear()


def test_a_forbidden_rescue_action_is_a_409_and_an_unknown_one_is_a_422(monkeypatch):
    from backend.api import payment_operations

    client, _, _ = setup_client(monkeypatch)
    run = AsyncMock(return_value={"error": "Action not permitted"})
    monkeypatch.setattr(payment_operations, "execute_rescue_action", run)
    url = f"/internal/payments/{uuid.uuid4()}/rescue-action"
    assert (
        client.post(url, json={"action": "RETRY_SAME_REQUEST"}, headers=HEADERS).status_code == 409
    )
    assert client.post(url, json={"action": "DROP_TABLE"}, headers=HEADERS).status_code == 422
    run.assert_awaited_once()
    get_settings.cache_clear()


def test_a_permitted_rescue_action_returns_its_result(monkeypatch):
    from backend.api import payment_operations

    client, _, _ = setup_client(monkeypatch)
    run = AsyncMock(return_value={"action": "RECONCILE", "result": {"state": "CAPTURED"}})
    monkeypatch.setattr(payment_operations, "execute_rescue_action", run)
    response = client.post(
        f"/internal/payments/{uuid.uuid4()}/rescue-action",
        json={"action": "RECONCILE"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["result"]["state"] == "CAPTURED"
    get_settings.cache_clear()