"""Offline PayPal gateway tests: no money operations hit the network."""

import httpx
import pytest

from backend.payments.paypal_client import (
    PayPalClient,
    PayPalError,
    PayPalOutcomeUnknown,
)


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/oauth2/token":
        return httpx.Response(200, json={"access_token": "test-token", "expires_in": 3600})
    if request.url.path == "/v2/checkout/orders" and request.method == "POST":
        assert request.headers["PayPal-Request-Id"] == "order-123"
        assert request.headers["Authorization"] == "Bearer test-token"
        import json

        payload = json.loads(request.content)
        assert payload["purchase_units"][0]["amount"]["value"] == "10.50"
        return httpx.Response(201, json={"id": "ORDER123", "status": "CREATED"})
    if request.url.path == "/v2/payments/authorizations/AUTH123/void":
        assert request.headers["PayPal-Request-Id"] == "void-123"
        return httpx.Response(204)
    if request.url.path.endswith("/capture"):
        raise httpx.ReadTimeout("simulated timeout")
    return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})


def client(**kwargs):
    return PayPalClient(
        client_id="sandbox-id",
        client_secret="sandbox-secret",
        base_url="https://api-m.sandbox.paypal.com",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_rejects_live_endpoint_and_missing_credentials():
    with pytest.raises(ValueError, match="sandbox"):
        PayPalClient(client_id="a", client_secret="b", base_url="https://api-m.paypal.com")
    with pytest.raises(ValueError, match="Missing"):
        PayPalClient(client_id="", client_secret="", base_url="https://api-m.sandbox.paypal.com")


@pytest.mark.asyncio
async def test_oauth_create_order_and_get_order():
    async with client() as paypal:
        assert await paypal.check_authentication()
        response = await paypal.create_order(
            intent="CAPTURE", amount="10.50", currency="USD", request_id="order-123"
        )
        assert response["id"] == "ORDER123"
        with pytest.raises(PayPalError) as exc:
            await paypal.get_order("NOTFOUND")
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_void_204_and_capture_timeout_is_unknown():
    async with client() as paypal:
        assert (await paypal.void_authorization("AUTH123", request_id="void-123")) == {
            "http_status": 204
        }
        with pytest.raises(PayPalOutcomeUnknown):
            await paypal.capture_authorization("AUTH123", request_id="capture-123")


@pytest.mark.asyncio
async def test_no_financial_call_without_id_and_validation():
    async with client() as paypal:
        with pytest.raises(ValueError, match="request_id"):
            await paypal.create_order(intent="CAPTURE", amount="10", currency="USD", request_id="")
        with pytest.raises(ValueError, match="Invalid order_id"):
            await paypal.get_order("../../secrets")
        with pytest.raises(ValueError, match="Amount"):
            await paypal.create_order(
                intent="CAPTURE", amount="-1", currency="USD", request_id="order-123"
            )

