from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI

from backend.api.deps import install_error_handlers
from backend.api.passport import build_passport
from backend.api.payments_service import DbPaymentApi
from backend.api.routes_passport import DbPassportSource
from backend.api.routes_payments import router as payments_router
from backend.models.payment import Payment
from backend.notifications.hub import InMemoryHub
from backend.orchestration.conditions import PaymentService
from backend.payments.hooks import NOOP
from backend.payments.paypal_client import SANDBOX_URL, PayPalClient
from tests.fake_paypal.app import create_app

OWNER = "owner-sim-a"
SMART = {"path": "SMARTPAY", "amount": "10.00", "currency": "USD", "description": None}
CONDITIONAL = {
    "path": "CONDITIONALPAY",
    "amount": "20.00",
    "currency": "USD",
    "description": None,
    "condition": {"kind": "delivery", "description": "Parcel arrives", "deadline_minutes": 1},
}


class SimWorld:
    """SlothPay's real API and database, talking to a fresh fake PayPal."""

    def __init__(self, maker, redis) -> None:
        self.maker = maker
        self.redis = redis
        self.fake = create_app()
        self.sim = self.fake.state.sim
        self.starter = SimpleNamespace(start=AsyncMock(return_value=True))
        self.api = DbPaymentApi(
            session_factory=maker,
            redis=redis,
            hub=InMemoryHub(),
            paypal_factory=self.client,
            starter=self.starter,
        )

    def client(self) -> PayPalClient:
        return PayPalClient(
            client_id="sim-client",
            client_secret="sim-secret",
            base_url=SANDBOX_URL,
            transport=httpx.ASGITransport(app=self.fake),
            injector=NOOP,
        )

    @asynccontextmanager
    async def control(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.fake), base_url="http://sim"
        ) as http:
            yield http

    async def faults(self, **body) -> None:
        async with self.control() as http:
            assert (await http.post("/_control/faults", json=body)).status_code == 200

    async def stats(self) -> dict:
        async with self.control() as http:
            return (await http.get("/_stats")).json()

    async def order_id(self, view) -> str:
        async with self.maker() as session:
            payment = await session.get(Payment, uuid.UUID(view.id))
            assert payment.paypal_order_id, "the payment has no PayPal order yet"
            return payment.paypal_order_id

    async def approve(self, view) -> None:
        """The buyer clicks Approve at PayPal (a human step in real life)."""
        order = await self.order_id(view)
        async with self.control() as http:
            assert (await http.post(f"/_control/orders/{order}/approve")).status_code == 200

    async def status(self, view) -> str:
        async with self.maker() as session:
            return (await session.get(Payment, uuid.UUID(view.id), populate_existing=True)).status

    async def pay(self, owner=OWNER, key=None, payload=None):
        return await self.api.create_payment(
            owner, key or f"key-{uuid.uuid4().hex}", dict(payload or SMART)
        )

    async def finish_condition(self, view, *, verified: bool, reason: str | None = None) -> None:
        """What the ConditionalPay timer does when it fires (the Temporal timer itself is not
        run here; tests/integration/test_workflows_db.py covers it with a time-skipping server)."""
        async with self.client() as gateway:
            service = PaymentService(self.maker, gateway)
            if reason == "deadline":
                await service.mark_condition_expired(view.id, "deadline")
            await service.resolve_condition(view.id, verified, {}, reason)

    def http_app(self) -> FastAPI:
        app = FastAPI()
        install_error_handlers(app)
        app.include_router(payments_router)
        app.state.payment_api = self.api
        return app

    async def passport(self, view, *, shared=False) -> dict:
        data = await DbPassportSource(self.maker).load(uuid.UUID(view.id))
        assert data is not None
        return build_passport(
            data["intent"],
            data["events"],
            data["attempts"],
            data["condition"],
            data["provider_events"],
            shared=shared,
        )

    async def assert_passport_ok(self, view) -> dict:
        """Scenario 8: the hash chain is valid and a Recorded entry is never shown as Confirmed."""
        passport = await self.passport(view)
        assert passport["integrity"]["chain_valid"] is True
        for entry in passport["timeline"]:
            source = entry["source"].lower()
            from_paypal = source.startswith("paypal") or "reconcil" in source
            # Confirmed means PayPal-sourced; a SlothPay-only note must never claim to be.
            assert entry["confirmed"] is from_paypal or not entry["confirmed"], entry
            if entry["confirmed"]:
                assert from_paypal, entry
        # And the shared view carries no owner id.
        shared = await self.passport(view, shared=True)
        assert OWNER not in str(shared)
        return passport


async def build_world(maker) -> SimWorld:
    return SimWorld(maker, FakeAsyncRedis(decode_responses=True))