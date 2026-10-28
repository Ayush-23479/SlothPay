"""Verified, deduplicated webhook inbox. No payment state transitions yet."""

from __future__ import annotations
from typing import Annotated
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.config.settings import get_settings
from backend.models.event import ProviderEvent
from backend.webhooks.verification import WebhookVerificationUnavailable, verify_paypal_webhook

router = APIRouter(tags=["paypal-webhooks"])
_engine = None
_maker = None


async def get_webhook_session():
    """Production DB dependency; tests can override it through FastAPI dependency_overrides."""
    global _engine, _maker
    if _maker is None:
        _engine = create_async_engine(get_settings().DATABASE_URL)
        _maker = async_sessionmaker(_engine, expire_on_commit=False)
    async with _maker() as session:
        yield session


@router.post("/webhooks/paypal")
async def receive_paypal_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_webhook_session)],
) -> dict[str, Any]:
    raw = await request.body()
    if not raw or len(raw) > 256 * 1024:
        raise HTTPException(status_code=400, detail="Invalid webhook size")
    try:
        event = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid webhook JSON") from exc
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")

    try:
        verified = await verify_paypal_webhook(request.headers, event)
    except WebhookVerificationUnavailable as exc:
        raise HTTPException(status_code=503, detail="Webhook verification unavailable") from exc
    if not verified:
        raise HTTPException(status_code=401, detail="Webhook signature rejected")

    event_id = event.get("id")
    event_type = event.get("event_type")
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 128:
        raise HTTPException(status_code=400, detail="Invalid event ID")
    if not isinstance(event_type, str) or not 1 <= len(event_type) <= 128:
        raise HTTPException(status_code=400, detail="Invalid event type")

    stmt = (
        insert(ProviderEvent)
        .values(
            provider="paypal",
            event_id=event_id,
            event_type=event_type,
            verified=True,
            payload=event,
        )
        .on_conflict_do_nothing(index_elements=["provider", "event_id"])
        .returning(ProviderEvent.id)
    )
    try:
        result = await session.execute(stmt)
        created = result.scalar_one_or_none() is not None
        await session.commit()
    except Exception:
        await session.rollback()
        raise  # return non-2xx so PayPal can redeliver; do not acknowledge loss
    return {"status": "accepted" if created else "duplicate", "event_id": event_id}
