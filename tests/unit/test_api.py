from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI

from backend.api import routes_payments
from backend.api.deps import (
    ApiError,
    Caller,
    NotAllowed,
    get_event_hub,
    install_error_handlers,
    sign_owner,
)
from backend.api.payments_service import approval_url, audit_event_out, mask_id
from backend.api.routes_payments import router
from backend.api.schemas import EventOut, PaymentOut, PayPalIdsOut, RescueOut
from backend.config.settings import get_settings
from backend.notifications import hub as hub_module
from backend.notifications.cursor import format_cursor, parse_cursor
from backend.notifications.hub import InMemoryHub, RedisHub, channel_for, subscribe
from backend.notifications.stream import payment_event_stream
from backend.orchestration.conditions import PaymentMissing, PaymentService
from backend.reliability.idempotency import IntentBusy, PayloadMismatch

OWNER_A = {"X-Owner-Id": "owner-a"}
OWNER_B = {"X-Owner-Id": "owner-b"}
KEY = {"Idempotency-Key": "key-12345678"}
BODY = {"path": "SMARTPAY", "amount": "10.00", "currency": "USD"}
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("PASSPORT_SIGNING_SECRET", "test-signing-secret")
    monkeypatch.setenv("FRONTEND_ORIGIN", "http://localhost:3000")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------------- fakes
class FakeApi:
    """In-memory PaymentApi with the same ownership and idempotency rules as the real one."""

    def __init__(self) -> None:
        self.payments: dict[uuid.UUID, tuple[str, PaymentOut]] = {}
        self.events: dict[uuid.UUID, list[EventOut]] = {}
        self.keys: dict[tuple[str, str], tuple[dict, uuid.UUID]] = {}
        self.calls: list[tuple] = []
        self.fail_with: Exception | None = None
        self._tick = 0

    def make_payment(self, owner: str, status: str = "AWAITING_APPROVAL") -> PaymentOut:
        payment = PaymentOut(
            id=str(uuid.uuid4()),
            path="SMARTPAY",
            amount="10.00",
            currency="USD",
            status=status,
            display_status="ACTION_NEEDED",
            allowed_actions=["ASK_BUYER_TO_APPROVE"],
            approval_url="https://www.sandbox.paypal.com/checkoutnow?token=ORDER-1",
            paypal_ids=PayPalIdsOut(order_id="ORDE…R-1X"),
            created_at=T0,
            updated_at=T0,
        )
        pid = uuid.UUID(payment.id)
        self.payments[pid] = (owner, payment)
        self.events[pid] = []
        return payment

    def add_event(self, payment_id: str | uuid.UUID, title: str = "Event") -> EventOut:
        self._tick += 1
        event = EventOut(
            id=format_cursor(T0 + timedelta(microseconds=self._tick), uuid.uuid4()),
            type="STATE_TRANSITION",
            title=title,
            summary=f"{title} happened.",
            from_status="ORDER_CREATED",
            to_status="AWAITING_APPROVAL",
            source="paypal_response",
            confirmed=True,
            created_at=T0 + timedelta(microseconds=self._tick),
        )
        self.events[uuid.UUID(str(payment_id))].append(event)
        return event

    def _owned(self, owner: str, payment_id: uuid.UUID) -> PaymentOut | None:
        entry = self.payments.get(payment_id)
        return entry[1] if entry and entry[0] == owner else None

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    async def create_payment(self, owner_id, key, payload):
        self._maybe_fail()
        self.calls.append(("create", owner_id, key, payload))
        known = self.keys.get((owner_id, key))
        if known is not None:
            if known[0] != payload:
                raise PayloadMismatch("This idempotency key was already used differently")
            return self.payments[known[1]][1], True
        payment = self.make_payment(owner_id)
        self.keys[(owner_id, key)] = (payload, uuid.UUID(payment.id))
        return payment, False

    async def list_payments(self, owner_id, limit, cursor):
        mine = [p for owner, p in self.payments.values() if owner == owner_id]
        return mine[:limit], None

    async def get_payment(self, owner_id, payment_id):
        return self._owned(owner_id, payment_id)

    async def list_events(self, owner_id, payment_id, after, limit):
        if self._owned(owner_id, payment_id) is None:
            return None
        events = self.events[payment_id]
        if after is not None:
            try:
                cutoff = parse_cursor(after)
            except ValueError:
                raise ApiError(400, "bad_cursor", "The cursor is not valid") from None
            events = [e for e in events if parse_cursor(e.id) > cutoff]
        return events[:limit]

    async def latest_event_cursor(self, owner_id, payment_id):
        events = self.events.get(payment_id) or []
        return events[-1].id if events else None

    async def buyer_returned(self, owner_id, payment_id):
        self.calls.append(("return", owner_id, payment_id))
        return self._owned(owner_id, payment_id)

    async def verify_condition(self, caller: Caller, payment_id, verified, evidence):
        self.calls.append(("verify", caller, payment_id, verified, evidence))
        entry = self.payments.get(payment_id)
        if entry is None or (not caller.is_admin and entry[0] != caller.owner_id):
            return None
        return entry[1]

    async def cancel(self, owner_id, payment_id):
        self.calls.append(("cancel", owner_id, payment_id))
        return self._owned(owner_id, payment_id)

    async def rescue_plan(self, owner_id, payment_id):
        if self._owned(owner_id, payment_id) is None:
            return None
        return RescueOut(
            payment_id=str(payment_id),
            display_status="ACTION_NEEDED",
            facts=["Payment status is AWAITING_APPROVAL"],
            allowed_actions=["ASK_BUYER_TO_APPROVE"],
            recommended_action="ASK_BUYER_TO_APPROVE",
            needs_human=False,
        )

    async def rescue_action(self, owner_id, payment_id, action):
        self.calls.append(("rescue", owner_id, payment_id, action))
        if self._owned(owner_id, payment_id) is None:
            return None
        if action != "WAIT":
            raise NotAllowed("not_allowed", "That action is not allowed right now")
        return {"action": action, "result": {"executed": False}}


