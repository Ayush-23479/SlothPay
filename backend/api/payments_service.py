from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from redis.asyncio import Redis
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import (
    ApiError,
    Caller,
    NotAllowed,
    PaymentsUnavailable,
)
from backend.api.schemas import EventOut, PaymentOut, PayPalIdsOut, RescueOut
from backend.config.settings import Settings, get_settings
from backend.models.attempt import PaymentAttempt
from backend.models.audit import PaymentAudit
from backend.models.payment import Payment
from backend.models.repository import (
    StalePaymentVersion,
    add_audit_event,
    get_intent_created_evidence,
    transition_payment,
)
from backend.notifications.cursor import format_cursor, parse_cursor
from backend.notifications.hub import EventHub
from backend.orchestration.conditions import ConditionNotReady, PaymentMissing, PaymentService
from backend.orchestration.lifecycle import (
    LifecycleSafetyError,
    OperationAlreadyStarted,
    create_order,
    execute_operation,
    verify_buyer_approval,
)
from backend.payments.paypal_client import PayPalError, PayPalNotSent, PayPalOutcomeUnknown
from backend.reliability.idempotency import register_intent
from backend.reliability.reconcile import DbReconcileStep
from backend.reliability.rescue import DbRescueService, build_rescue_plan, execute_rescue_action
from backend.state_machine.policy import allowed_actions, display_status_for
from backend.state_machine.states import (
    APPROVED,
    AWAITING_APPROVAL,
    AWAITING_CONDITION,
    CONDITIONALPAY,
    INTENT_CREATED,
    ORDER_CREATED,
    SMARTPAY,
    UNCERTAIN,
    normalize_status,
)
from backend.state_machine.transitions import validate_transition
from backend.workflows.client import TemporalWorkflowStarter

log = logging.getLogger("slothpay.api.service")

PAYPAL_ERRORS = (PayPalNotSent, PayPalOutcomeUnknown, PayPalError)
SessionFactory = Callable[[], AsyncSession]
PaypalFactory = Callable[[], AbstractAsyncContextManager[Any]]
MAX_PAGE = 100


# ----------------------------------------------------------------------------- helpers
def mask_id(value: str | None) -> str | None:
    """First 4 and last 4 characters only; short ids show 2 characters."""
    if not value:
        return None
    if len(value) <= 8:
        return f"{value[:2]}…"
    return f"{value[:4]}…{value[-4:]}"


def approval_url(order_id: str | None, status: str, paypal_base_url: str) -> str | None:
    """Where the buyer approves the payment. Only offered while approval is still pending."""
    if not order_id or normalize_status(status) not in {ORDER_CREATED, AWAITING_APPROVAL}:
        return None
    host = (
        "https://www.sandbox.paypal.com"
        if "sandbox" in paypal_base_url.lower()
        else "https://www.paypal.com"
    )
    return f"{host}/checkoutnow?token={quote(order_id, safe='')}"


_STATE_TITLES = {
    "INTENT_CREATED": "Payment started",
    "ORDER_CREATED": "PayPal order created",
    "AWAITING_APPROVAL": "Waiting for the buyer to approve",
    "APPROVED": "Buyer approved",
    "AUTHORIZE_REQUESTED": "Holding the funds",
    "AUTHORIZED": "Funds held",
    "AWAITING_CONDITION": "Waiting for the condition",
    "CAPTURE_REQUESTED": "Collecting the payment",
    "CAPTURE_PENDING": "Payment is pending at PayPal",
    "CAPTURED": "Payment completed",
    "VOID_REQUESTED": "Releasing the held funds",
    "VOIDED": "Held funds released",
    "FAILED": "Payment failed",
    "CANCELLED": "Payment cancelled",
    "UNCERTAIN": "Checking the result with PayPal",
}
_SAFE_TEXT = re.compile(r"[A-Za-z0-9 _.,:'()-]{1,120}")


def _title_case(event_type: str) -> str:
    return event_type.replace("_", " ").strip().capitalize() or "Event"


