"""HTTP entry point for PayPal webhook deliveries."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config.settings import get_settings
from backend.notifications.hub import get_hub
from backend.webhooks.handler import (
    APPLIED,
    DUPLICATE,
    IGNORED,
    REJECTED,
    VERIFICATION_UNAVAILABLE,
    WebhookDeps,
    process_webhook,
)

log = logging.getLogger("slothpay.webhooks.receiver")

MAX_BODY_BYTES = 256 * 1024
router = APIRouter(prefix="/webhooks", tags=["paypal-webhooks"])


async def get_webhook_deps() -> AsyncIterator[WebhookDeps]:
    """Provide per-request database resources; tests may override this dependency."""
    engine = create_async_engine(get_settings().DATABASE_URL)
    try:
        yield WebhookDeps(
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            publish=get_hub().publish,  # live screens refresh the moment a webhook applies
        )
    finally:
        await engine.dispose()


async def _read_limited_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Webhook body too large",
            )
        body.extend(chunk)
    return bytes(body)


@router.post("/paypal")
async def receive_paypal_webhook(
    request: Request,
    deps: Annotated[WebhookDeps, Depends(get_webhook_deps)],
) -> dict[str, str]:
    raw_body = await _read_limited_body(request)
    try:
        outcome = await process_webhook(request.headers, raw_body, deps)
    except Exception:
        log.exception("PayPal webhook processing failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook processing unavailable",
        ) from None

    if outcome == REJECTED:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Webhook signature rejected",
        )
    if outcome == VERIFICATION_UNAVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook verification unavailable",
        )
    if outcome not in {APPLIED, DUPLICATE, IGNORED}:
        log.error("PayPal webhook returned an unexpected outcome: %s", outcome)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook processing unavailable",
        )
    return {"status": "received"}