"""Mocked PayPal postback verification; no real credentials or network."""

import httpx
import pytest

from backend.config.settings import get_settings
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
EVENT = {"id": "WH-123", "event_type": "PAYMENT.CAPTURE.COMPLETED"}


@pytest.fixture(autouse=True)
def configure(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "PAYPAL_CLIENT_ID", "test-client")
    monkeypatch.setattr(s, "PAYPAL_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(s, "PAYPAL_WEBHOOK_ID", "test-webhook")
    monkeypatch.setattr(s, "PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")


def make_client(status="SUCCESS"):
    def handler(req):
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "test-token"})
        if req.url.path == "/v1/notifications/verify-webhook-signature":
            assert req.headers["authorization"] == "Bearer test-token"
            body = __import__("json").loads(req.content)
            assert body["webhook_id"] == "test-webhook"
            assert body["webhook_event"] == EVENT
            return httpx.Response(200, json={"verification_status": status})
        raise AssertionError(req.url.path)

    return httpx.AsyncClient(
        base_url="https://api-m.sandbox.paypal.com",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_success():
    async with make_client() as client:
        assert await verify_paypal_webhook(HEADERS, EVENT, http=client)


@pytest.mark.asyncio
async def test_invalid_signature():
    async with make_client("FAILURE") as client:
        assert not await verify_paypal_webhook(HEADERS, EVENT, http=client)


@pytest.mark.asyncio
async def test_missing_header_is_rejected_without_network():
    async with make_client() as client:
        assert not await verify_paypal_webhook({}, EVENT, http=client)


@pytest.mark.asyncio
async def test_verification_service_error_fails_closed():
    async def fail(_):
        raise httpx.ConnectError("offline")

    async with httpx.AsyncClient(
        base_url="https://api-m.sandbox.paypal.com",
        transport=httpx.MockTransport(fail),
    ) as client:
        with pytest.raises(WebhookVerificationUnavailable):
            await verify_paypal_webhook(HEADERS, EVENT, http=client)