def make_app(api: FakeApi, hub: InMemoryHub | None = None) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)
    app.state.payment_api = api
    the_hub = hub or InMemoryHub()
    app.dependency_overrides[get_event_hub] = lambda: the_hub
    return app


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def hub() -> InMemoryHub:
    return InMemoryHub()


@pytest.fixture
def app(api, hub) -> FastAPI:
    return make_app(api, hub)


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


class SSEClient:
    """One browser tab on the stream: runs the ASGI app and reads what it sends."""

    def __init__(self, app: FastAPI, path: str, headers: dict[str, str] | None = None) -> None:
        path, _, query = path.partition("?")
        self._disconnect = asyncio.Event()
        self._sent_request = False
        self.text = ""
        self.status: int | None = None
        self.response_headers: dict[str, str] = {}
        self._scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": query.encode(),
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
            "client": ("127.0.0.1", 50000),
            "server": ("test", 80),
        }
        self._app = app
        self._task: asyncio.Task | None = None

    async def _receive(self) -> dict[str, Any]:
        if not self._sent_request:
            self._sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.response_headers = {
                k.decode().lower(): v.decode() for k, v in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            self.text += message.get("body", b"").decode()

    async def __aenter__(self) -> SSEClient:
        self._task = asyncio.create_task(self._app(self._scope, self._receive, self._send))
        await self.until(lambda: self.status is not None)
        return self

    async def until(self, check, timeout: float = 5.0) -> None:
        async with asyncio.timeout(timeout):
            while not check():
                if self._task is not None and self._task.done():
                    self._task.result()  # surface a crash in the app
                    break
                await asyncio.sleep(0.01)

    async def wait_for(self, needle: str, timeout: float = 5.0) -> None:
        try:
            await self.until(lambda: needle in self.text, timeout)
        except TimeoutError:
            raise AssertionError(f"{needle!r} never arrived; got:\n{self.text}") from None

    async def __aexit__(self, *_exc) -> None:
        self._disconnect.set()
        assert self._task is not None
        try:
            await asyncio.wait_for(self._task, 5)  # the stream must end when the tab closes
        except TimeoutError:
            raise AssertionError("the stream did not close after the client disconnected") from None


async def subscribed(hub: InMemoryHub, payment_id: str, count: int = 1) -> None:
    async with asyncio.timeout(5):
        while hub.subscriber_count(payment_id) < count:
            await asyncio.sleep(0.01)


# ------------------------------------------------------------------- create + idempotency
async def test_post_without_idempotency_key_is_400(client, api):
    response = await client.post("/api/payments", json=BODY, headers=OWNER_A)
    assert response.status_code == 400
    assert response.json()["error"] == "missing_idempotency_key"
    assert api.calls == []


async def test_post_with_a_too_short_key_is_400(client):
    response = await client.post(
        "/api/payments", json=BODY, headers={**OWNER_A, "Idempotency-Key": "abc"}
    )
    assert response.status_code == 400 and response.json()["error"] == "invalid_idempotency_key"


async def test_the_key_is_checked_even_when_the_body_is_bad(client):
    response = await client.post("/api/payments", json={"amount": "x"}, headers=OWNER_A)
    assert response.status_code == 400


async def test_first_post_creates_with_201_and_no_replay_header(client):
    response = await client.post("/api/payments", json=BODY, headers={**OWNER_A, **KEY})
    assert response.status_code == 201
    assert "idempotent-replay" not in response.headers
    body = response.json()
    assert body["status"] == "AWAITING_APPROVAL" and body["amount"] == "10.00"
    assert body["approval_url"].startswith("https://www.sandbox.paypal.com/")


async def test_a_replay_returns_the_same_payment_with_the_replay_header(client, api):
    first = await client.post("/api/payments", json=BODY, headers={**OWNER_A, **KEY})
    second = await client.post("/api/payments", json=BODY, headers={**OWNER_A, **KEY})
    assert second.status_code == 200
    assert second.headers["idempotent-replay"] == "true"
    assert second.json()["id"] == first.json()["id"]
    assert len(api.payments) == 1


async def test_a_mismatched_payload_with_the_same_key_is_409(client):
    await client.post("/api/payments", json=BODY, headers={**OWNER_A, **KEY})
    other = {**BODY, "amount": "99.00"}
    response = await client.post("/api/payments", json=other, headers={**OWNER_A, **KEY})
    assert response.status_code == 409
    assert response.json()["error"] == "payload_mismatch"


async def test_a_busy_key_is_409_with_retry_after_1(client, api):
    api.fail_with = IntentBusy("busy")
    response = await client.post("/api/payments", json=BODY, headers={**OWNER_A, **KEY})
    assert response.status_code == 409
    assert response.headers["retry-after"] == "1"
    assert response.json() == {
        "error": "intent_busy",
        "message": "Another request for this payment is in progress",
        "retry_after": 1,
    }


async def test_a_rejected_request_is_422_with_the_error_shape(client, api):
    api.fail_with = ValueError("Currency is not allowed")
    response = await client.post("/api/payments", json=BODY, headers={**OWNER_A, **KEY})
    assert response.status_code == 422
    assert response.json() == {
        "error": "invalid_request",
        "message": "Currency is not allowed",
        "retry_after": None,
    }


@pytest.mark.parametrize(
    "body",
    [
        {**BODY, "amount": "-1"},
        {**BODY, "amount": "1.005"},
        {**BODY, "currency": "DOLLARS"},
        {**BODY, "extra": "field"},
        {**BODY, "condition": {"kind": "k", "description": "d", "deadline_minutes": 5}},
        {**BODY, "path": "CONDITIONALPAY"},
        {**BODY, "path": "CONDITIONALPAY", "condition": {"kind": "k", "description": "d"}},
    ],
)
async def test_invalid_bodies_are_rejected_before_any_service_call(client, api, body):
    response = await client.post("/api/payments", json=body, headers={**OWNER_A, **KEY})
    assert response.status_code == 422
    assert api.calls == []


async def test_a_conditional_payment_passes_its_condition_through(client, api):
    body = {
        "path": "CONDITIONALPAY",
        "amount": "25.50",
        "currency": "usd",
        "condition": {"kind": "delivery", "description": "Parcel arrives", "deadline_minutes": 90},
    }
    response = await client.post("/api/payments", json=body, headers={**OWNER_A, **KEY})
    assert response.status_code == 201
    _, owner, key, payload = api.calls[0]
    assert (owner, key) == ("owner-a", "key-12345678")
    assert payload["currency"] == "USD" and payload["amount"] == "25.50"
    assert payload["condition"]["deadline_minutes"] == 90


# ------------------------------------------------------------------------ who is calling
async def test_no_identity_is_401(client):
    response = await client.get("/api/payments")
    assert response.status_code == 401 and response.json()["error"] == "unauthorized"


async def test_a_malformed_owner_header_is_401(client):
    response = await client.get("/api/payments", headers={"X-Owner-Id": "bad owner!"})
    assert response.status_code == 401


async def test_a_signed_cookie_identifies_the_owner(client, api):
    api.make_payment("cookie-owner")
    cookie = sign_owner("cookie-owner", "test-signing-secret")
    response = await client.get("/api/payments", headers={"Cookie": f"slothpay_owner={cookie}"})
    assert response.status_code == 200 and len(response.json()["items"]) == 1


@pytest.mark.parametrize("cookie", ["cookie-owner.0000", "cookie-owner", "other.deadbeef"])
async def test_a_forged_cookie_is_401(client, cookie):
    response = await client.get("/api/payments", headers={"Cookie": f"slothpay_owner={cookie}"})
    assert response.status_code == 401


async def test_the_admin_key_alone_is_not_an_owner_identity(client):
    response = await client.get("/api/payments", headers={"X-Admin-Api-Key": "test-admin-key"})
    assert response.status_code == 401


# ------------------------------------------------------------------------------ ownership
def other_routes(payment_id: str) -> list[tuple[str, str, dict | None]]:
    base = f"/api/payments/{payment_id}"
    return [
        ("GET", base, None),
        ("GET", f"{base}/events", None),
        ("POST", f"{base}/return", None),
        ("POST", f"{base}/cancel", None),
        ("GET", f"{base}/rescue", None),
        ("POST", f"{base}/rescue", {"action": "WAIT"}),
        ("POST", f"{base}/conditions/verify", {"verified": True}),
    ]


async def test_another_owners_payment_is_404_exactly_like_a_missing_one(client, api):
    mine = api.make_payment("owner-a")
    missing = str(uuid.uuid4())
    for (method, path, body), (_, missing_path, _) in zip(
        other_routes(mine.id), other_routes(missing), strict=True
    ):
        theirs = await client.request(method, path, json=body, headers=OWNER_B)
        nothing = await client.request(method, missing_path, json=body, headers=OWNER_B)
        assert theirs.status_code == 404, path
        assert (
            theirs.json()
            == nothing.json()
            == {
                "error": "not_found",
                "message": "Payment not found",
                "retry_after": None,
            }
        )


async def test_a_bad_payment_id_is_the_same_404(client):
    response = await client.get("/api/payments/not-a-uuid", headers=OWNER_A)
    assert response.status_code == 404 and response.json()["error"] == "not_found"


async def test_the_owner_can_read_their_payment(client, api):
    mine = api.make_payment("owner-a")
    response = await client.get(f"/api/payments/{mine.id}", headers=OWNER_A)
    assert response.status_code == 200 and response.json()["id"] == mine.id


async def test_the_list_only_shows_the_owners_payments(client, api):
    api.make_payment("owner-a")
    api.make_payment("owner-b")
    response = await client.get("/api/payments?limit=10", headers=OWNER_A)
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1 and response.json()["next_cursor"] is None


async def test_the_list_limit_is_bounded(client):
    assert (await client.get("/api/payments?limit=0", headers=OWNER_A)).status_code == 422
    assert (await client.get("/api/payments?limit=101", headers=OWNER_A)).status_code == 422


# ----------------------------------------------------------------------- condition + rescue
async def test_verify_needs_a_real_json_true_or_false(client, api):
    mine = api.make_payment("owner-a")
    path = f"/api/payments/{mine.id}/conditions/verify"
    for bad in ("yes", 1, "true", None):
        response = await client.post(path, json={"verified": bad}, headers=OWNER_A)
        assert response.status_code == 422, bad
    assert not [c for c in api.calls if c[0] == "verify"]


async def test_the_owner_can_verify_with_evidence(client, api):
    mine = api.make_payment("owner-a")
    path = f"/api/payments/{mine.id}/conditions/verify"
    response = await client.post(
        path, json={"verified": True, "evidence": {"tracking": "T1"}}, headers=OWNER_A
    )
    assert response.status_code == 200
    _, caller, _, verified, evidence = api.calls[-1]
    assert (
        caller == Caller("owner-a", False) and verified is True and evidence == {"tracking": "T1"}
    )


async def test_an_admin_can_verify_without_owning_the_payment(client, api):
    mine = api.make_payment("owner-a")
    path = f"/api/payments/{mine.id}/conditions/verify"
    response = await client.post(
        path, json={"verified": False}, headers={"X-Admin-Api-Key": "test-admin-key"}
    )
    assert response.status_code == 200
    assert api.calls[-1][1] == Caller(None, True)


async def test_a_wrong_admin_key_is_not_enough(client, api):
    mine = api.make_payment("owner-a")
    path = f"/api/payments/{mine.id}/conditions/verify"
    response = await client.post(
        path, json={"verified": True}, headers={"X-Admin-Api-Key": "guess"}
    )
    assert response.status_code == 401


async def test_a_wrong_admin_key_cannot_override_another_owner(client, api):
    mine = api.make_payment("owner-a")
    path = f"/api/payments/{mine.id}/conditions/verify"
    response = await client.post(
        path, json={"verified": True}, headers={**OWNER_B, "X-Admin-Api-Key": "guess"}
    )
    assert response.status_code == 404


async def test_rescue_plan_and_a_permitted_action(client, api):
    mine = api.make_payment("owner-a")
    plan = await client.get(f"/api/payments/{mine.id}/rescue", headers=OWNER_A)
    assert plan.status_code == 200 and plan.json()["recommended_action"] == "ASK_BUYER_TO_APPROVE"
    done = await client.post(
        f"/api/payments/{mine.id}/rescue", json={"action": "WAIT"}, headers=OWNER_A
    )
    assert done.status_code == 200 and done.json() == {
        "action": "WAIT",
        "result": {"executed": False},
    }


async def test_a_refused_rescue_action_is_409_and_an_unknown_one_is_422(client, api):
    mine = api.make_payment("owner-a")
    path = f"/api/payments/{mine.id}/rescue"
    refused = await client.post(path, json={"action": "VOID"}, headers=OWNER_A)
    assert refused.status_code == 409 and refused.json()["error"] == "not_allowed"
    unknown = await client.post(path, json={"action": "REFUND_EVERYTHING"}, headers=OWNER_A)
    assert unknown.status_code == 422


async def test_paypal_failures_become_a_503_with_fixed_text(client, api):
    from backend.payments.paypal_client import PayPalOutcomeUnknown

    api.fail_with = PayPalOutcomeUnknown("secret-token-abc leaked in an upstream message")
    response = await client.post("/api/payments", json=BODY, headers={**OWNER_A, **KEY})
    assert response.status_code == 503
    assert "secret-token" not in response.text
    assert response.json()["error"] == "paypal_unavailable"


# ------------------------------------------------------------------------------ events
async def test_events_after_a_cursor_only_returns_later_events(client, api):
    mine = api.make_payment("owner-a")
    first, second, third = (api.add_event(mine.id, name) for name in ("One", "Two", "Three"))
    everything = await client.get(f"/api/payments/{mine.id}/events", headers=OWNER_A)
    assert [e["title"] for e in everything.json()] == ["One", "Two", "Three"]
    later = await client.get(
        f"/api/payments/{mine.id}/events", params={"after_id": first.id}, headers=OWNER_A
    )
    assert [e["id"] for e in later.json()] == [second.id, third.id]


async def test_a_bad_cursor_is_400(client, api):
    mine = api.make_payment("owner-a")
    response = await client.get(
        f"/api/payments/{mine.id}/events", params={"after_id": "nonsense"}, headers=OWNER_A
    )
    assert response.status_code == 400 and response.json()["error"] == "bad_cursor"


# --------------------------------------------------------------------- live stream (SSE)
async def test_the_stream_replays_missed_events_after_last_event_id(app, api):
    mine = api.make_payment("owner-a")
    seen, missed_one, missed_two = (
        api.add_event(mine.id, n) for n in ("Seen", "Missed1", "Missed2")
    )
    headers = {**OWNER_A, "Last-Event-ID": seen.id}
    async with SSEClient(app, f"/api/payments/{mine.id}/stream", headers) as tab:
        await tab.wait_for(missed_two.id)
        assert tab.status == 200
        assert tab.response_headers["content-type"].startswith("text/event-stream")
        assert f"id: {missed_one.id}" in tab.text and f"id: {missed_two.id}" in tab.text
        assert seen.id not in tab.text, "an event the client already has is not sent again"
        assert tab.text.index(missed_one.id) < tab.text.index(missed_two.id)
        assert "event: snapshot" in tab.text, "a snapshot follows so the tab can redraw"


async def test_the_first_connection_can_ask_for_a_replay_with_after_id(app, api):
    mine = api.make_payment("owner-a")
    old, new = api.add_event(mine.id, "Old"), api.add_event(mine.id, "New")
    path = f"/api/payments/{mine.id}/stream?after_id={old.id}"
    async with SSEClient(app, path, OWNER_A) as tab:
        await tab.wait_for(new.id)
        assert old.id not in tab.text


async def test_last_event_id_wins_over_after_id(app, api):
    mine = api.make_payment("owner-a")
    one, two, three = (api.add_event(mine.id, n) for n in ("One", "Two", "Three"))
    path = f"/api/payments/{mine.id}/stream?after_id={one.id}"
    async with SSEClient(app, path, {**OWNER_A, "Last-Event-ID": two.id}) as tab:
        await tab.wait_for(three.id)
        assert one.id not in tab.text and two.id not in tab.text


async def test_without_a_cursor_only_new_events_are_sent(app, api, hub):
    mine = api.make_payment("owner-a")
    old = api.add_event(mine.id, "Old")
    async with SSEClient(app, f"/api/payments/{mine.id}/stream", OWNER_A) as tab:
        await subscribed(hub, mine.id)
        new = api.add_event(mine.id, "New")
        await hub.publish(mine.id, {"type": "payment.updated"})
        await tab.wait_for(new.id)
        assert old.id not in tab.text


async def test_two_tabs_on_the_same_payment_both_update_live(app, api, hub):
    mine = api.make_payment("owner-a")
    async with (
        SSEClient(app, f"/api/payments/{mine.id}/stream", OWNER_A) as tab_one,
        SSEClient(app, f"/api/payments/{mine.id}/stream", OWNER_A) as tab_two,
    ):
        await subscribed(hub, mine.id, count=2)
        event = api.add_event(mine.id, "Captured")
        await hub.publish(mine.id, {"type": "payment.updated"})
        await asyncio.gather(tab_one.wait_for(event.id), tab_two.wait_for(event.id))
        assert "event: snapshot" in tab_one.text and "event: snapshot" in tab_two.text


async def test_an_event_is_sent_once_even_if_it_is_announced_twice(app, api, hub):
    mine = api.make_payment("owner-a")
    async with SSEClient(app, f"/api/payments/{mine.id}/stream", OWNER_A) as tab:
        await subscribed(hub, mine.id)
        event = api.add_event(mine.id, "Once")
        await hub.publish(mine.id, {})
        await hub.publish(mine.id, {})
        await tab.wait_for(event.id)
        await asyncio.sleep(0.1)
        assert tab.text.count(f"id: {event.id}") == 1


async def test_the_stream_closes_cleanly_and_releases_its_subscription(app, api, hub):
    mine = api.make_payment("owner-a")
    async with SSEClient(app, f"/api/payments/{mine.id}/stream", OWNER_A):
        await subscribed(hub, mine.id)
    assert hub.subscriber_count(mine.id) == 0


async def test_the_stream_sends_a_heartbeat_comment(app, api, monkeypatch):
    monkeypatch.setattr(routes_payments, "HEARTBEAT_SECONDS", 0.05)
    mine = api.make_payment("owner-a")
    async with SSEClient(app, f"/api/payments/{mine.id}/stream", OWNER_A) as tab:
        await tab.wait_for(": ping")


def test_the_heartbeat_is_every_15_seconds():
    assert routes_payments.HEARTBEAT_SECONDS == 15


async def test_stream_of_another_owner_is_404_and_not_an_event_stream(client, api):
    mine = api.make_payment("owner-a")
    response = await client.get(f"/api/payments/{mine.id}/stream", headers=OWNER_B)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


async def test_stream_with_a_bad_last_event_id_is_400(client, api):
    mine = api.make_payment("owner-a")
    response = await client.get(
        f"/api/payments/{mine.id}/stream", headers={**OWNER_A, "Last-Event-ID": "garbage"}
    )
    assert response.status_code == 400 and response.json()["error"] == "bad_cursor"


async def test_the_stream_still_delivers_when_a_wake_up_message_is_lost():
    api = FakeApi()
    mine = api.make_payment("owner-a")
    pid = uuid.UUID(mine.id)

    async def fetch_events(after, limit):
        return await api.list_events("owner-a", pid, after, limit) or []

    async def fetch_payment():
        return await api.get_payment("owner-a", pid)

    stream = payment_event_stream(
        hub=InMemoryHub(),
        intent_id=mine.id,
        fetch_events=fetch_events,
        fetch_payment=fetch_payment,
        cursor=None,
        poll_seconds=0.05,
    )
    event = api.add_event(mine.id, "Quiet")  # no publish: only the poll can find it
    first = await asyncio.wait_for(anext(stream), 2)
    assert first["id"] == event.id and first["event"] == "payment"
    await stream.aclose()


async def test_the_stream_keeps_serving_when_the_hub_breaks():
    api = FakeApi()
    mine = api.make_payment("owner-a")
    pid = uuid.UUID(mine.id)

    class BrokenSub:
        async def next(self, timeout):
            raise ConnectionError("redis went away")

    class BrokenHub:
        def subscription(self, intent_id):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def manager():
                yield BrokenSub()

            return manager()

    async def fetch_events(after, limit):
        return await api.list_events("owner-a", pid, after, limit) or []

    stream = payment_event_stream(
        hub=BrokenHub(),
        intent_id=mine.id,
        fetch_events=fetch_events,
        fetch_payment=lambda: api.get_payment("owner-a", pid),
        cursor=None,
        poll_seconds=0.05,
    )
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.15)
    event = api.add_event(mine.id, "Late")
    message = await asyncio.wait_for(task, 2)
    assert message["id"] == event.id
    await stream.aclose()


