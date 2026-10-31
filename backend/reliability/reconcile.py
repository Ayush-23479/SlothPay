
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select

from backend.models.attempt import PaymentAttempt
from backend.models.payment import Payment
from backend.orchestration.reconcile import UNRESOLVED, reconcile_attempt

# Seconds to wait before read 1..5. Five entries = at most five PayPal reads.
RECONCILE_DELAYS: tuple[float, ...] = (0.3, 1.0, 2.0, 4.0, 8.0)

# Step decisions that end the loop: the payment moved, was already settled, or needs a human.
_STOP_DECISIONS = frozenset(
    {
        "EVIDENCE_APPLIED",
        "NOT_OCCURRED",
        "FINAL_STATE",
        "NOT_RECONCILABLE",
        "CONFLICT",
        "STALE_EVIDENCE",
        "EXHAUSTED",
        "NO_ATTEMPT",
        "AMBIGUOUS",
    }
)
# Decisions that mean "nothing proven yet, look again": status is left exactly as it was.
_RETRY_DECISIONS = frozenset({"UNKNOWN", "WAITING"})


class Reconciler(Protocol):
    """Anything that can settle an uncertain payment and report its status afterwards."""

    async def reconcile(self, intent_id: str) -> str: ...


@dataclass(frozen=True)
class StepOutcome:
    """Result of ONE reconciliation read."""

    decision: str
    status: str


@dataclass(frozen=True)
class ReconcileResult:
    status: str
    decision: str
    checks: int
    exhausted: bool
    retry_allowed: bool


ReconcileStep = Callable[[str], Awaitable[StepOutcome]]
Sleep = Callable[[float], Awaitable[Any]]


class DbReconcileStep:
    """One forced reconciliation read of the payment's single unresolved attempt."""

    def __init__(self, session_factory: Callable[[], Any], gateway: Any) -> None:
        self._session_factory = session_factory
        self._gateway = gateway

    async def __call__(self, intent_id: str) -> StepOutcome:
        payment_id = uuid.UUID(str(intent_id))
        async with self._session_factory() as session:
            payment = await session.get(Payment, payment_id)
            if payment is None:
                raise ValueError("Payment does not exist")
            attempt_ids = list(
                await session.scalars(
                    select(PaymentAttempt.id)
                    .where(
                        PaymentAttempt.payment_id == payment_id,
                        PaymentAttempt.status.in_(UNRESOLVED),
                    )
                    .order_by(PaymentAttempt.started_at, PaymentAttempt.id)
                )
            )
            if not attempt_ids:
                return StepOutcome("NO_ATTEMPT", payment.status)
            if len(attempt_ids) > 1:
                # Never guess which of several unresolved operations an answer belongs to.
                return StepOutcome("AMBIGUOUS", payment.status)
            observation = await reconcile_attempt(
                session, self._gateway, attempt_id=attempt_ids[0], force=True
            )
            return StepOutcome(observation.decision, observation.payment_status)


class StatusReconciler:
    """Bounded reconciliation loop. Implements the Reconciler protocol."""

    def __init__(
        self,
        step: ReconcileStep,
        *,
        sleep: Sleep = asyncio.sleep,
        delays: tuple[float, ...] = RECONCILE_DELAYS,
    ) -> None:
        if not delays or len(delays) > len(RECONCILE_DELAYS):
            raise ValueError("At most five reconciliation reads are allowed")
        if any(delay < 0 for delay in delays):
            raise ValueError("Reconciliation delays must not be negative")
        self._step = step
        self._sleep = sleep
        self._delays = tuple(delays)

    @classmethod
    def for_database(
        cls,
        session_factory: Callable[[], Any],
        gateway: Any,
        *,
        sleep: Sleep = asyncio.sleep,
    ) -> StatusReconciler:
        return cls(DbReconcileStep(session_factory, gateway), sleep=sleep)

    async def reconcile_detailed(self, intent_id: str) -> ReconcileResult:
        outcome = StepOutcome("UNKNOWN", "UNCERTAIN")
        checks = 0
        for delay in self._delays:
            await self._sleep(delay)
            outcome = await self._step(intent_id)
            if outcome.decision in _STOP_DECISIONS:
                break
            if outcome.decision not in _RETRY_DECISIONS:
                # An unrecognised decision is never treated as proof: stop and escalate.
                outcome = StepOutcome("AMBIGUOUS", outcome.status)
                break
            checks += 1
        else:
            # Every allowed read ran and none proved anything.
            return ReconcileResult(outcome.status, "EXHAUSTED", checks, True, False)
        counted = checks + (0 if outcome.decision in {"NO_ATTEMPT", "AMBIGUOUS"} else 1)
        return ReconcileResult(
            status=outcome.status,
            decision=outcome.decision,
            checks=counted,
            exhausted=outcome.decision == "EXHAUSTED",
            retry_allowed=outcome.decision == "NOT_OCCURRED",
        )

    async def reconcile(self, intent_id: str) -> str:
        """Return the payment status after reconciling. Never raises for a PayPal read failure."""
        return (await self.reconcile_detailed(intent_id)).status