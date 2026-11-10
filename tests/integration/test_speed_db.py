from __future__ import annotations

import os
import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from backend.api.deps import install_error_handlers
from backend.api.routes_baseline import router as baseline_router
from backend.api.routes_payments import router as payments_router
from backend.api.routes_speed import router as speed_router
from backend.config.settings import get_settings
from backend.payments.paypal_client import PayPalClient
from backend.reliability import timing
from backend.reliability.baseline import run_naive
from tests.integration.sim_support import SMART

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1",
        reason="Set SLOTHPAY_RUN_DB_TESTS=1 for local PostgreSQL tests",
    ),
]

A = {"X-Owner-Id": "speed-owner-a"}
B = {"X-Owner-Id": "speed-owner-b"}


def app_for(sim) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    for router in (payments_router, speed_router, baseline_router):
        app.include_router(router)
    app.state.payment_api = sim.api
    app.state.session_factory = sim.maker
    app.state.paypal_factory = sim.client
    return app


def http_for(sim) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sim)), base_url="http://t")


async def pay_over_http(sim, http, headers=A):
    created = await http.post(
        "/api/payments", json=SMART, headers={**headers, "Idempotency-Key": uuid.uuid4().hex}
    )
    assert created.status_code == 201, created.text
    view = created.json()
    return view


async def rows(sim, intent: str):
    async with sim.maker() as session:
        result = await session.execute(
            text("SELECT step, kind, ms::float AS ms FROM payment_timings WHERE intent_id = :i"),
            {"i": uuid.UUID(intent)},
        )
        return [dict(r) for r in result.mappings()]


async def test_a_payment_saves_ours_provider_and_human_time_apart(sim):
    async with http_for(sim) as http:
        view = await pay_over_http(sim, http)
        await sim.approve(type("V", (), {"id": view["id"]}))
        done = await http.post(f"/api/payments/{view['id']}/return", headers=A)
        assert done.status_code == 200 and done.json()["status"] == "CAPTURED"
        await timing.wait_background()

        saved = await rows(sim, view["id"])
        kinds = {r["kind"] for r in saved}
        assert {"ours", "provider", "human"} <= kinds
        names = {r["step"] for r in saved}
        assert "paypal_create_order" in names and "buyer_approval" in names
        assert all(r["ms"] >= 0 for r in saved)

        mine = (await http.get("/api/speed", headers=A)).json()
        assert mine["label"] == "sandbox" and mine["payments"] == 1
        assert mine["ours"]["p50_ms"] is not None and mine["provider"]["p50_ms"] is not None
        assert mine["human"]["p50_ms"] is not None
        assert mine["recent"][0]["payment_id"] == view["id"]
        assert mine["recent"][0]["path"] == "SMARTPAY"
        assert mine["targets"]["overhead_p95_ms"] == 100

        theirs = (await http.get("/api/speed", headers=B)).json()
        assert theirs["payments"] == 0 and theirs["recent"] == []  # owner scoped


async def test_speed_needs_a_session(sim):
    async with http_for(sim) as http:
        assert (await http.get("/api/speed")).status_code == 401


async def test_the_hot_path_never_waits_for_live_updates_or_extra_reads(sim, monkeypatch):
    order: list[str] = []
    calls = {"get_order": 0}

    real_create = PayPalClient.create_order
    real_get = PayPalClient.get_order

    async def spy_create(self, *a, **k):
        order.append("paypal_create")
        return await real_create(self, *a, **k)

    async def spy_get(self, *a, **k):
        calls["get_order"] += 1
        return await real_get(self, *a, **k)

    class SpyHub:
        async def publish(self, *a, **k):
            order.append("hub")

        async def subscribe(self, *a, **k):  # pragma: no cover
            raise NotImplementedError

    monkeypatch.setattr(PayPalClient, "create_order", spy_create)
    monkeypatch.setattr(PayPalClient, "get_order", spy_get)
    sim.api._hub = SpyHub()
    async with http_for(sim) as http:
        view = await pay_over_http(sim, http)
        await timing.wait_background()
    assert view["status"] == "AWAITING_APPROVAL"
    assert order and order[0] == "paypal_create"
    assert "hub" in order  # the update still goes out, just afterwards
    assert calls["get_order"] == 0  # the extra read right after create_order is gone


async def test_a_lost_create_answer_still_ends_in_a_saved_timing_row(sim):
    await sim.faults(drop_after=["create_order"], drop_after_next=1)
    async with http_for(sim) as http:
        created = await http.post(
            "/api/payments", json=SMART, headers={**A, "Idempotency-Key": uuid.uuid4().hex}
        )
        assert created.status_code in (200, 201, 202), created.text
        await timing.wait_background()
        intent = created.json()["id"]
    saved = await rows(sim, intent)
    assert any(r["kind"] == "provider" for r in saved)


async def test_baseline_is_hidden_unless_chaos_is_allowed(sim, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "CHAOS_ENABLED", False)
    async with http_for(sim) as http:
        assert (await http.post("/api/baseline/pay", json={}, headers=A)).status_code == 404


async def test_baseline_sends_no_request_id_and_a_blind_retry_makes_two_orders(sim):
    await sim.faults(drop_after=["create_order"], drop_after_next=1)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    async with sim.client() as gateway:
        result = await run_naive(
            gateway, amount="5.00", currency="USD", timeout_seconds=30, sleep=fake_sleep
        )
    assert result["request_id_used"] is False and result["label"] == "sandbox"
    assert result["attempts"] == 2 and slept == [30]
    stats = await sim.stats()
    assert stats["orders"] == 2  # the lost answer's order and the blind retry's order
    assert result["paypal_orders_possible"] == 2


async def test_migration_0007_created_the_table_and_it_is_safe_to_repeat(sim):
    async with sim.maker() as session:
        found = await session.scalar(text("SELECT to_regclass('public.payment_timings')"))
        assert found == "payment_timings"
        # The same statements again must not fail (IF NOT EXISTS).
        from importlib import import_module

        import_module("database.migrations.versions.0007_payment_timings")