# ---------------------------------------------------------------------------------- hub
async def test_in_memory_hub_delivers_to_every_subscriber_of_that_payment_only():
    hub = InMemoryHub()
    one, other = str(uuid.uuid4()), str(uuid.uuid4())
    async with hub.subscription(one) as a, hub.subscription(one) as b, hub.subscription(other) as c:
        await hub.publish(one, {"n": 1})
        assert await a.next(1) == {"n": 1} and await b.next(1) == {"n": 1}
        assert await c.next(0.05) is None
    assert hub.subscriber_count(one) == 0


async def test_redis_hub_uses_the_payments_channel_and_delivers():
    redis = FakeAsyncRedis(decode_responses=True)
    hub = RedisHub(redis)
    intent_id = str(uuid.uuid4())
    assert channel_for(intent_id) == f"payments:{intent_id}"
    async with hub.subscription(intent_id) as sub:
        assert await hub.publish(intent_id, {"type": "payment.updated", "status": "CAPTURED"})
        assert await sub.next(2) == {"type": "payment.updated", "status": "CAPTURED"}
        assert await sub.next(0.05) is None
    await redis.aclose()


async def test_redis_hub_subscribe_iterates_events():
    redis = FakeAsyncRedis(decode_responses=True)
    hub = RedisHub(redis)
    intent_id = str(uuid.uuid4())
    received: list[dict] = []

    async def reader():
        async for event in subscribe(hub, intent_id, idle=0.05):
            received.append(event)
            if len(received) == 2:
                return

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.1)
    await hub.publish(intent_id, {"n": 1})
    await hub.publish(intent_id, {"n": 2})
    await asyncio.wait_for(task, 2)
    assert received == [{"n": 1}, {"n": 2}]
    await redis.aclose()


