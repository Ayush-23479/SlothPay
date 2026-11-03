from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import ApiError, Owner, PaymentId, PaymentsUnavailable, translate_errors
from backend.api.passport import build_passport, make_token, read_token
from backend.config.settings import get_settings
from backend.models.attempt import PaymentAttempt
from backend.models.audit import PaymentAudit
from backend.models.condition import PaymentCondition
from backend.models.event import ProviderEvent
from backend.models.payment import Payment
from backend.models.repository import get_intent_created_evidence

router = APIRouter(prefix="/api", tags=["passport"])

MAX_ROWS = 500


def passport_not_found() -> ApiError:
    return ApiError(404, "not_found", "Passport not found")


class ShareOut(BaseModel):
    token: str
    url: str
    expires_at: datetime


class PassportSource(Protocol):
    """Loads the rows a passport is built from. None when the payment does not exist."""

    async def load(self, payment_id: uuid.UUID) -> dict[str, Any] | None: ...


class DbPassportSource:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def load(self, payment_id: uuid.UUID) -> dict[str, Any] | None:
        async with self._factory() as session:
            payment = await session.get(Payment, payment_id, populate_existing=True)
            if payment is None:
                return None
            audit = (
                await session.scalars(
                    select(PaymentAudit)
                    .where(PaymentAudit.payment_id == payment_id)
                    .order_by(PaymentAudit.created_at, PaymentAudit.id)
                    .limit(MAX_ROWS)
                )
            ).all()
            attempts = (
                await session.scalars(
                    select(PaymentAttempt)
                    .where(PaymentAttempt.payment_id == payment_id)
                    .order_by(PaymentAttempt.started_at)
                    .limit(MAX_ROWS)
                )
            ).all()
            provider = (
                await session.scalars(
                    select(ProviderEvent)
                    .where(ProviderEvent.payment_id == payment_id)
                    .order_by(ProviderEvent.received_at)
                    .limit(MAX_ROWS)
                )
            ).all()
            condition_row = await session.scalar(
                select(PaymentCondition)
                .where(PaymentCondition.payment_id == payment_id)
                .order_by(PaymentCondition.created_at.desc())
                .limit(1)
            )
            original = await get_intent_created_evidence(session, payment_id) or {}
        raw = original.get("condition") if isinstance(original, dict) else None
        condition = None
        if payment.mode == "CONDITIONALPAY":
            condition = {
                "description": raw.get("description") if isinstance(raw, dict) else None,
                "status": condition_row.status if condition_row else "PENDING",
                "verified_at": condition_row.verified_at if condition_row else None,
                "evidence": condition_row.evidence if condition_row else None,
            }
        return {
            "intent": {
                "id": str(payment.id),
                "path": payment.mode,
                "amount": f"{payment.amount:.2f}",
                "currency": payment.currency,
                "status": payment.status,
                "owner_id": payment.merchant_id,
            },
            "events": [
                {
                    "event_type": row.event_type,
                    "from_state": row.from_state,
                    "to_state": row.to_state,
                    "actor": row.actor,
                    "evidence": row.evidence,
                    "created_at": row.created_at,
                }
                for row in audit
            ],
            "attempts": [
                {"operation": row.operation, "request_id": row.request_id, "status": row.status}
                for row in attempts
            ],
            "provider_events": [
                {
                    "event_id": row.event_id,
                    "event_type": row.event_type,
                    "verified": row.verified,
                    "received_at": row.received_at,
                }
                for row in provider
            ],
            "condition": condition,
        }


def get_passport_source(request: Request) -> PassportSource:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise PaymentsUnavailable("The database is not available")
    return DbPassportSource(factory)


Source = Annotated[PassportSource, Depends(get_passport_source)]


def _build(data: dict[str, Any], *, shared: bool) -> dict[str, Any]:
    return build_passport(
        data["intent"],
        data["events"],
        data["attempts"],
        data["condition"],
        data["provider_events"],
        shared=shared,
    )


def _now() -> datetime:
    return datetime.now(UTC)


@router.get("/payments/{payment_id}/passport")
async def owner_passport(payment_id: PaymentId, owner: Owner, source: Source) -> dict[str, Any]:
    with translate_errors():
        data = await source.load(payment_id)
    if data is None or data["intent"]["owner_id"] != owner:
        raise passport_not_found()
    return _build(data, shared=False)


@router.post("/payments/{payment_id}/passport/share", response_model=ShareOut)
async def share_passport(payment_id: PaymentId, owner: Owner, source: Source) -> ShareOut:
    settings = get_settings()
    with translate_errors():
        data = await source.load(payment_id)
    if data is None or data["intent"]["owner_id"] != owner:
        raise passport_not_found()
    try:
        token, expires = make_token(payment_id, settings.PASSPORT_SIGNING_SECRET, _now())
    except ValueError:
        raise ApiError(
            503, "sharing_unavailable", "Sharing is not set up on this server."
        ) from None
    origin = settings.FRONTEND_ORIGIN.rstrip("/")
    return ShareOut(token=token, url=f"{origin}/passport/{token}", expires_at=expires)


@router.get("/passport/{token}")
async def shared_passport(token: str, source: Source) -> dict[str, Any]:
    payment_id = read_token(token, get_settings().PASSPORT_SIGNING_SECRET, _now())
    if payment_id is None:
        raise passport_not_found()
    with translate_errors():
        data = await source.load(payment_id)
    if data is None:
        raise passport_not_found()
    return _build(data, shared=True)