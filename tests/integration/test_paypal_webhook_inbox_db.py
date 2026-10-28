"""PostgreSQL webhook inbox integration tests (mocked PayPal verification).

Run only against a disposable migrated LOCAL database:
    $env:SLOTHPAY_RUN_DB_TESTS = "1"
    python -m pytest -v tests/integration/test_paypal_webhook_inbox_db.py

Rows are committed and intentionally retained for audit-like behavior.
No live PayPal network requests are made.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config.settings import get_settings
from backend.models.event import ProviderEvent
from backend.webhooks import router as webhook_module

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1",
        reason="Opt in to disposable local PostgreSQL integration tests",
    ),
]


@pytest_asyncio.fixture
async def maker():
    url = get_settings().DATABASE_URL
    if "localhost" not in url and "127.0.0.1" not in url:
        pytest.skip("Tests require a LOCAL disposable PostgreSQL database")
    engine = create_async_engine(url, pool_size=8)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def app(maker, monkeypatch):
    application = FastAPI()
    application.include_router(webhook_module.router)

    async def session_override():
        async with maker() as session:
            yield session

    application.dependency_overrides[webhook_module.get_webhook_session] = session_override
    verifier = AsyncMock(return_value=True)
    monkeypatch.setattr(webhook_module, "verify_paypal_webhook", verifier)
    return application, verifier


def event():
    return {
        "id": f"WH-{uuid.uuid4().hex}",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {"id": "CAPTURE-TEST", "status": "COMPLETED"},
    }


async def post(app, payload):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/webhooks/paypal", json=payload)


async def stored(maker, event_id):
    async with maker() as session:
        rows = await session.scalars(
            select(ProviderEvent).where(
                ProviderEvent.provider == "paypal", ProviderEvent.event_id == event_id
            )
        )
        return list(rows)


async def test_verified_delivery_is_committed_once(maker, app):
    application, verifier = app
    payload = event()
    response = await post(application, payload)
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "accepted", "event_id": payload["id"]}
    rows = await stored(maker, payload["id"])
    assert len(rows) == 1
    assert rows[0].verified is True
    assert rows[0].payload == payload
    assert rows[0].payment_id is None  # Reconciliation happens later, never at ingestion.
    verifier.assert_awaited_once()


async def test_repeat_delivery_is_idempotent(maker, app):
    application, verifier = app
    payload = event()
    first = await post(application, payload)
    second = await post(application, payload)
    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"
    assert len(await stored(maker, payload["id"])) == 1
    assert verifier.await_count == 2


async def test_concurrent_duplicate_deliveries_commit_one_row(maker, app):
    application, _ = app
    payload = event()
    responses = await asyncio.gather(*(post(application, payload) for _ in range(4)))
    assert all(response.status_code == 200 for response in responses)
    assert sorted(response.json()["status"] for response in responses) == [
        "accepted",
        "duplicate",
        "duplicate",
        "duplicate",
    ]
    assert len(await stored(maker, payload["id"])) == 1


async def test_bad_signature_does_not_persist(maker, app):
    application, verifier = app
    verifier.return_value = False
    payload = event()
    response = await post(application, payload)
    assert response.status_code == 401
    assert len(await stored(maker, payload["id"])) == 0


async def test_verification_outage_does_not_persist(maker, app):
    application, verifier = app
    verifier.side_effect = webhook_module.WebhookVerificationUnavailable("offline")
    payload = event()
    response = await post(application, payload)
    assert response.status_code == 503
    assert len(await stored(maker, payload["id"])) == 0


async def test_malformed_payload_is_rejected_before_verification(app):
    application, verifier = app
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhooks/paypal",
            content=b"{not-json",
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 400
    verifier.assert_not_awaited()


async def test_database_failure_is_not_acknowledged(app):
    application, _ = app

    class BrokenSession:
        async def execute(self, statement):
            raise RuntimeError("simulated database outage")

        async def rollback(self):
            pass

    async def broken_override():
        yield BrokenSession()

    application.dependency_overrides[webhook_module.get_webhook_session] = broken_override
    response = await post(application, event())
    assert response.status_code >= 500


async def test_no_second_event_row_on_duplicate_after_commit(maker, app):
    application, _ = app
    payload = event()
    await post(application, payload)
    await post(application, payload)
    async with maker() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ProviderEvent)
            .where(
                ProviderEvent.provider == "paypal",
                ProviderEvent.event_id == payload["id"],
            )
        )
    assert count == 1