async def test_publishing_never_raises_when_redis_is_down():
    class Down:
        async def publish(self, channel, message):
            raise ConnectionError("down")

    hub = RedisHub(Down())
    assert await hub.publish(str(uuid.uuid4()), {"a": 1}) is False


async def test_an_oversized_or_unserializable_event_still_wakes_subscribers():
    redis = FakeAsyncRedis(decode_responses=True)
    hub = RedisHub(redis)
    intent_id = str(uuid.uuid4())
    async with hub.subscription(intent_id) as sub:
        await hub.publish(intent_id, {"blob": "x" * 10_000})
        assert await sub.next(2) == {}
    await redis.aclose()


def test_the_channel_name_only_accepts_a_real_payment_id():
    with pytest.raises(ValueError):
        channel_for("payments:*")


async def test_set_hub_replaces_the_process_wide_hub():
    mine = InMemoryHub()
    hub_module.set_hub(mine)
    try:
        assert hub_module.get_hub() is mine
    finally:
        hub_module.set_hub(None)


async def test_webhook_deps_publish_into_the_hub():
    from backend.webhooks.receiver import get_webhook_deps

    mine = InMemoryHub()
    hub_module.set_hub(mine)
    generator = get_webhook_deps()
    try:
        deps = await generator.__anext__()
        intent_id = str(uuid.uuid4())
        async with mine.subscription(intent_id) as sub:
            await deps.publish(intent_id, {"event_type": "PAYMENT.CAPTURE.COMPLETED"})
            assert await sub.next(1) == {"event_type": "PAYMENT.CAPTURE.COMPLETED"}
    finally:
        await generator.aclose()
        hub_module.set_hub(None)


