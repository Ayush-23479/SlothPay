import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.config.settings import get_settings
from backend.webhooks import receiver
from backend.webhooks.events_map import (
    EVENT_MAP,
    extract_amount,
    extract_order_id,
    extract_resource_id,
    mask_payload,
)
from backend.webhooks.handler import WebhookDeps
from backend.webhooks.verification import (
    WebhookVerificationUnavailable,
    verify_paypal_webhook,
)

HEADERS = {
    "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    "PAYPAL-CERT-URL": "https://api-m.sandbox.paypal.com/cert",
    "PAYPAL-TRANSMISSION-ID": "abc",
    "PAYPAL-TRANSMISSION-SIG": "signed",
    "PAYPAL-TRANSMISSION-TIME": "2026-10-29T00:00:00Z",
}
# Deliberately odd spacing and key order: the bytes PayPal signed must reach PayPal as-is.
RAW = b'{ "id":"WH-1",  "event_type":"PAYMENT.CAPTURE.COMPLETED", "resource":{"id":"CAP-1"} }'


@pytest.fixture(autouse=True)
def configure(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "PAYPAL_CLIENT_ID", "test-client")
    monkeypatch.setattr(s, "PAYPAL_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(s, "PAYPAL_WEBHOOK_ID", "test-webhook")
    monkeypatch.setattr(s, "PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")


def mock_paypal(status="SUCCESS", seen=None):
    def handler(req):
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok"})
        if req.url.path == "/v1/notifications/verify-webhook-signature":
            if seen is not None:
                seen.append(req.content)
            return httpx.Response(200, json={"verification_status": status})
        raise AssertionError(req.url.path)

    return httpx.AsyncClient(
        base_url="https://api-m.sandbox.paypal.com", transport=httpx.MockTransport(handler)
    )


# ----------------------------------------------------------------- verification
async def test_the_raw_bytes_are_what_gets_verified():
    seen: list[bytes] = []
    async with mock_paypal(seen=seen) as client:
        assert await verify_paypal_webhook(HEADERS, RAW, http=client)
    sent = seen[0]
    assert RAW in sent, "the exact signed bytes (odd spacing included) reach PayPal"
    body = json.loads(sent)
    assert body["webhook_id"] == "test-webhook"
    assert body["transmission_id"] == "abc"
    assert body["webhook_event"]["id"] == "WH-1"


async def test_failure_status_is_not_verified():
    async with mock_paypal("FAILURE") as client:
        assert not await verify_paypal_webhook(HEADERS, RAW, http=client)


async def test_missing_signature_header_is_rejected_without_calling_paypal():
    async with mock_paypal() as client:
        broken = {k: v for k, v in HEADERS.items() if k != "PAYPAL-TRANSMISSION-SIG"}
        assert not await verify_paypal_webhook(broken, RAW, http=client)


@pytest.mark.parametrize("body", [b"", b"not json", b"[1,2]", b"\xff\xfe"])
async def test_a_body_that_is_not_a_json_object_is_not_verified(body):
    async with mock_paypal() as client:
        assert not await verify_paypal_webhook(HEADERS, body, http=client)


async def test_paypal_being_down_is_unavailable_not_forged():
    async def fail(_):
        raise httpx.ConnectError("offline")

    async with httpx.AsyncClient(
        base_url="https://api-m.sandbox.paypal.com", transport=httpx.MockTransport(fail)
    ) as client:
        with pytest.raises(WebhookVerificationUnavailable):
            await verify_paypal_webhook(HEADERS, RAW, http=client)


async def test_a_live_paypal_host_is_refused(monkeypatch):
    monkeypatch.setattr(get_settings(), "PAYPAL_BASE_URL", "https://api-m.paypal.com")
    with pytest.raises(WebhookVerificationUnavailable):
        await verify_paypal_webhook(HEADERS, RAW)


# ------------------------------------------------------------------- events map
def test_event_map_has_exactly_the_planned_events():
    assert set(EVENT_MAP) == {
        "CHECKOUT.ORDER.APPROVED",
        "PAYMENT.AUTHORIZATION.CREATED",
        "PAYMENT.AUTHORIZATION.VOIDED",
        "PAYMENT.CAPTURE.PENDING",
        "PAYMENT.CAPTURE.COMPLETED",
        "PAYMENT.CAPTURE.DENIED",
    }


def test_order_id_prefers_related_ids_then_order_resource_id():
    capture = {
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {"id": "CAP-1", "supplementary_data": {"related_ids": {"order_id": "ORD-1"}}},
    }
    assert extract_order_id(capture) == "ORD-1"
    approved = {"event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": "ORD-2"}}
    assert extract_order_id(approved) == "ORD-2"
    # A capture's own id is NOT an order id.
    assert (
        extract_order_id({"event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {"id": "C"}})
        is None
    )
    assert extract_order_id({"resource": "junk"}) is None
    assert extract_order_id("junk") is None


def test_resource_id_and_amount_extraction():
    event = {"resource": {"id": "CAP-1", "amount": {"value": "15.00", "currency_code": "usd"}}}
    assert extract_resource_id(event) == "CAP-1"
    value, currency = extract_amount(event)
    assert str(value) == "15.00" and currency == "USD"
    order = {
        "resource": {"purchase_units": [{"amount": {"value": "9.50", "currency_code": "USD"}}]}
    }
    assert str(extract_amount(order)[0]) == "9.50"
    assert (
        extract_amount({"resource": {"amount": {"value": "abc", "currency_code": "USD"}}}) is None
    )
    assert extract_amount({}) is None


def test_masking_removes_emails_names_and_addresses_at_any_depth():
    event = {
        "id": "WH-1",
        "summary": "Payment by buyer@example.com",
        "resource": {
            "id": "CAP-1",
            "amount": {"value": "15.00", "currency_code": "USD"},
            "payer": {
                "email_address": "buyer@example.com",
                "name": {"given_name": "Ada", "surname": "Lovelace"},
                "address": {"address_line_1": "1 Main St", "postal_code": "12345"},
            },
            "purchase_units": [{"shipping": {"name": {"full_name": "Ada"}}, "reference_id": "r"}],
        },
    }
    masked = json.dumps(mask_payload(event))
    for leaked in ("buyer@example.com", "Ada", "Lovelace", "1 Main St", "12345"):
        assert leaked not in masked
    assert '"CAP-1"' in masked and '"15.00"' in masked and '"reference_id"' in masked
    assert "buyer@example.com" in json.dumps(event), "the original is not modified"


# --------------------------------------------------------------------- receiver
@pytest.fixture
def client_and_calls(monkeypatch):
    calls = []

    async def fake_process(headers, raw, deps):
        calls.append((headers, raw))
        return "rejected"

    monkeypatch.setattr(receiver, "process_webhook", fake_process)
    app = FastAPI()
    app.include_router(receiver.router)
    app.dependency_overrides[receiver.get_webhook_deps] = lambda: WebhookDeps(
        session_factory=lambda: None
    )
    return TestClient(app), calls


def test_receiver_answers_200_and_passes_the_request_bytes_and_headers(client_and_calls):
    client, calls = client_and_calls
    response = client.post("/webhooks/paypal", content=RAW, headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"status": "received"}
    assert calls[0][1] == RAW, "the handler gets the exact request bytes"
    assert calls[0][0]["paypal-transmission-id"] == "abc"


def test_receiver_returns_200_even_when_processing_rejects_or_crashes(monkeypatch):
    async def boom(headers, raw, deps):
        raise RuntimeError("database down")

    monkeypatch.setattr(receiver, "process_webhook", boom)
    app = FastAPI()
    app.include_router(receiver.router)
    app.dependency_overrides[receiver.get_webhook_deps] = lambda: WebhookDeps(
        session_factory=lambda: None
    )
    client = TestClient(app)
    response = client.post("/webhooks/paypal", content=b"garbage", headers={})
    assert response.status_code == 200
    assert "verif" not in response.text.lower(), "never explains a verification failure"


def test_receiver_refuses_an_oversized_body(client_and_calls):
    client, calls = client_and_calls
    response = client.post("/webhooks/paypal", content=b"x" * (receiver.MAX_BODY_BYTES + 1))
    assert response.status_code == 413
    assert calls == []