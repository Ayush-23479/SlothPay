"""Task 18 extension API contract tests (mocked PaymentApi; no real PayPal)."""

from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI

from backend.api import routes_extensions
from backend.api.deps import get_api, install_error_handlers
from backend.extensions.signed_conditions import sign_event

SECRET = "s" * 48
OWNER = "owner-a"


class FakePayments:
    def __init__(self):
        self.id = uuid.uuid4()
        self.status = "AWAITING_CONDITION"
        self.owner = OWNER
        self.path = "CONDITIONALPAY"
        self.actions = []

    async def get_payment(self, owner, payment_id):
        if owner != self.owner or payment_id != self.id:
            return None
        return SimpleNamespace(path=self.path, status=self.status)

    async def verify_condition(self, caller, payment_id, verified, evidence):
        assert caller.owner_id == self.owner
        assert caller.is_admin is False
        assert payment_id == self.id
        self.actions.append((verified, evidence))
        self.status = "CAPTURED" if verified else "VOIDED"
        return SimpleNamespace(status=self.status)


@pytest.fixture
def setup(monkeypatch):
    fake = FakePayments()
    redis = FakeAsyncRedis()
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(routes_extensions.router)
    app.dependency_overrides[get_api] = lambda: fake
    app.dependency_overrides[routes_extensions.get_extension_redis] = lambda: redis
    monkeypatch.setattr(
        routes_extensions, "get_settings", lambda: SimpleNamespace(CONDITION_WEBHOOK_SECRET=SECRET)
    )
    return app, fake, redis


def request_data(fake, *, verified=True, owner=OWNER, event_id="event-00000001"):
    body = json.dumps({
        "payment_id": str(fake.id), "owner_id": owner, "verified": verified
    }, separators=(",", ":")).encode()
    now = int(time.time())
    signature = sign_event(SECRET, now, event_id, body)
    return body, {
        "X-SlothPay-Timestamp": str(now),
        "X-SlothPay-Event-Id": event_id,
        "X-SlothPay-Signature": signature,
        "Content-Type": "application/json",
    }


async def submit(app, body, headers):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/extensions/zapier/verify", content=body, headers=headers)


@pytest.mark.asyncio
@pytest.mark.parametrize("verified,expected", [(True, "CAPTURED"), (False, "VOIDED")])
async def test_signed_event_calls_existing_service_once(setup, verified, expected):
    app, fake, _ = setup
    body, headers = request_data(fake, verified=verified)
    response = await submit(app, body, headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == expected
    assert len(fake.actions) == 1
    assert fake.actions[0][1]["source"] == "external_signed_webhook"


@pytest.mark.asyncio
async def test_tampered_payload_rejected_without_side_effect(setup):
    app, fake, _ = setup
    body, headers = request_data(fake)
    altered = body.replace(b"true", b"false")
    response = await submit(app, altered, headers)
    assert response.status_code == 401
    assert fake.actions == []


@pytest.mark.asyncio
async def test_wrong_owner_cannot_resolve_payment(setup):
    app, fake, _ = setup
    body, headers = request_data(fake, owner="owner-b")
    response = await submit(app, body, headers)
    assert response.status_code == 404
    assert fake.actions == []


@pytest.mark.asyncio
async def test_replay_same_event_does_not_call_twice(setup):
    app, fake, _ = setup
    body, headers = request_data(fake)
    first = await submit(app, body, headers)
    again = await submit(app, body, headers)
    assert first.status_code == 200
    assert again.status_code == 409
    assert len(fake.actions) == 1


@pytest.mark.asyncio
async def test_different_event_cannot_resolve_same_payment(setup):
    app, fake, _ = setup
    body, headers = request_data(fake)
    first = await submit(app, body, headers)
    body2, headers2 = request_data(fake, event_id="event-00000002")
    again = await submit(app, body2, headers2)
    assert first.status_code == 200
    assert again.status_code == 409
    assert len(fake.actions) == 1


@pytest.mark.asyncio
async def test_not_held_rejected(setup):
    app, fake, _ = setup
    fake.status = "AWAITING_APPROVAL"
    body, headers = request_data(fake)
    response = await submit(app, body, headers)
    assert response.status_code == 409
    assert fake.actions == []


@pytest.mark.asyncio
async def test_disabled_when_secret_missing(setup, monkeypatch):
    app, fake, _ = setup
    monkeypatch.setattr(
        routes_extensions, "get_settings", lambda: SimpleNamespace(CONDITION_WEBHOOK_SECRET="")
    )
    body, headers = request_data(fake)
    response = await submit(app, body, headers)
    assert response.status_code == 503
    assert fake.actions == []


@pytest.mark.asyncio
async def test_invalid_timestamp_rejected(setup):
    app, fake, _ = setup
    body, headers = request_data(fake)
    headers["X-SlothPay-Timestamp"] = str(int(time.time()) - 900)
    response = await submit(app, body, headers)
    assert response.status_code == 401
    assert fake.actions == []