async def test_the_condition_service_notifies_and_survives_a_broken_notifier():
    seen: list[tuple[str, dict]] = []

    async def notifier(intent_id, event):
        seen.append((intent_id, event))
        raise RuntimeError("notifier is broken")

    service = PaymentService(lambda: None, None, on_event=notifier)
    with pytest.raises(PaymentMissing):  # the real outcome is kept, not the notifier's error
        await service.resolve_condition("not-a-uuid", True)
    assert seen == [("not-a-uuid", {"type": "condition.resolved"})]


# -------------------------------------------------------------------------------- cursor
def test_cursors_round_trip_and_sort_like_the_events_they_name():
    ids = [uuid.UUID(int=n) for n in (5, 1, 9)]
    stamps = [T0, T0 + timedelta(microseconds=1), T0 + timedelta(seconds=3)]
    cursors = [format_cursor(s, i) for s, i in zip(stamps, ids, strict=True)]
    for cursor, stamp, row_id in zip(cursors, stamps, ids, strict=True):
        assert parse_cursor(cursor) == (stamp, row_id)
    assert [parse_cursor(c)[0] for c in cursors] == sorted(stamps)


def test_a_naive_timestamp_is_read_as_utc():
    naive = datetime(2026, 11, 1, 12, 0)
    assert parse_cursor(format_cursor(naive, uuid.uuid4()))[0] == T0


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "x",
        "123",
        "123.abc",
        "-5." + "0" * 32,
        "1e3." + "0" * 32,
        "9" * 40 + "." + "0" * 32,
        "1." + "g" * 32,
        "١٢٣." + "0" * 32,
        "1." + "0" * 31,
        "1" * 200,
    ],
)
def test_garbage_is_not_a_cursor(bad):
    with pytest.raises(ValueError):
        parse_cursor(bad)


