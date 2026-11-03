import json
import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI

from backend.api.deps import Caller, install_error_handlers
from backend.api.passport import build_passport
from backend.api.payments_service import DbPaymentApi
from backend.api.routes_passport import DbPassportSource
from backend.api.routes_passport import router as passport_router
from backend.models.event import ProviderEvent
from backend.notifications.hub import InMemoryHub
from backend.payments.paypal_client import PayPalOutcomeUnknown
from tests.integration.test_api_db import (
    CONDITIONAL,
    OTHER,
    OWNER,
    SMART,
    _gateway_context,
    fake_paypal,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1",
        reason="Set SLOTHPAY_RUN_DB_TESTS=1 for local PostgreSQL tests",
    ),
]


@pytest_asyncio.fixture
async def world(isolated_maker):
    paypal = fake_paypal()
    redis = FakeAsyncRedis(decode_responses=True)
    api = DbPaymentApi(
        session_factory=isolated_maker,
        redis=redis,
        hub=InMemoryHub(),
        paypal_factory=lambda: _gateway_context(paypal),
        starter=SimpleNamespace(start=AsyncMock(return_value=True)),
    )
    yield SimpleNamespace(api=api, paypal=paypal, maker=isolated_maker)
    await redis.aclose()


async def passport_for(world, view, *, shared=False):
    data = await DbPassportSource(world.maker).load(uuid.UUID(view.id))
    assert data is not None
    return build_passport(
        data["intent"],
        data["events"],
        data["attempts"],
        data["condition"],
        data["provider_events"],
        shared=shared,
    )


async def test_a_completed_smartpay_payment_has_a_confirmed_timeline(world):
    view, _ = await world.api.create_payment(OWNER, "key-pp-0001", dict(SMART))
    world.paypal.state.order_status = "APPROVED"
    done = await world.api.buyer_returned(OWNER, uuid.UUID(view.id))
    assert done.status == "CAPTURED"

    passport = await passport_for(world, view)
    titles = [(entry["title"], entry["confirmed"]) for entry in passport["timeline"]]
    assert ("Payment started", False) in titles
    assert ("Payment completed", True) in titles
    assert passport["summary"]["final_outcome"] == "The payment was completed."
    assert passport["condition"] is None
    assert passport["errors"] == []
    assert passport["integrity"]["chain_valid"] is True
    assert passport["protections"]["blind_retries"] == 0


async def test_a_replayed_checkout_is_counted_as_a_blocked_duplicate(world):
    first, _ = await world.api.create_payment(OWNER, "key-pp-0002", dict(SMART))
    await world.api.create_payment(OWNER, "key-pp-0002", dict(SMART))
    protections = (await passport_for(world, first))["protections"]
    assert protections["duplicate_replays"] == 1
    assert protections["blind_retries"] == 0
    assert all("…" in masked for masked in protections["paypal_request_ids_used"])


async def test_a_lost_answer_shows_up_as_an_error_and_a_reconciliation(world, monkeypatch):
    monkeypatch.delenv("GEMMA_BASE_URL", raising=False)
    view, _ = await world.api.create_payment(OWNER, "key-pp-0003", dict(SMART))
    world.paypal.state.order_status = "APPROVED"
    world.paypal.capture_order.side_effect = PayPalOutcomeUnknown("answer was lost")
    await world.api.buyer_returned(OWNER, uuid.UUID(view.id))
    await world.api.rescue_plan(OWNER, uuid.UUID(view.id))  # records the explanation
    world.paypal.state.order_status = "COMPLETED"
    await world.api.buyer_returned(OWNER, uuid.UUID(view.id))

    passport = await passport_for(world, view)
    [error] = passport["errors"]
    assert error["error_class"] == "RETRY"
    assert error["explanation_source"] == "template"
    assert error["plain_explanation"]
    assert passport["protections"]["reconciled"] is True
    assert passport["protections"]["blind_retries"] == 0
    world.paypal.capture_order.assert_awaited_once()


async def test_a_conditional_payment_shows_its_verified_condition(world):
    view, _ = await world.api.create_payment(OWNER, "key-pp-0004", dict(CONDITIONAL))
    world.paypal.state.order_status = "APPROVED"
    await world.api.buyer_returned(OWNER, uuid.UUID(view.id))
    await world.api.verify_condition(Caller(OWNER), uuid.UUID(view.id), True, {"note": "ok"})

    condition = (await passport_for(world, view))["condition"]
    assert condition["required"] == "Parcel arrives"
    assert condition["status"] == "VERIFIED"
    assert condition["verified_at"] is not None


async def test_an_unverified_webhook_row_is_reported_and_never_confirms_anything(world):
    view, _ = await world.api.create_payment(OWNER, "key-pp-0005", dict(SMART))
    async with world.maker() as session:
        session.add(
            ProviderEvent(
                payment_id=uuid.UUID(view.id),
                event_id="WH-FORGED",
                event_type="PAYMENT.CAPTURE.COMPLETED",
                verified=False,
                payload={},
            )
        )
        await session.commit()
    passport = await passport_for(world, view)
    assert [e["error_class"] for e in passport["errors"]] == ["WEBHOOK_UNVERIFIED"]
    assert not any(
        e["status_change"] and e["status_change"]["to"] == "CAPTURED" for e in passport["timeline"]
    )


async def test_the_real_routes_own_share_and_refuse(world):
    view, _ = await world.api.create_payment(OWNER, "key-pp-0006", dict(SMART))
    world.paypal.state.order_status = "APPROVED"
    await world.api.buyer_returned(OWNER, uuid.UUID(view.id))
    order_id = view.paypal_ids.order_id

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(passport_router)
    app.state.session_factory = world.maker
    owner = {"X-Owner-Id": OWNER}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as http:
        mine = await http.get(f"/api/payments/{view.id}/passport", headers=owner)
        theirs = await http.get(f"/api/payments/{view.id}/passport", headers={"X-Owner-Id": OTHER})
        share = await http.post(f"/api/payments/{view.id}/passport/share", headers=owner)
        shared = (
            await http.get(f"/api/passport/{share.json()['token']}")
            if share.status_code == 200
            else None
        )
        forged = await http.get("/api/passport/aaaa.bbbb")

    assert mine.status_code == 200 and mine.json()["view"] == "owner"
    assert theirs.status_code == 404
    assert forged.status_code == 404
    if share.status_code == 503:  # PASSPORT_SIGNING_SECRET not set in this environment
        pytest.skip("PASSPORT_SIGNING_SECRET is not set")
    assert shared is not None and shared.status_code == 200
    text = json.dumps(shared.json())
    assert OWNER not in text and view.id not in text and order_id[:4] + "…" not in text
    assert datetime.fromisoformat(share.json()["expires_at"]) > datetime.now(UTC)