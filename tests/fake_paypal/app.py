from __future__ import annotations

import asyncio
import json
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

MONEY_OPERATIONS = {
    "create_order",
    "authorize_order",
    "capture_order",
    "capture_authorization",
    "void_authorization",
}


class ConnectionDropped(httpx.ReadError):
    """Raised inside the app when a response is "lost" after PayPal processed the request."""


def _operation(method: str, path: str) -> str | None:
    if method != "POST":
        return None
    if path == "/v2/checkout/orders":
        return "create_order"
    if path.endswith("/authorize") and "/checkout/orders/" in path:
        return "authorize_order"
    if path.endswith("/capture") and "/checkout/orders/" in path:
        return "capture_order"
    if path.endswith("/capture") and "/payments/authorizations/" in path:
        return "capture_authorization"
    if path.endswith("/void") and "/payments/authorizations/" in path:
        return "void_authorization"
    return None


def _money(value: str, currency: str) -> dict[str, str]:
    return {"value": value, "currency_code": currency}


class Simulator:
    """All state of one fake PayPal. Held on app.state.sim, never shared between apps."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        self.authorizations: dict[str, dict[str, Any]] = {}
        self.captures: dict[str, dict[str, Any]] = {}
        self.replay_cache: dict[tuple[str, str, str], tuple[int, dict[str, Any] | None]] = {}
        self.capture_request_ids: list[str] = []
        self.requests_total = 0
        self.replays = 0
        self.dropped = 0
        self.errors_returned = 0
        self.declines = 0
        self.voids = 0
        self.faults: dict[str, Any] = {}
        self.rng = random.Random(0)
        self.drop_rng = random.Random(1)
        self.capture_requests_by_order: dict[str, set[str]] = {}
        self.drop_after_remaining: int | None = None
        self.decline_remaining = 0

    def set_faults(self, body: dict[str, Any]) -> None:
        self.faults = {
            "delay_ms": int(body.get("delay_ms", 0)),
            "drop_after": list(body.get("drop_after", [])),
            "error_rate": float(body.get("error_rate", 0.0)),
            "error_when": body.get("error_when", "before"),
            "error_operations": list(body.get("error_operations", sorted(MONEY_OPERATIONS))),
            "drop_rate": float(body.get("drop_rate", 1.0)),
        }
        self.rng = random.Random(int(body.get("seed", 0)))
        self.drop_rng = random.Random(int(body.get("seed", 0)) + 1)
        nxt = body.get("drop_after_next")
        self.drop_after_remaining = int(nxt) if nxt is not None else None
        self.decline_remaining = int(body.get("decline_next_capture", 0))

    def stats(self) -> dict[str, Any]:
        statuses: dict[str, int] = {}
        for order in self.orders.values():
            statuses[order["status"]] = statuses.get(order["status"], 0) + 1
        real_captures = [c for c in self.captures.values() if c["status"] == "COMPLETED"]
        return {
            "provider": "FAKE PAYPAL (SIMULATOR ONLY)",
            "orders": len(self.orders),
            "orders_by_status": statuses,
            "authorizations": len(self.authorizations),
            "captures": len(real_captures),
            "capture_request_ids": len(set(self.capture_request_ids)),
            "orders_with_more_than_one_capture_request_id": sum(
                1 for ids in self.capture_requests_by_order.values() if len(ids) > 1
            ),
            "voids": self.voids,
            "replays": self.replays,
            "requests_total": self.requests_total,
            "dropped_responses": self.dropped,
            "errors_returned": self.errors_returned,
            "declines": self.declines,
        }

    # ---- the pieces of an order document --------------------------------------------------
    def order_document(self, order: dict[str, Any]) -> dict[str, Any]:
        unit: dict[str, Any] = {"amount": _money(order["amount"], order["currency"])}
        payments: dict[str, Any] = {}
        capture = self.captures.get(order.get("capture_id") or "")
        if capture is not None:
            payments["captures"] = [self.capture_document(capture)]
        auth = self.authorizations.get(order.get("authorization_id") or "")
        if auth is not None:
            payments["authorizations"] = [self.authorization_document(auth)]
        if payments:
            unit["payments"] = payments
        return {
            "id": order["id"],
            "intent": order["intent"],
            "status": order["status"],
            "purchase_units": [unit],
            "links": [
                {
                    "rel": "approve",
                    "href": f"https://www.sandbox.paypal.com/checkoutnow?token={order['id']}",
                    "method": "GET",
                }
            ],
        }

    @staticmethod
    def authorization_document(auth: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": auth["id"],
            "status": auth["status"],
            "amount": _money(auth["amount"], auth["currency"]),
            "expiration_time": auth["expiration_time"],
        }

    @staticmethod
    def capture_document(capture: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": capture["id"],
            "status": capture["status"],
            "amount": _money(capture["amount"], capture["currency"]),
        }


def _json(status: int, body: dict[str, Any] | None) -> Response:
    if body is None:
        return Response(status_code=status)
    return JSONResponse(
        body, status_code=status, headers={"PayPal-Debug-Id": uuid.uuid4().hex[:13]}
    )


def _error(status: int, name: str, issue: str, message: str) -> tuple[int, dict[str, Any]]:
    return status, {"name": name, "message": message, "details": [{"issue": issue}]}


class FaultMiddleware(BaseHTTPMiddleware):
    """Applies delay, random 5xx and lost answers around the money endpoints."""

    async def dispatch(self, request: Request, call_next):
        sim: Simulator = request.app.state.sim
        sim.requests_total += 1
        operation = _operation(request.method, request.url.path)
        faults = sim.faults
        if operation is None or not faults:
            return await call_next(request)
        if faults.get("delay_ms"):
            await asyncio.sleep(faults["delay_ms"] / 1000)
        roll = sim.rng.random() if faults.get("error_rate") else 1.0
        fail = roll < faults.get("error_rate", 0.0) and operation in faults["error_operations"]
        if fail and faults["error_when"] == "before":
            sim.errors_returned += 1
            return _json(503, {"name": "SERVICE_UNAVAILABLE", "message": "simulated 503"})
        response = await call_next(request)
        if fail:  # processed, but the answer is a 503
            sim.errors_returned += 1
            return _json(
                503, {"name": "SERVICE_UNAVAILABLE", "message": "simulated 503 after work"}
            )
        drop = operation in faults["drop_after"] and (
            faults["drop_rate"] >= 1.0 or sim.drop_rng.random() < faults["drop_rate"]
        )
        if drop and sim.drop_after_remaining is not None:
            if sim.drop_after_remaining <= 0:
                drop = False
            else:
                sim.drop_after_remaining -= 1
        if drop and response.status_code < 300:
            sim.dropped += 1
            raise ConnectionDropped("SIMULATOR: the answer was lost after PayPal processed it")
        return response


def create_app() -> FastAPI:
    app = FastAPI(title="Fake PayPal (simulator only)")
    app.state.sim = Simulator()
    app.add_middleware(FaultMiddleware)

    def sim_of(request: Request) -> Simulator:
        return request.app.state.sim

    def replayed(request: Request, body_key: str) -> Response | None:
        sim = sim_of(request)
        request_id = request.headers.get("PayPal-Request-Id")
        if not request_id:
            return None
        cached = sim.replay_cache.get((request_id, request.method, request.url.path))
        if cached is None:
            return None
        sim.replays += 1
        return _json(*cached)

    def remember(request: Request, status: int, body: dict[str, Any] | None) -> Response:
        request_id = request.headers.get("PayPal-Request-Id")
        if request_id:
            sim_of(request).replay_cache[(request_id, request.method, request.url.path)] = (
                status,
                body,
            )
        return _json(status, body)

    # ---------------------------------------------------------------- oauth
    @app.post("/v1/oauth2/token")
    async def token() -> dict[str, Any]:
        return {
            "access_token": "SIM-" + uuid.uuid4().hex,
            "token_type": "Bearer",
            "expires_in": 32400,
        }

    # ---------------------------------------------------------------- orders
    @app.post("/v2/checkout/orders")
    async def create_order(request: Request) -> Response:
        if (again := replayed(request, "order")) is not None:
            return again
        sim = sim_of(request)
        body = await request.json()
        unit = body["purchase_units"][0]["amount"]
        order_id = "SIMORDER-" + uuid.uuid4().hex[:12].upper()
        sim.orders[order_id] = {
            "id": order_id,
            "intent": body["intent"],
            "status": "CREATED",
            "amount": unit["value"],
            "currency": unit["currency_code"],
            "capture_id": None,
            "authorization_id": None,
        }
        document = sim.order_document(sim.orders[order_id])
        document["links"] = [
            {
                "rel": "payer-action",
                "href": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}",
                "method": "GET",
            }
        ]
        return remember(request, 201, document)

    @app.get("/v2/checkout/orders/{order_id}")
    async def get_order(order_id: str, request: Request) -> Response:
        sim = sim_of(request)
        order = sim.orders.get(order_id)
        if order is None:
            return _json(
                *_error(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID", "Order not found")
            )
        return _json(200, sim.order_document(order))

    @app.post("/v2/checkout/orders/{order_id}/capture")
    async def capture_order(order_id: str, request: Request) -> Response:
        if (again := replayed(request, "capture")) is not None:
            return again
        sim = sim_of(request)
        request_id = request.headers.get("PayPal-Request-Id", "")
        sim.capture_request_ids.append(request_id)
        sim.capture_requests_by_order.setdefault(order_id, set()).add(request_id)
        order = sim.orders.get(order_id)
        if order is None:
            return _json(
                *_error(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID", "Order not found")
            )
        if sim.decline_remaining > 0:
            sim.decline_remaining -= 1
            sim.declines += 1
            return remember(
                request,
                *_error(
                    422,
                    "UNPROCESSABLE_ENTITY",
                    "INSTRUMENT_DECLINED",
                    "The instrument was declined",
                ),
            )
        if order["intent"] != "CAPTURE" or order["status"] != "APPROVED":
            if order["status"] == "COMPLETED":
                return remember(
                    request,
                    *_error(
                        422, "UNPROCESSABLE_ENTITY", "ORDER_ALREADY_CAPTURED", "Already captured"
                    ),
                )
            return remember(
                request,
                *_error(422, "UNPROCESSABLE_ENTITY", "ORDER_NOT_APPROVED", "Order is not approved"),
            )
        capture_id = "SIMCAP-" + uuid.uuid4().hex[:12].upper()
        sim.captures[capture_id] = {
            "id": capture_id,
            "status": "COMPLETED",
            "amount": order["amount"],
            "currency": order["currency"],
        }
        order["capture_id"] = capture_id
        order["status"] = "COMPLETED"
        return remember(request, 201, sim.order_document(order))

    @app.post("/v2/checkout/orders/{order_id}/authorize")
    async def authorize_order(order_id: str, request: Request) -> Response:
        if (again := replayed(request, "authorize")) is not None:
            return again
        sim = sim_of(request)
        order = sim.orders.get(order_id)
        if order is None:
            return _json(
                *_error(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID", "Order not found")
            )
        if order["intent"] != "AUTHORIZE" or order["status"] != "APPROVED":
            return remember(
                request,
                *_error(422, "UNPROCESSABLE_ENTITY", "ORDER_NOT_APPROVED", "Order is not approved"),
            )
        auth_id = "SIMAUTH-" + uuid.uuid4().hex[:12].upper()
        sim.authorizations[auth_id] = {
            "id": auth_id,
            "status": "CREATED",
            "amount": order["amount"],
            "currency": order["currency"],
            "expiration_time": (datetime.now(UTC) + timedelta(days=29)).isoformat(),
            "capture_id": None,
        }
        order["authorization_id"] = auth_id
        order["status"] = "COMPLETED"
        return remember(request, 201, sim.order_document(order))

    # ------------------------------------------------------- authorizations
    @app.get("/v2/payments/authorizations/{auth_id}")
    async def get_authorization(auth_id: str, request: Request) -> Response:
        sim = sim_of(request)
        auth = sim.authorizations.get(auth_id)
        if auth is None:
            return _json(*_error(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID", "Not found"))
        return _json(200, sim.authorization_document(auth))

    @app.post("/v2/payments/authorizations/{auth_id}/capture")
    async def capture_authorization(auth_id: str, request: Request) -> Response:
        if (again := replayed(request, "capture")) is not None:
            return again
        sim = sim_of(request)
        request_id = request.headers.get("PayPal-Request-Id", "")
        sim.capture_request_ids.append(request_id)
        sim.capture_requests_by_order.setdefault("auth:" + auth_id, set()).add(request_id)
        auth = sim.authorizations.get(auth_id)
        if auth is None:
            return _json(*_error(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID", "Not found"))
        if auth["status"] != "CREATED":
            return remember(
                request,
                *_error(
                    422,
                    "UNPROCESSABLE_ENTITY",
                    "AUTHORIZATION_VOIDED"
                    if auth["status"] == "VOIDED"
                    else "AUTHORIZATION_ALREADY_CAPTURED",
                    "The authorization cannot be captured",
                ),
            )
        capture_id = "SIMCAP-" + uuid.uuid4().hex[:12].upper()
        sim.captures[capture_id] = {
            "id": capture_id,
            "status": "COMPLETED",
            "amount": auth["amount"],
            "currency": auth["currency"],
        }
        auth["status"] = "CAPTURED"
        auth["capture_id"] = capture_id
        return remember(request, 201, sim.capture_document(sim.captures[capture_id]))

    @app.post("/v2/payments/authorizations/{auth_id}/void")
    async def void_authorization(auth_id: str, request: Request) -> Response:
        if (again := replayed(request, "void")) is not None:
            return again
        sim = sim_of(request)
        auth = sim.authorizations.get(auth_id)
        if auth is None:
            return _json(*_error(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID", "Not found"))
        if auth["status"] == "CAPTURED":
            return remember(
                request,
                *_error(
                    422,
                    "UNPROCESSABLE_ENTITY",
                    "AUTHORIZATION_ALREADY_CAPTURED",
                    "Already captured",
                ),
            )
        if auth["status"] != "VOIDED":
            auth["status"] = "VOIDED"
            sim.voids += 1
            for order in sim.orders.values():
                if order.get("authorization_id") == auth_id:
                    order["status"] = "VOIDED"
        return remember(request, 204, None)

    @app.get("/v2/payments/captures/{capture_id}")
    async def get_capture(capture_id: str, request: Request) -> Response:
        sim = sim_of(request)
        capture = sim.captures.get(capture_id)
        if capture is None:
            return _json(*_error(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID", "Not found"))
        return _json(200, sim.capture_document(capture))

    # -------------------------------------------------------------- controls
    @app.get("/_stats")
    async def stats(request: Request) -> dict[str, Any]:
        return sim_of(request).stats()

    @app.post("/_control/orders/{order_id}/approve")
    async def approve(order_id: str, request: Request) -> Response:
        order = sim_of(request).orders.get(order_id)
        if order is None:
            return _json(404, {"name": "RESOURCE_NOT_FOUND"})
        if order["status"] == "CREATED":
            order["status"] = "APPROVED"
        return _json(200, {"id": order_id, "status": order["status"]})

    @app.post("/_control/faults")
    async def faults(request: Request) -> dict[str, Any]:
        body = await request.json()
        sim_of(request).set_faults(body)
        return {"faults": sim_of(request).faults}

    @app.post("/_control/reset")
    async def reset(request: Request) -> dict[str, str]:
        sim_of(request).reset()
        return {"status": "reset"}

    return app


app = create_app()


def dumps(value: Any) -> str:  # tiny helper for scripts that print stats
    return json.dumps(value, indent=2, sort_keys=True)