# ---------------------------------------------------------------------- shaping helpers
def test_paypal_ids_are_masked():
    assert mask_id("5O190127TN364715T") == "5O19…715T"
    assert mask_id("ABCD") == "AB…"
    assert mask_id(None) is None and mask_id("") is None


def test_the_approval_link_exists_only_while_approval_is_pending():
    sandbox = "https://api-m.sandbox.paypal.com"
    link = approval_url("ORDER 1", "AWAITING_APPROVAL", sandbox)
    assert link == "https://www.sandbox.paypal.com/checkoutnow?token=ORDER%201"
    assert approval_url("ORDER-1", "ORDER_CREATED", sandbox)
    assert approval_url("ORDER-1", "CAPTURED", sandbox) is None
    assert approval_url("ORDER-1", "APPROVED", sandbox) is None
    assert approval_url(None, "AWAITING_APPROVAL", sandbox) is None


def audit_row(**overrides):
    values = {
        "id": uuid.uuid4(),
        "event_type": "STATE_TRANSITION",
        "from_state": "ORDER_CREATED",
        "to_state": "AWAITING_APPROVAL",
        "actor": "orchestration",
        "evidence": {"source": "paypal_response", "confirmed": True},
        "created_at": T0,
    }
    return SimpleNamespace(**{**values, **overrides})


