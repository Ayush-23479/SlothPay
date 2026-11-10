from __future__ import annotations

import re
from contextlib import AsyncExitStack
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.api.deps import ApiError, Owner, PaymentsUnavailable
from backend.config.settings import Settings, get_settings
from backend.reliability.baseline import run_naive
from backend.reliability.chaos import chaos_allowed

router = APIRouter(prefix="/api/baseline", tags=["baseline"])

_AMOUNT = re.compile(r"^\d{1,7}\.\d{2}$")
MAX_TIMEOUT_SECONDS = 30.0


def require_baseline_allowed() -> Settings:
    settings = get_settings()
    if not chaos_allowed(settings):
        raise ApiError(404, "not_found", "Not found")
    return settings


Allowed = Annotated[Settings, Depends(require_baseline_allowed)]


class BaselineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: str = Field(default="5.00", max_length=12)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    timeout_seconds: float | None = Field(default=None, ge=1, le=MAX_TIMEOUT_SECONDS)


class BaselineOut(BaseModel):
    arm: str
    label: str
    outcome: str
    attempts: int
    paypal_orders_possible: int
    order_ids: list[str | None]
    waited_ms: float
    provider_ms: float
    ours_ms: float
    total_ms: float
    timeout_seconds: float
    request_id_used: bool
    note: str


@router.post("/pay", response_model=BaselineOut)
async def baseline_pay(
    body: BaselineIn, request: Request, settings: Allowed, _owner: Owner
) -> dict[str, Any]:
    if not _AMOUNT.match(body.amount):
        raise ApiError(422, "invalid_request", "Amount must look like 5.00")
    if body.currency.upper() not in settings.currency_allowlist:
        raise ApiError(422, "invalid_request", "That currency is not allowed")
    factory = getattr(request.app.state, "paypal_factory", None)
    if factory is None:
        raise PaymentsUnavailable("The PayPal sandbox is not available")
    timeout = body.timeout_seconds or min(settings.BASELINE_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS)
    async with AsyncExitStack() as stack:
        try:
            gateway = await stack.enter_async_context(factory())
        except ValueError as exc:
            raise PaymentsUnavailable("The PayPal sandbox is not configured") from exc
        return await run_naive(
            gateway,
            amount=body.amount,
            currency=body.currency.upper(),
            timeout_seconds=timeout,
            return_url=f"{settings.FRONTEND_ORIGIN.rstrip('/')}/compare",
        )