def audit_event_out(row: PaymentAudit) -> EventOut:
    """One audit row as the buyer-safe EventOut. Raw evidence is never copied through."""
    evidence = row.evidence if isinstance(row.evidence, dict) else {}
    to_state = normalize_status(row.to_state) or row.to_state
    from_state = normalize_status(row.from_state) or row.from_state
    if row.event_type == "STATE_TRANSITION" and to_state in _STATE_TITLES:
        title = _STATE_TITLES[to_state]
    else:
        title = _title_case(row.event_type)
    if from_state and to_state:
        summary = f"Status changed from {from_state} to {to_state}."
    else:
        reason = evidence.get("reason")
        summary = reason if isinstance(reason, str) and _SAFE_TEXT.fullmatch(reason) else title
    source = evidence.get("source")
    return EventOut(
        id=format_cursor(row.created_at, row.id),
        type=row.event_type,
        title=title,
        summary=summary,
        from_status=from_state,
        to_status=to_state,
        source=source if isinstance(source, str) and source else row.actor,
        confirmed=evidence.get("confirmed") is True,
        created_at=row.created_at,
    )


def _after_cursor(after: str | None):
    if after is None:
        return None
    try:
        created_at, row_id = parse_cursor(after)
    except ValueError:
        raise ApiError(400, "bad_cursor", "The cursor is not valid") from None
    return or_(
        PaymentAudit.created_at > created_at,
        and_(PaymentAudit.created_at == created_at, PaymentAudit.id > row_id),
    )


_RESULT_KEYS = ("state", "executed", "needs_reconciliation", "decision", "payment_status")


def _safe_result(result: Any) -> dict[str, Any]:
    """Only plain status facts leave the server: never a PayPal response body."""
    if not isinstance(result, dict):
        return {}
    return {
        key: result[key]
        for key in _RESULT_KEYS
        if key in result and (result[key] is None or isinstance(result[key], str | bool | int))
    }


class LazyWorkflowStarter:
    """Connects to Temporal on first use, so the API can boot (and /health can answer) while
    Temporal is down. A failure to start is reported to the caller, which records it."""

    def __init__(self) -> None:
        self._starter: TemporalWorkflowStarter | None = None
        self._lock = asyncio.Lock()

    async def _get(self) -> TemporalWorkflowStarter:
        async with self._lock:
            if self._starter is None:
                self._starter = await TemporalWorkflowStarter.connect()
            return self._starter

    async def start(self, intent_id: str, deadline_iso: str) -> bool:
        return await (await self._get()).start(intent_id, deadline_iso)

    async def signal(self, intent_id: str, verified: bool, evidence: dict[str, Any]) -> bool:
        return await (await self._get()).signal(intent_id, verified, evidence)