def test_an_audit_row_becomes_a_plain_language_event():
    event = audit_event_out(audit_row())
    assert event.title == "Waiting for the buyer to approve"
    assert event.summary == "Status changed from ORDER_CREATED to AWAITING_APPROVAL."
    assert event.source == "paypal_response" and event.confirmed is True
    assert parse_cursor(event.id) == (T0, parse_cursor(event.id)[1])


def test_raw_evidence_never_reaches_the_event():
    secret = {"source": "api", "confirmed": False, "email": "a@b.c", "token": "A21AAsecret"}
    event = audit_event_out(audit_row(event_type="CUSTOM_THING", from_state=None, evidence=secret))
    dumped = event.model_dump_json()
    assert "a@b.c" not in dumped and "A21AA" not in dumped
    assert event.title == "Custom thing" and event.confirmed is False


def test_an_unsafe_reason_is_not_used_as_the_summary():
    row = audit_row(
        event_type="WEBHOOK_IGNORED",
        from_state=None,
        to_state=None,
        evidence={"reason": "<script>alert(1)</script> payer@x.com"},
    )
    assert audit_event_out(row).summary == "Webhook ignored"
    ok = audit_row(
        event_type="WEBHOOK_IGNORED",
        from_state=None,
        to_state=None,
        evidence={"reason": "Event arrived after the payment closed"},
    )
    assert audit_event_out(ok).summary == "Event arrived after the payment closed"


# ------------------------------------------------------------------------ app wiring
async def test_cors_allows_only_the_frontend_origin():
    from backend.main import create_app

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        preflight = {
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "idempotency-key,content-type,x-owner-id",
        }
        good = await http.options(
            "/api/payments", headers={"Origin": "http://localhost:3000", **preflight}
        )
        assert good.status_code == 200
        assert good.headers["access-control-allow-origin"] == "http://localhost:3000"
        bad = await http.options(
            "/api/payments", headers={"Origin": "https://evil.example", **preflight}
        )
        assert "access-control-allow-origin" not in bad.headers
        health = await http.get("/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in health.headers


async def test_the_real_app_mounts_the_routers_and_fails_closed_without_a_service():
    from backend.main import create_app

    app = create_app()
    paths = set(app.openapi()["paths"])
    assert "/api/payments" in paths and "/webhooks/paypal" in paths
    assert "/api/payments/{payment_id}/stream" in paths
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        response = await http.get("/api/payments", headers=OWNER_A)
    assert response.status_code == 503 and response.json()["error"] == "payments_unavailable"