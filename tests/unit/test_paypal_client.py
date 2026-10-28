"""Offline PayPal gateway tests: no money operations hit the network."""

import json

import httpx
import pytest

from backend.payments.paypal_client import (
    DEFAULT_TIMEOUT_SECONDS,
    PayPalClient,
    PayPalError,
    PayPalNotSent,
    PayPalOutcomeUnknown,
)

TOKEN_PATH = "/v1/oauth2/token"


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == TOKEN_PATH:
        return httpx.Response(200, json={"access_token": "test-token", "expires_in": 3600})
    if request.url.path == "/v2/checkout/orders" and request.method == "POST":
        assert request.headers["PayPal-Request-Id"] == "order-123"
        assert request.headers["Authorization"] == "Bearer test-token"
        payload = json.loads(request.content)
        assert payload["purchase_units"][0]["amount"]["value"] == "10.50"
        return httpx.Response(201, json={"id": "ORDER123", "status": "CREATED"})
    if request.url.path == "/v2/payments/authorizations/AUTH123/void":
        assert request.headers["PayPal-Request-Id"] == "void-123"
        return httpx.Response(204)
    if request.url.path.endswith("/capture"):
        raise httpx.ReadTimeout("simulated timeout")
    return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})


def make_client(request_handler=handler, **kwargs):
    return PayPalClient(
        client_id="sandbox-id",
        client_secret="sandbox-secret",
        base_url="https://api-m.sandbox.paypal.com",
        transport=httpx.MockTransport(request_handler),
        **kwargs,
    )


def client(**kwargs):
    return make_client(handler, **kwargs)


def test_rejects_live_endpoint_and_missing_credentials():
    with pytest.raises(ValueError, match="sandbox"):
        PayPalClient(client_id="a", client_secret="b", base_url="https://api-m.paypal.com")
    with pytest.raises(ValueError, match="Missing"):
        PayPalClient(client_id="", client_secret="", base_url="https://api-m.sandbox.paypal.com")


def test_default_timeout_is_three_seconds():
    assert DEFAULT_TIMEOUT_SECONDS == 3.0
    paypal = client()
    assert paypal._http.timeout == httpx.Timeout(3.0)


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
        assert exc.value.error_name == "RESOURCE_NOT_FOUND"


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


@pytest.mark.asyncio
async def test_error_keeps_body_issue_and_debug_id():
    def declined(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(
            422,
            headers={"PayPal-Debug-Id": "debug-abc"},
            json={
                "name": "UNPROCESSABLE_ENTITY",
                "details": [{"issue": "INSTRUMENT_DECLINED"}],
            },
        )

    async with make_client(declined) as paypal:
        with pytest.raises(PayPalError) as exc:
            await paypal.capture_order("ORDER123", request_id="cap-1")
    err = exc.value
    assert err.status_code == 422
    assert err.issue == "INSTRUMENT_DECLINED"
    assert err.error_name == "UNPROCESSABLE_ENTITY"
    assert err.debug_id == "debug-abc"
    assert err.body["details"][0]["issue"] == "INSTRUMENT_DECLINED"


@pytest.mark.asyncio
async def test_error_without_json_body_still_works():
    def plain(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(400, content=b"not json")

    async with make_client(plain) as paypal:
        with pytest.raises(PayPalError) as exc:
            await paypal.get_order("ORDER123")
    assert exc.value.status_code == 400
    assert exc.value.body == {}
    assert exc.value.issue is None
    assert exc.value.debug_id is None


@pytest.mark.asyncio
async def test_oauth_network_failure_on_write_is_not_sent_not_unknown():
    seen: list[str] = []

    def oauth_down(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        raise httpx.ConnectError("dns down")

    async with make_client(oauth_down) as paypal:
        with pytest.raises(PayPalNotSent):
            await paypal.capture_authorization("AUTH123", request_id="cap-1")
    # Only the token request was attempted; the money request was never sent.
    assert seen == [TOKEN_PATH]


@pytest.mark.asyncio
async def test_validation_runs_before_any_network_call():
    seen: list[str] = []

    def spy(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})

    async with make_client(spy) as paypal:
        with pytest.raises(ValueError, match="request_id"):
            await paypal.capture_order("ORDER123", request_id="")
        with pytest.raises(ValueError, match="Invalid request_id"):
            await paypal.capture_order("ORDER123", request_id="bad id!")
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 503])
async def test_server_error_on_write_is_outcome_unknown(status):
    def server_error(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(status, headers={"PayPal-Debug-Id": "dbg-5xx"})

    async with make_client(server_error) as paypal:
        with pytest.raises(PayPalOutcomeUnknown) as exc:
            await paypal.capture_order("ORDER123", request_id="cap-1")
    assert exc.value.debug_id == "dbg-5xx"


@pytest.mark.asyncio
async def test_malformed_success_body_is_outcome_unknown():
    def malformed(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, content=b"not json")

    async with make_client(malformed) as paypal:
        with pytest.raises(PayPalOutcomeUnknown):
            await paypal.capture_order("ORDER123", request_id="cap-1")


@pytest.mark.asyncio
async def test_token_is_cached_and_request_id_sent_on_every_write():
    token_calls = 0
    request_ids: dict[str, str | None] = {}

    def recording(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.url.path == TOKEN_PATH:
            token_calls += 1
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        request_ids[request.url.path] = request.headers.get("PayPal-Request-Id")
        return httpx.Response(200, json={"ok": True})

    async with make_client(recording) as paypal:
        await paypal.authorize_order("O1", request_id="auth-1")
        await paypal.capture_order("O1", request_id="cap-1")
        await paypal.capture_authorization("A1", request_id="cap-2")
        await paypal.void_authorization("A1", request_id="void-1")
        await paypal.get_order("O1")
    assert token_calls == 1
    assert request_ids == {
        "/v2/checkout/orders/O1/authorize": "auth-1",
        "/v2/checkout/orders/O1/capture": "cap-1",
        "/v2/payments/authorizations/A1/capture": "cap-2",
        "/v2/payments/authorizations/A1/void": "void-1",
        "/v2/checkout/orders/O1": None,  # reads carry no request id
    }