# ------------------------------------------------------------------------------ service
class DbPaymentApi:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        redis: Redis,
        hub: EventHub,
        paypal_factory: PaypalFactory,
        starter: Any,
        settings: Settings | None = None,
    ) -> None:
        self._sessions = session_factory
        self._redis = redis
        self._hub = hub
        self._paypal_factory = paypal_factory
        self._starter = starter
        self._settings = settings or get_settings()

    # --- plumbing -----------------------------------------------------------------
    @asynccontextmanager
    async def _gateway(self) -> AsyncIterator[Any]:
        async with AsyncExitStack() as stack:
            try:
                gateway = await stack.enter_async_context(self._paypal_factory())
            except ValueError as exc:  # PayPalClient refuses to start without sandbox keys
                raise PaymentsUnavailable("The PayPal sandbox is not configured") from exc
            yield gateway

    async def _notify(self, payment_id: uuid.UUID) -> None:
        await self._hub.publish(str(payment_id), {"type": "payment.updated"})

    async def _publish(self, intent_id: str, event: dict[str, Any]) -> None:
        await self._hub.publish(intent_id, event)

    @property
    def publisher(self) -> Callable[[str, dict[str, Any]], Awaitable[Any]]:
        """Pass this to PaymentService(on_event=...) and WebhookDeps(publish=...)."""
        return self._publish

    async def _owned(
        self, session: AsyncSession, owner_id: str, payment_id: uuid.UUID
    ) -> Payment | None:
        payment = await session.get(Payment, payment_id, populate_existing=True)
        if payment is None or payment.merchant_id != owner_id:
            return None
        return payment

    async def _view(self, session: AsyncSession, payment: Payment) -> PaymentOut:
        context: dict[str, Any] = {}
        if normalize_status(payment.status) in {AWAITING_CONDITION, UNCERTAIN}:
            intent = await DbRescueService(session, None).load_intent(str(payment.id))
            context = {
                "reconcile_exhausted": intent["reconcile_exhausted"],
                "condition_verified": intent["condition_verified"],
            }
        rows = (
            await session.execute(
                select(PaymentAttempt.operation, PaymentAttempt.provider_operation_id)
                .where(
                    PaymentAttempt.payment_id == payment.id,
                    PaymentAttempt.operation.in_(("authorize", "capture")),
                    PaymentAttempt.status.in_(("SUCCEEDED", "PENDING")),
                    PaymentAttempt.provider_operation_id.is_not(None),
                )
                .order_by(PaymentAttempt.started_at)
            )
        ).all()
        ids = {operation: provider_id for operation, provider_id in rows}
        return PaymentOut(
            id=str(payment.id),
            path=payment.mode,
            amount=f"{payment.amount:.2f}",
            currency=payment.currency,
            status=normalize_status(payment.status) or payment.status,
            display_status=display_status_for(payment.status, context),
            allowed_actions=allowed_actions(payment.mode, payment.status, context),
            approval_url=approval_url(
                payment.paypal_order_id, payment.status, self._settings.PAYPAL_BASE_URL
            ),
            paypal_ids=PayPalIdsOut(
                order_id=mask_id(payment.paypal_order_id),
                authorization_id=mask_id(ids.get("authorize")),
                capture_id=mask_id(ids.get("capture")),
            ),
            created_at=payment.created_at,
            updated_at=payment.updated_at,
        )

    async def _view_by_id(self, payment_id: uuid.UUID) -> PaymentOut | None:
        async with self._sessions() as session:
            payment = await session.get(Payment, payment_id, populate_existing=True)
            return await self._view(session, payment) if payment else None

    # --- create + checkout ------------------------------------------------------------
    async def create_payment(
        self, owner_id: str, key: str, payload: dict[str, Any]
    ) -> tuple[PaymentOut, bool]:
        async with self._sessions() as session:
            intent, replay = await register_intent(session, self._redis, owner_id, key, payload)
        payment_id = uuid.UUID(intent["id"])
        if normalize_status(intent["status"]) == INTENT_CREATED:
            # A first request, or a replay whose first attempt stopped before PayPal answered.
            # The durable attempt row makes a second pass safe: it cannot send a second order.
            await self._start_checkout(payment_id)
        await self._notify(payment_id)
        view = await self._view_by_id(payment_id)
        assert view is not None
        return view, replay

    async def _start_checkout(self, payment_id: uuid.UUID) -> None:
        async with self._gateway() as gateway, self._sessions() as session:
            payment = await session.get(Payment, payment_id, populate_existing=True)
            if payment is None:
                return
            if payment.status == "CREATED":
                # The table default is CREATED; the state machine calls it INTENT_CREATED.
                try:
                    await transition_payment(
                        session,
                        payment_id=payment_id,
                        expected_version=payment.version,
                        expected_status="CREATED",
                        new_status=INTENT_CREATED,
                        actor="api",
                        evidence={
                            "source": "internal_record",
                            "confirmed": False,
                            "phase": "checkout_started",
                        },
                    )
                    await session.commit()
                except StalePaymentVersion:
                    await session.rollback()
                    return  # another request is already moving this payment
            try:
                outcome = await create_order(session, gateway, payment_id=payment_id)
            except (OperationAlreadyStarted, LifecycleSafetyError):
                await session.rollback()
                return
            except PAYPAL_ERRORS as exc:
                # The attempt and its state are saved; reconciliation decides, never a replay.
                await session.rollback()
                log.warning("create_order did not complete: %s", type(exc).__name__)
                return
            if outcome.get("state") != "ORDER_CREATED":
                return
            try:
                await verify_buyer_approval(session, gateway, payment_id=payment_id)
            except (*PAYPAL_ERRORS, LifecycleSafetyError) as exc:
                await session.rollback()  # the order exists; the buyer can still approve it
                log.warning("approval check skipped: %s", type(exc).__name__)

    # --- reads ----------------------------------------------------------------------
    async def list_payments(
        self, owner_id: str, limit: int, cursor: str | None
    ) -> tuple[list[PaymentOut], str | None]:
        limit = max(1, min(limit, MAX_PAGE))
        query = select(Payment).where(Payment.merchant_id == owner_id)
        if cursor is not None:
            try:
                created_at, row_id = parse_cursor(cursor)
            except ValueError:
                raise ApiError(400, "bad_cursor", "The cursor is not valid") from None
            query = query.where(
                or_(
                    Payment.created_at < created_at,
                    and_(Payment.created_at == created_at, Payment.id < row_id),
                )
            )
        query = query.order_by(Payment.created_at.desc(), Payment.id.desc()).limit(limit + 1)
        async with self._sessions() as session:
            rows = list((await session.scalars(query)).all())
            page = rows[:limit]
            items = [await self._view(session, payment) for payment in page]
            more = len(rows) > limit
            next_cursor = format_cursor(page[-1].created_at, page[-1].id) if more else None
        return items, next_cursor

    async def get_payment(self, owner_id: str, payment_id: uuid.UUID) -> PaymentOut | None:
        async with self._sessions() as session:
            payment = await self._owned(session, owner_id, payment_id)
            return await self._view(session, payment) if payment else None

    async def list_events(
        self, owner_id: str, payment_id: uuid.UUID, after: str | None, limit: int
    ) -> list[EventOut] | None:
        condition = _after_cursor(after)
        query = select(PaymentAudit).where(PaymentAudit.payment_id == payment_id)
        if condition is not None:
            query = query.where(condition)
        query = query.order_by(PaymentAudit.created_at, PaymentAudit.id).limit(
            max(1, min(limit, 500))
        )
        async with self._sessions() as session:
            if await self._owned(session, owner_id, payment_id) is None:
                return None
            return [audit_event_out(row) for row in (await session.scalars(query)).all()]

    async def latest_event_cursor(self, owner_id: str, payment_id: uuid.UUID) -> str | None:
        async with self._sessions() as session:
            if await self._owned(session, owner_id, payment_id) is None:
                return None
            row = await session.scalar(
                select(PaymentAudit)
                .where(PaymentAudit.payment_id == payment_id)
                .order_by(PaymentAudit.created_at.desc(), PaymentAudit.id.desc())
                .limit(1)
            )
            return format_cursor(row.created_at, row.id) if row else None

    # --- buyer comes back from PayPal -------------------------------------------------
    async def buyer_returned(self, owner_id: str, payment_id: uuid.UUID) -> PaymentOut | None:
        async with self._sessions() as session:
            payment = await self._owned(session, owner_id, payment_id)
            if payment is None:
                return None
            status, mode = normalize_status(payment.status), payment.mode
        async with self._gateway() as gateway:
            if status == UNCERTAIN:
                # One forced read of PayPal (Task 8) settles what happened before we go on.
                await DbReconcileStep(self._sessions, gateway)(str(payment_id))
                status = await self._status_of(payment_id)
            if status in {ORDER_CREATED, AWAITING_APPROVAL}:
                async with self._sessions() as session:
                    try:
                        status = (
                            await verify_buyer_approval(session, gateway, payment_id=payment_id)
                        )["state"]
                    except (LifecycleSafetyError, StalePaymentVersion):
                        # Another request moved the payment first: use whatever it became.
                        await session.rollback()
                        status = await self._status_of(payment_id)
            if status == APPROVED:
                await self._after_approval(gateway, payment_id, mode)
        await self._notify(payment_id)
        return await self._view_by_id(payment_id)

    async def _status_of(self, payment_id: uuid.UUID) -> str | None:
        async with self._sessions() as session:
            payment = await session.get(Payment, payment_id, populate_existing=True)
            return normalize_status(payment.status) if payment else None

    async def _after_approval(self, gateway: Any, payment_id: uuid.UUID, mode: str) -> None:
        operation = "capture" if mode == SMARTPAY else "authorize"
        async with self._sessions() as session:
            try:
                outcome = await execute_operation(
                    session, gateway, payment_id=payment_id, operation=operation
                )
            except (OperationAlreadyStarted, LifecycleSafetyError, StalePaymentVersion):
                await session.rollback()
                return  # someone else already owns this operation
            except PAYPAL_ERRORS as exc:
                await session.rollback()
                log.warning("%s did not complete: %s", operation, type(exc).__name__)
                return  # saved as UNCERTAIN or claimed: reconciliation decides, no replay
        if operation == "authorize" and outcome.get("state") == AWAITING_CONDITION:
            await self._start_condition_workflow(payment_id)

    async def _start_condition_workflow(self, payment_id: uuid.UUID) -> None:
        async with self._sessions() as session:
            original = await get_intent_created_evidence(session, payment_id) or {}
        condition = original.get("condition") if isinstance(original, dict) else None
        minutes = condition.get("deadline_minutes") if isinstance(condition, dict) else None
        if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 1:
            minutes = self._settings.CONDITION_MAX_HOURS * 60
        deadline = (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()
        try:
            await self._starter.start(str(payment_id), deadline)
        except Exception as exc:
            # Held funds with no timer: make it visible to operators instead of silent.
            log.exception("could not start the condition workflow for %s", payment_id)
            async with self._sessions() as session:
                await add_audit_event(
                    session,
                    payment_id=payment_id,
                    event_type="CONDITION_WORKFLOW_NOT_STARTED",
                    actor="api",
                    evidence={
                        "source": "internal_record",
                        "confirmed": False,
                        "reason": type(exc).__name__,
                    },
                )
                await session.commit()

    # --- condition, cancel ---------------------------------------------------------------
    async def verify_condition(
        self, caller: Caller, payment_id: uuid.UUID, verified: bool, evidence: dict[str, Any]
    ) -> PaymentOut | None:
        async with self._sessions() as session:
            payment = await session.get(Payment, payment_id, populate_existing=True)
            if payment is None or (not caller.is_admin and payment.merchant_id != caller.owner_id):
                return None
            if payment.mode != CONDITIONALPAY:
                raise NotAllowed("not_conditional", "This payment has no condition")
            actor = "admin" if caller.is_admin else f"owner:{caller.owner_id}"
            await add_audit_event(
                session,
                payment_id=payment_id,
                event_type="CONDITION_VERIFY_REQUESTED",
                actor=actor[:128],
                evidence={"source": "api", "confirmed": False, "verified": verified is True},
            )
            await session.commit()
        await self._resolve(payment_id, verified is True, evidence)
        await self._notify(payment_id)
        return await self._view_by_id(payment_id)

    async def _resolve(
        self,
        payment_id: uuid.UUID,
        verified: bool,
        evidence: dict[str, Any],
        reason: str | None = None,
    ) -> None:
        async with self._gateway() as gateway:
            service = PaymentService(self._sessions, gateway, on_event=self._publish)
            try:
                await service.resolve_condition(str(payment_id), verified, evidence, reason)
            except ConditionNotReady as exc:
                raise NotAllowed(
                    "condition_not_ready", "The funds are not held yet; try again shortly"
                ) from exc
            except PaymentMissing as exc:
                raise ApiError(404, "not_found", "Payment not found") from exc

    async def cancel(self, owner_id: str, payment_id: uuid.UUID) -> PaymentOut | None:
        async with self._sessions() as session:
            payment = await self._owned(session, owner_id, payment_id)
            if payment is None:
                return None
            status = normalize_status(payment.status)
            if payment.mode == CONDITIONALPAY and status == AWAITING_CONDITION:
                held = True
            elif status in {INTENT_CREATED, AWAITING_APPROVAL}:
                held = False
                allowed, _reason = validate_transition(payment.mode, payment.status, "CANCELLED")
                if not allowed:
                    raise NotAllowed("not_cancellable", "This payment can no longer be cancelled")
                await transition_payment(
                    session,
                    payment_id=payment_id,
                    expected_version=payment.version,
                    expected_status=payment.status,
                    new_status="CANCELLED",
                    actor=f"owner:{owner_id}"[:128],
                    evidence={"source": "api", "confirmed": False, "reason": "cancelled_by_owner"},
                )
                await session.commit()
            else:
                raise NotAllowed("not_cancellable", "This payment can no longer be cancelled")
        if held:
            # Funds are held: cancelling means voiding them, through the guarded path.
            await self._resolve(payment_id, False, {"cancelled_by": "owner"}, reason="cancelled")
        await self._notify(payment_id)
        return await self._view_by_id(payment_id)

    # --- rescue ---------------------------------------------------------------------------
    async def rescue_plan(self, owner_id: str, payment_id: uuid.UUID) -> RescueOut | None:
        async with self._sessions() as session:
            if await self._owned(session, owner_id, payment_id) is None:
                return None
            intent = await DbRescueService(session, None).load_intent(str(payment_id))
        plan = build_rescue_plan(intent, intent.get("last_error"), intent.get("provider_status"))
        return RescueOut(
            payment_id=str(payment_id),
            display_status=plan["display_status"],
            facts=list(plan["facts"]),
            allowed_actions=list(plan["allowed_actions"]),
            recommended_action=plan["recommended_action"],
            needs_human=bool(plan["needs_human"]),
        )

    async def rescue_action(
        self, owner_id: str, payment_id: uuid.UUID, action: str
    ) -> dict[str, Any] | None:
        async with self._sessions() as session:
            if await self._owned(session, owner_id, payment_id) is None:
                return None
        async with self._gateway() as gateway, self._sessions() as session:
            outcome = await execute_rescue_action(
                DbRescueService(session, gateway, actor=f"owner:{owner_id}"[:128]),
                str(payment_id),
                action,
            )
        if outcome.get("error"):
            raise NotAllowed("not_allowed", "That action is not allowed right now")
        await self._notify(payment_id)
        return {"action": outcome["action"], "result": _safe_result(outcome.get("result"))}