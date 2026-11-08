
import asyncio
import os

import httpx
import pytest

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("SLOTHPAY_RUN_DB_TESTS") != "1",
        reason="Set SLOTHPAY_RUN_DB_TESTS=1 for local PostgreSQL tests",
    ),
]

OWNER_HEADER = {"X-Owner-Id": "owner-sim-click"}
BODY = {"path": "SMARTPAY", "amount": "10.00", "currency": "USD"}


def client(sim):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=sim.http_app()), base_url="http://slothpay"
    )


async def test_50_concurrent_identical_posts_make_one_intent_and_one_order(sim):
    headers = {**OWNER_HEADER, "Idempotency-Key": "click-key-0000001"}
    async with client(sim) as http:
        responses = await asyncio.gather(
            *[http.post("/api/payments", json=BODY, headers=headers) for _ in range(50)]
        )
        # The losers of the race may be told to retry (409 intent_busy). Nothing else is allowed.
        for r in responses:
            assert r.status_code in {200, 201, 409}, r.text
            if r.status_code == 409:
                assert "intent_busy" in r.text
                assert r.headers.get("retry-after")
        assert sum(r.status_code == 201 for r in responses) == 1
        ids = {r.json()["id"] for r in responses if r.status_code in {200, 201}}
        assert len(ids) == 1
        # Retrying the same key afterwards simply returns that payment.
        retry = await http.post("/api/payments", json=BODY, headers=headers)
        assert retry.status_code == 200 and retry.json()["id"] in ids
    stats = await sim.stats()
    assert stats["orders"] == 1, stats


async def test_the_same_key_with_a_different_amount_is_a_409(sim):
    headers = {**OWNER_HEADER, "Idempotency-Key": "click-key-0000002"}
    async with client(sim) as http:
        first = await http.post("/api/payments", json=BODY, headers=headers)
        clash = await http.post("/api/payments", json={**BODY, "amount": "11.00"}, headers=headers)
    assert first.status_code == 201
    assert clash.status_code == 409
    assert (await sim.stats())["orders"] == 1


async def test_a_second_click_with_a_new_key_and_the_same_amount_returns_the_open_intent(sim):
    async with client(sim) as http:
        first = await http.post(
            "/api/payments",
            json=BODY,
            headers={**OWNER_HEADER, "Idempotency-Key": "click-key-0000003"},
        )
        second = await http.post(
            "/api/payments",
            json=BODY,
            headers={**OWNER_HEADER, "Idempotency-Key": "click-key-0000004"},
        )
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert (await sim.stats())["orders"] == 1


async def test_a_different_amount_with_a_new_key_is_a_new_payment(sim):
    async with client(sim) as http:
        one = await http.post(
            "/api/payments",
            json=BODY,
            headers={**OWNER_HEADER, "Idempotency-Key": "click-key-0000005"},
        )
        two = await http.post(
            "/api/payments",
            json={**BODY, "amount": "25.00"},
            headers={**OWNER_HEADER, "Idempotency-Key": "click-key-0000006"},
        )
    assert one.json()["id"] != two.json()["id"]
    assert (await sim.stats())["orders"] == 2