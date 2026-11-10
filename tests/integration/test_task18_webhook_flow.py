"""Task 18 webhook integration with PostgreSQL and simulated PayPal."""

import json
import os
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from backend.api import routes_extensions
from backend.api.deps import install_error_handlers
from backend.extensions.signed_conditions import sign_event
from tests.integration.sim_support import CONDITIONAL, OWNER

SECRET = "task18-integration-test-secret-" + "x" * 32

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1",
        reason="Requires local PostgreSQL integration tests",
    ),
]


def make_app(sim, monkeypatch):
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(routes_extensions.router)
    app.state.payment_api = sim.api

    app.dependency_overrides[
        routes_extensions.get_extension_redis
    ] = lambda: sim.redis

    monkeypatch.setattr(
        routes_extensions,
        "get_settings",
        lambda: SimpleNamespace(CONDITION_WEBHOOK_SECRET=SECRET),
    )
    return app


def signed_request(payment_id, verified=True, owner=OWNER):
    event_id = "task18-" + uuid.uuid4().hex
    timestamp = int(time.time())

    body = json.dumps(
        {
            "payment_id": str(payment_id),
            "owner_id": owner,
            "verified": verified,
        },
        separators=(",", ":"),
    ).encode("utf-8")

    signature = sign_event(SECRET, timestamp, event_id, body)

    headers = {
        "Content-Type": "application/json",
        "X-SlothPay-Timestamp": str(timestamp),
        "X-SlothPay-Event-Id": event_id,
        "X-SlothPay-Signature": signature,
    }

    return body, headers


async def create_held_payment(sim):
    view, _ = await sim.pay(payload=CONDITIONAL)
    await sim.approve(view)
    held = await sim.api.buyer_returned(OWNER, uuid.UUID(view.id))
    assert held.status == "AWAITING_CONDITION"
    return view


async def deliver(client, body, headers):
    return await client.post(
        "/api/extensions/zapier/verify",
        content=body,
        headers=headers,
    )


@pytest.mark.parametrize(
    "verified,expected,operation",
    [
        (True, "CAPTURED", "captures"),
        (False, "VOIDED", "voids"),
    ],
)
async def test_signed_webhook_resolves_simulated_payment(
    sim, monkeypatch, verified, expected, operation
):
    view = await create_held_payment(sim)
    app = make_app(sim, monkeypatch)

    body, headers = signed_request(view.id, verified=verified)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await deliver(client, body, headers)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == expected
    assert await sim.status(view) == expected

    stats = await sim.stats()
    assert stats[operation] == 1
    assert stats["captures" if not verified else "voids"] == 0

    passport = await sim.assert_passport_ok(view)
    assert passport["integrity"]["chain_valid"] is True


async def test_replayed_webhook_does_not_capture_twice(sim, monkeypatch):
    view = await create_held_payment(sim)
    app = make_app(sim, monkeypatch)
    body, headers = signed_request(view.id)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first = await deliver(client, body, headers)
        second = await deliver(client, body, headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert (await sim.stats())["captures"] == 1


async def test_tampered_webhook_does_not_capture(sim, monkeypatch):
    view = await create_held_payment(sim)
    app = make_app(sim, monkeypatch)
    body, headers = signed_request(view.id)

    tampered = body.replace(b"true", b"false")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await deliver(client, tampered, headers)

    assert response.status_code == 401, response.text
    assert await sim.status(view) == "AWAITING_CONDITION"
    assert (await sim.stats())["captures"] == 0


async def test_wrong_owner_cannot_resolve_payment(sim, monkeypatch):
    view = await create_held_payment(sim)
    app = make_app(sim, monkeypatch)
    body, headers = signed_request(view.id, owner="unrelated-owner")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await deliver(client, body, headers)

    assert response.status_code == 404, response.text
    assert await sim.status(view) == "AWAITING_CONDITION"
    assert (await sim.stats())["captures"] == 0
