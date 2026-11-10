from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import Owner, translate_errors
from backend.api.routes_metrics import get_session_factory
from backend.reliability import timing

router = APIRouter(prefix="/api", tags=["speed"])

# What SlothPay aims for (also shown on the dashboard).
OVERHEAD_P95_TARGET_MS = 100
CONDITION_TO_CAPTURE_TARGET_MS = 1000
FIRST_CHECK_TARGET_MS = 500
MAX_ROWS = 5000
RECENT_PAYMENTS = 20

_ROWS = text(
    """
    SELECT t.intent_id::text AS intent_id, t.step, t.kind, t.ms::float AS ms, t.ts
    FROM payment_timings t
    JOIN payments p ON p.id = t.intent_id
    WHERE p.merchant_id = :owner
    ORDER BY t.ts DESC
    LIMIT :limit
    """
)
_PAYMENTS = text(
    """
    SELECT id::text AS id, mode, amount::text AS amount, currency, created_at
    FROM payments WHERE id = ANY(:ids) AND merchant_id = :owner
    """
)


class KindStats(BaseModel):
    p50_ms: float | None = None
    p95_ms: float | None = None


class StepStats(KindStats):
    kind: str
    count: int


class RecentPayment(BaseModel):
    payment_id: str
    path: str
    amount: str
    currency: str
    created_at: datetime
    ours_ms: float
    provider_ms: float
    human_ms: float


class Targets(BaseModel):
    overhead_p95_ms: int = OVERHEAD_P95_TARGET_MS
    condition_to_capture_ms: int = CONDITION_TO_CAPTURE_TARGET_MS
    first_check_ms: int = FIRST_CHECK_TARGET_MS


class SpeedOut(BaseModel):
    label: str = "sandbox"
    payments: int
    ours: KindStats
    provider: KindStats
    human: KindStats
    overhead: KindStats
    steps: dict[str, StepStats]
    recent: list[RecentPayment]
    targets: Targets = Targets()
    note: str = (
        "Sandbox numbers. Overhead is SlothPay's own time per payment; PayPal's time and "
        "the buyer's time are shown apart and are not ours."
    )
    generated_at: datetime


async def compute_speed(session: AsyncSession, owner: str, now: datetime) -> SpeedOut:
    rows = [
        dict(row)
        for row in (await session.execute(_ROWS, {"owner": owner, "limit": MAX_ROWS}))
        .mappings()
        .all()
    ]
    summary = timing.summarize_rows(rows)
    # The newest payments first, by the time of their latest step (rows arrive newest first).
    order: list[str] = []
    for row in rows:
        if row["intent_id"] not in order:
            order.append(row["intent_id"])
        if len(order) >= RECENT_PAYMENTS:
            break
    totals = timing.per_payment(r for r in rows if r["intent_id"] in set(order))
    info: dict[str, Any] = {}
    if order:
        found = (
            await session.execute(_PAYMENTS, {"ids": [uuid.UUID(i) for i in order], "owner": owner})
        ).mappings()
        info = {row["id"]: row for row in found}
    recent = [
        RecentPayment(
            payment_id=intent,
            path=info[intent]["mode"],
            amount=info[intent]["amount"],
            currency=info[intent]["currency"],
            created_at=info[intent]["created_at"],
            ours_ms=round(totals[intent]["ours"], 1),
            provider_ms=round(totals[intent]["provider"], 1),
            human_ms=round(totals[intent]["human"], 1),
        )
        for intent in order
        if intent in info and intent in totals
    ]
    return SpeedOut(
        payments=summary["payments"],
        ours=KindStats(**summary["ours"]),
        provider=KindStats(**summary["provider"]),
        human=KindStats(**summary["human"]),
        overhead=KindStats(**summary["overhead"]),
        steps={name: StepStats(**stats) for name, stats in summary["steps"].items()},
        recent=recent,
        generated_at=now,
    )


@router.get("/speed", response_model=SpeedOut)
async def speed(
    owner: Owner,
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> SpeedOut:
    from datetime import UTC

    with translate_errors():
        async with factory() as session:
            return await compute_speed(session, owner, datetime.now(UTC))