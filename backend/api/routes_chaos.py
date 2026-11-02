from __future__ import annotations

import asyncio
import hmac
import uuid
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.api.deps import ApiError, translate_errors
from backend.api.schemas import PaymentCreate
from backend.config.settings import Settings, get_settings
from backend.reliability.chaos import (
    ChaosController,
    ChaosDisabled,
    ChaosInvalid,
    chaos_allowed,
    validate_state,
)

router = APIRouter(prefix="/api/chaos", tags=["chaos"])

MAX_DOUBLE_CLICKS = 20
DEMO_OWNER = "chaos-demo"
_ORDER_PATH = "/api/payments"


def require_chaos_admin(
    x_admin_api_key: Annotated[str | None, Header()] = None,
) -> Settings:
    settings = get_settings()
    if not chaos_allowed(settings):
        raise ApiError(404, "not_found", "Not found")
    configured = settings.ADMIN_API_KEY
    if (
        not configured
        or not x_admin_api_key
        or not hmac.compare_digest(configured, x_admin_api_key)
    ):
        raise ApiError(401, "unauthorized", "Missing or invalid admin key")
    return settings


Admin = Annotated[Settings, Depends(require_chaos_admin)]


def get_controller(settings: Admin) -> ChaosController:
    return ChaosController(settings)


Controller = Annotated[ChaosController, Depends(get_controller)]


class ChaosIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["none", "delay", "drop_before_send", "drop_after_send", "mock_error"]
    probability: float = Field(default=1.0, ge=0, le=1)
    delay_ms: int = Field(default=0, ge=0, le=10_000)
    code: str | None = Field(default=None, max_length=64)
    remaining_count: int | None = Field(default=None, ge=1, le=1_000)


class ChaosOut(BaseModel):
    mode: str
    probability: float
    delay_ms: int
    code: str | None = None
    remaining_count: int | None = None
    chaos_enabled: bool = True


async def _current(controller: ChaosController) -> ChaosOut:
    state = await controller.get_state()
    return ChaosOut(**state.as_dict())


@router.get("", response_model=ChaosOut)
async def read_chaos(controller: Controller) -> ChaosOut:
    with translate_errors():
        return await _current(controller)


@router.post("", response_model=ChaosOut)
async def set_chaos(body: ChaosIn, controller: Controller) -> ChaosOut:
    try:
        state = validate_state(
            body.mode, body.probability, body.delay_ms, body.code, body.remaining_count
        )
        with translate_errors():
            await controller.set_state(state)
            return await _current(controller)
    except ChaosInvalid as exc:
        raise ApiError(422, "invalid_chaos", str(exc)) from exc
    except ChaosDisabled as exc:
        raise ApiError(404, "not_found", "Not found") from exc


class DoubleClickIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n: int = Field(default=5, ge=1, le=MAX_DOUBLE_CLICKS)
    payment: PaymentCreate


class DoubleClickOut(BaseModel):
    requested: int
    accepted: int
    replays: int
    busy: int
    failed: int
    intents: int
    paypal_orders: int
    safe: bool


def _body_of(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@router.post("/double-click", response_model=DoubleClickOut)
async def double_click(body: DoubleClickIn, request: Request, _: Admin) -> DoubleClickOut:
    """Fire N identical create requests with ONE Idempotency-Key at this API.

    A safe system ends with one payment intent and at most one PayPal order. PayPal order
    counts use the masked ids the API returns (first and last four characters).
    """
    key = f"chaos-{uuid.uuid4().hex}"
    payload = body.payment.model_dump(mode="json")
    headers = {"Idempotency-Key": key, "X-Owner-Id": DEMO_OWNER}
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://slothpay.local") as http:
        responses = await asyncio.gather(
            *(http.post(_ORDER_PATH, json=payload, headers=headers) for _ in range(body.n)),
            return_exceptions=True,
        )
    intents: set[str] = set()
    orders: set[str] = set()
    accepted = replays = busy = failed = 0
    for response in responses:
        if isinstance(response, BaseException):
            failed += 1
            continue
        data = _body_of(response)
        if response.status_code in (200, 201):
            accepted += 1
            if response.headers.get("Idempotent-Replay", "").lower() == "true":
                replays += 1
            if isinstance(data.get("id"), str):
                intents.add(data["id"])
            ids = data.get("paypal_ids")
            order_id = ids.get("order_id") if isinstance(ids, dict) else None
            if isinstance(order_id, str):
                orders.add(order_id)
        elif data.get("error") == "intent_busy":
            busy += 1
        else:
            failed += 1
    return DoubleClickOut(
        requested=body.n,
        accepted=accepted,
        replays=replays,
        busy=busy,
        failed=failed,
        intents=len(intents),
        paypal_orders=len(orders),
        safe=len(intents) <= 1 and len(orders) <= 1,
    )