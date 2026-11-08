from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import Owner, PaymentsUnavailable, translate_errors
from backend.api.schemas import MetricsOut

router = APIRouter(prefix="/api", tags=["metrics"])

_COUNTS = text(
    """
    SELECT
      (SELECT count(*) FROM payments) AS total,
      (SELECT count(*) FROM payments WHERE status = 'CAPTURED') AS completed,
      (SELECT count(*) FROM payments WHERE status = 'UNCERTAIN') AS uncertain_now,
      (SELECT count(*) FROM payments p
         WHERE p.status <> 'UNCERTAIN'
           AND EXISTS (SELECT 1 FROM payment_audit a
                       WHERE a.payment_id = p.id
                         AND a.event_type = 'PROVIDER_OPERATION_OUTCOME_UNKNOWN')
           AND EXISTS (SELECT 1 FROM payment_audit a
                       WHERE a.payment_id = p.id AND a.event_type = 'RECONCILED')
      ) AS recovered,
      (SELECT count(*) FROM payment_audit
         WHERE event_type = 'DUPLICATE_SUPPRESSED') AS duplicates_suppressed,
      (SELECT count(*) FROM payment_audit
         WHERE event_type = 'RESCUE_RETRY_CLAIMED') AS retries
    """
)

# Seconds from the pay click (payment created) to the first CAPTURED transition.
_CAPTURE_LATENCY = text(
    """
    SELECT avg(secs) AS avg_s,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY secs) AS p95_s
    FROM (
      SELECT EXTRACT(EPOCH FROM (min(a.created_at) - p.created_at)) AS secs
      FROM payments p
      JOIN payment_audit a ON a.payment_id = p.id AND a.to_state = 'CAPTURED'
      GROUP BY p.id, p.created_at
    ) t
    """
)

_WEBHOOK_VERIFY = text(
    """
    SELECT avg(verify_ms) AS avg_ms,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY verify_ms) AS p95_ms
    FROM provider_events
    WHERE verify_ms IS NOT NULL
    """
)


def _round(value: Any, places: int) -> float | None:
    return None if value is None else round(float(value), places)


async def compute_metrics(session: AsyncSession) -> MetricsOut:
    counts = (await session.execute(_COUNTS)).mappings().one()
    latency = (await session.execute(_CAPTURE_LATENCY)).mappings().one()
    webhook = (await session.execute(_WEBHOOK_VERIFY)).mappings().one()
    return MetricsOut(
        total=int(counts["total"]),
        completed=int(counts["completed"]),
        uncertain_now=int(counts["uncertain_now"]),
        recovered=int(counts["recovered"]),
        duplicates_suppressed=int(counts["duplicates_suppressed"]),
        retries=int(counts["retries"]),
        capture_seconds_avg=_round(latency["avg_s"], 2),
        capture_seconds_p95=_round(latency["p95_s"], 2),
        webhook_verify_ms_avg=_round(webhook["avg_ms"], 1),
        webhook_verify_ms_p95=_round(webhook["p95_ms"], 1),
        generated_at=datetime.now(UTC),
    )


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise PaymentsUnavailable("The database is not available")
    return factory


@router.get("/metrics", response_model=MetricsOut)
async def metrics(
    _owner: Owner,  # Task 16: a session is required; the numbers are aggregate counts only
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> MetricsOut:
    with translate_errors():
        async with factory() as session:
            return await compute_metrics(session)