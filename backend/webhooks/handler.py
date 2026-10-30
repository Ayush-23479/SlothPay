from __future__ import annotations

import inspect
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.attempt import PaymentAttempt
from backend.models.event import ProviderEvent
from backend.models.payment import Payment
from backend.models.repository import (
    StalePaymentVersion,
    add_audit_event,
    record_provider_event_once,
    transition_payment,
)
from backend.state_machine.states import (
    APPROVED,
    AUTHORIZE_REQUESTED,
    AUTHORIZED,
    AWAITING_APPROVAL,
    AWAITING_CONDITION,
    CAPTURE_PENDING,
    CAPTURE_REQUESTED,
    CAPTURED,
    CONDITIONALPAY,
    FAILED,
    ORDER_CREATED,
    UNCERTAIN,
    VOID_REQUESTED,
    VOIDED,
)
from backend.state_machine.transitions import resolve_uncertain, validate_transition
from backend.webhooks.events_map import (
    ACTION_OPERATION,
    EVENT_MAP,
    PROVIDER_STATUS,
    extract_amount,
    extract_order_id,
    extract_resource_id,
    mask_payload,
)
from backend.webhooks.verification import WebhookVerificationUnavailable, verify_paypal_webhook

log = logging.getLogger("slothpay.webhooks")

ACTOR = "paypal_webhook"
Publish = Callable[[str, dict[str, Any]], Awaitable[None] | None]
Verify = Callable[[Mapping[str, str], bytes], Awaitable[bool]]


def _no_publish(intent_id: str, event: dict[str, Any]) -> None:
    """Default notification sink. Task 10 supplies the real one."""


@dataclass
class WebhookDeps:
    """Everything the handler needs, injected so tests need no network or global state."""

    session_factory: Callable[[], AsyncSession]
    verify: Verify = verify_paypal_webhook
    publish: Publish = field(default=_no_publish)


# Webhook outcomes (returned by process_webhook; tests and logs use them).
REJECTED = "rejected"
VERIFICATION_UNAVAILABLE = "verification_unavailable"
DUPLICATE = "duplicate"
IGNORED = "ignored"
APPLIED = "applied"


@dataclass(frozen=True)
class _Plan:
    steps: list[str]  # statuses to move through, in order
    attempt_status: str  # what the matching provider attempt becomes
    operation: str | None  # the attempt's operation, if one should be updated


def _parse(raw_body: bytes) -> dict[str, Any] | None:
    try:
        event = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        return None
    return event if isinstance(event, dict) else None


def _short(value: Any, limit: int = 128) -> str:
    return value[:limit] if isinstance(value, str) and value else "UNKNOWN"


async def _store_unverified(
    deps: WebhookDeps, event: dict[str, Any] | None, raw_body: bytes, *, verify_ms: int, why: str
) -> None:
    """Keep a record of a delivery we could not trust. It never touches a payment.

    The id is GENERATED, never the id the sender claims: otherwise a forged event that
    reuses a real event id would take its place in the dedupe table and the genuine
    delivery would then be dropped as a "duplicate".
    """
    claimed = event.get("id") if event else None
    payload: dict[str, Any] = {"why": why, "size": len(raw_body)}
    if event is not None:
        payload["claimed_event_id"] = claimed if isinstance(claimed, str) else None
        payload["event"] = mask_payload(event)
    async with deps.session_factory() as session:
        await record_provider_event_once(
            session,
            event_id=f"unverified:{uuid.uuid4().hex}",
            event_type=_short(event.get("event_type") if event else None),
            verified=False,
            payload=payload,
            verify_ms=verify_ms,
        )
        await session.commit()


async def _find_payment(session: AsyncSession, event: dict[str, Any]) -> Payment | None:
    """Lock and return the payment this event belongs to, or None.

    By PayPal order id first; otherwise by the capture/authorization id that one of our
    own provider attempts stored. A payment found by resource id must also agree on the
    order id when the event carries one.
    """
    order_id = extract_order_id(event)
    resource_id = extract_resource_id(event)
    if order_id:
        payment = await session.scalar(
            select(Payment).where(Payment.paypal_order_id == order_id).with_for_update()
        )
        if payment is not None:
            return payment
    if resource_id:
        attempt_payment_ids = (
            await session.scalars(
                select(PaymentAttempt.payment_id).where(
                    PaymentAttempt.provider == "paypal",
                    PaymentAttempt.provider_operation_id == resource_id,
                )
            )
        ).all()
        if len(set(attempt_payment_ids)) == 1:
            payment = await session.scalar(
                select(Payment).where(Payment.id == attempt_payment_ids[0]).with_for_update()
            )
            if payment is not None and (order_id is None or payment.paypal_order_id == order_id):
                return payment
    return None


async def _unfinished_attempts(
    session: AsyncSession, payment_id: uuid.UUID, operation: str | None = None
) -> list[PaymentAttempt]:
    query = select(PaymentAttempt).where(
        PaymentAttempt.payment_id == payment_id,
        PaymentAttempt.status.in_(["STARTED", "UNCERTAIN"]),
    )
    if operation is not None:
        query = query.where(PaymentAttempt.operation == operation)
    return list((await session.scalars(query.order_by(PaymentAttempt.started_at))).all())


def _identity_problem(payment: Payment, event: dict[str, Any], action: str) -> str | None:
    """Why this event cannot be trusted to belong to this payment, or None if it can."""
    order_id = extract_order_id(event)
    if order_id is not None and order_id != payment.paypal_order_id:
        return "order_id_mismatch"
    amount = extract_amount(event)
    if amount is None:
        # Capture and authorization events always carry an amount. Never move money
        # state on an event that does not say how much.
        return None if action in {"ORDER_APPROVED", "AUTHORIZATION_VOIDED"} else "amount_missing"
    value, currency = amount
    if value != payment.amount or currency != payment.currency:
        return "amount_or_currency_mismatch"
    return None


async def _plan(session: AsyncSession, payment: Payment, action: str) -> tuple[_Plan | None, str]:
    """Work out the status steps for this event, or (None, reason) to ignore it."""
    path, status = payment.mode, payment.status

    if action == "ORDER_APPROVED":
        # Buyer approval. ORDER_CREATED covers "verify-approval was never called".
        if status == ORDER_CREATED:
            steps = [AWAITING_APPROVAL, APPROVED]
        elif status == AWAITING_APPROVAL:
            steps = [APPROVED]
        else:
            return None, "out_of_order_or_repeat"
        return _Plan(steps, "SUCCEEDED", None), ""

    if action == "AUTHORIZATION_CREATED" and path != CONDITIONALPAY:
        return None, "authorization_on_smartpay_payment"
    if action == "AUTHORIZATION_VOIDED" and path != CONDITIONALPAY:
        return None, "void_on_smartpay_payment"

    operation = ACTION_OPERATION[action]
    attempt_status = "FAILED" if action == "CAPTURE_DENIED" else "SUCCEEDED"

    if status == UNCERTAIN:
        # We lost the answer to one of our own calls: this event is that answer.
        unfinished = await _unfinished_attempts(session, payment.id)
        if len(unfinished) != 1:
            return None, "uncertain_operation_not_identified"
        pending = unfinished[0].operation
        if pending != operation:
            return None, "event_does_not_match_pending_operation"
        target = resolve_uncertain(path, pending, PROVIDER_STATUS[action])
        if target == UNCERTAIN:
            return None, "no_resolution_for_event"
        steps = [target]
        if target == AUTHORIZED:
            steps.append(AWAITING_CONDITION)
        return _Plan(steps, attempt_status, operation), ""

    table: dict[str, dict[str, list[str]]] = {
        "AUTHORIZATION_CREATED": {AUTHORIZE_REQUESTED: [AUTHORIZED, AWAITING_CONDITION]},
        "AUTHORIZATION_VOIDED": {VOID_REQUESTED: [VOIDED]},
        "CAPTURE_PENDING": {CAPTURE_REQUESTED: [CAPTURE_PENDING]},
        "CAPTURE_COMPLETED": {CAPTURE_REQUESTED: [CAPTURED], CAPTURE_PENDING: [CAPTURED]},
        "CAPTURE_DENIED": {CAPTURE_REQUESTED: [FAILED], CAPTURE_PENDING: [FAILED]},
    }
    steps = table[action].get(status)
    if steps is None:
        return None, "out_of_order_or_repeat"
    return _Plan(steps, attempt_status, operation), ""


def _validate(payment: Payment, steps: list[str], pending_operation: str | None) -> str | None:
    """Every step must be a legal state-machine move. Returns the reason if one is not."""
    current = payment.status
    for step in steps:
        ok, reason = validate_transition(
            payment.mode,
            current,
            step,
            pending_operation=pending_operation if current == UNCERTAIN else None,
        )
        if not ok:
            return reason
        current = step
    return None


async def _apply(
    session: AsyncSession,
    payment: Payment,
    plan: _Plan,
    *,
    event: dict[str, Any],
    event_id: str,
    event_type: str,
) -> str:
    """Apply the steps with compare-and-set and settle the matching provider attempt."""
    resource_id = extract_resource_id(event)
    evidence = {
        "confirmed": True,
        "source": "webhook",
        "event_id": event_id,
        "event_type": event_type,
        "resource_id": resource_id,
    }
    version, current = payment.version, payment.status
    for step in plan.steps:
        version = await transition_payment(
            session,
            payment_id=payment.id,
            expected_version=version,
            expected_status=current,
            new_status=step,
            actor=ACTOR,
            evidence=evidence,
        )
        current = step
    if plan.operation is not None:
        attempts = await _unfinished_attempts(session, payment.id, plan.operation)
        if len(attempts) == 1:
            # The provider answered our call: record it so later steps (for example a
            # conditional capture, which needs the authorization id) can rely on it.
            attempts[0].status = plan.attempt_status
            if resource_id and plan.attempt_status == "SUCCEEDED":
                attempts[0].provider_operation_id = resource_id
    return current


async def _publish(deps: WebhookDeps, intent_id: str, event: dict[str, Any]) -> None:
    try:
        result = deps.publish(intent_id, event)
        if inspect.isawaitable(result):
            await result
    except Exception:  # a broken notifier must never undo a committed payment fact
        log.exception("webhook notification failed for payment %s", intent_id)


async def process_webhook(headers: Mapping[str, str], raw_body: bytes, deps: WebhookDeps) -> str:
    """Process one webhook delivery. Returns the outcome (see the constants above)."""
    started = time.perf_counter()
    event = _parse(raw_body)
    try:
        verified = await deps.verify(headers, raw_body)
    except WebhookVerificationUnavailable:
        verified = None
    verify_ms = int((time.perf_counter() - started) * 1000)

    # 2. Not verified: record and stop. No payment audit row is written: the audit table
    # is append-only, so it must not accept writes from unauthenticated senders.
    if verified is not True:
        await _store_unverified(
            deps,
            event,
            raw_body,
            verify_ms=verify_ms,
            why="verification_unavailable" if verified is None else "signature_rejected",
        )
        if verified is None:
            log.warning("webhook not verified: PayPal verification unavailable")
            return VERIFICATION_UNAVAILABLE
        log.warning("webhook rejected: signature check failed")
        return REJECTED

    assert event is not None  # verification passed only for a JSON object
    event_type = event.get("event_type")
    event_id = event.get("id")
    masked = mask_payload(event)
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 128:
        # Signed but without a usable id: keep it, but it cannot be de-duplicated.
        event_id = f"noid:{uuid.uuid4().hex}"

    async with deps.session_factory() as session:
        try:
            # 3. Record once. The UNIQUE (provider, event_id) constraint is the dedupe.
            row_id = await record_provider_event_once(
                session,
                event_id=event_id,
                event_type=_short(event_type),
                verified=True,
                payload=masked,
                verify_ms=verify_ms,
            )
            payment = await _find_payment(session, event)
            if row_id is None:
                if payment is not None:
                    await add_audit_event(
                        session,
                        payment_id=payment.id,
                        event_type="DUPLICATE_SUPPRESSED",
                        actor=ACTOR,
                        evidence={"reason": "webhook_replay", "event_id": event_id},
                    )
                await session.commit()
                return DUPLICATE

            if payment is not None:
                await session.execute(
                    update(ProviderEvent)
                    .where(ProviderEvent.id == row_id)
                    .values(payment_id=payment.id)
                )

            outcome, reason, new_status = IGNORED, "", None
            action = EVENT_MAP.get(event_type) if isinstance(event_type, str) else None
            if action is None:
                reason = "unsupported_event_type"
            elif payment is None:
                reason = "no_matching_payment"
            else:
                problem = _identity_problem(payment, event, action)
                plan, plan_reason = (
                    (None, problem) if problem else await _plan(session, payment, action)
                )
                if plan is not None:
                    pending = plan.operation if payment.status == UNCERTAIN else None
                    problem = _validate(payment, plan.steps, pending)
                    if problem is not None:
                        plan, plan_reason = None, problem
                if plan is None:
                    reason = plan_reason or "ignored"
                else:
                    try:
                        new_status = await _apply(
                            session,
                            payment,
                            plan,
                            event=event,
                            event_id=event_id,
                            event_type=str(event_type),
                        )
                        outcome = APPLIED
                    except StalePaymentVersion:
                        # Another worker moved the payment first. Their state wins.
                        await session.rollback()
                        return await _record_stale(deps, event, event_id, str(event_type))

            if outcome == IGNORED and payment is not None:
                await add_audit_event(
                    session,
                    payment_id=payment.id,
                    event_type="WEBHOOK_IGNORED",
                    actor=ACTOR,
                    evidence={
                        "confirmed": False,
                        "source": "webhook",
                        "reason": reason,
                        "event_id": event_id,
                        "event_type": _short(event_type),
                        "payment_status": payment.status,
                    },
                )
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    if outcome == APPLIED and payment is not None:
        await _publish(
            deps,
            str(payment.id),
            {"event_id": event_id, "event_type": event_type, "status": new_status},
        )
    log.info("webhook %s: %s %s", outcome, event_type, reason)
    return outcome


async def _record_stale(
    deps: WebhookDeps, event: dict[str, Any], event_id: str, event_type: str
) -> str:
    """The first attempt rolled back (a concurrent update won). Keep the event as ignored."""
    async with deps.session_factory() as session:
        row_id = await record_provider_event_once(
            session,
            event_id=event_id,
            event_type=_short(event_type),
            verified=True,
            payload=mask_payload(event),
        )
        payment = await _find_payment(session, event)
        if payment is not None:
            await add_audit_event(
                session,
                payment_id=payment.id,
                event_type="WEBHOOK_IGNORED",
                actor=ACTOR,
                evidence={
                    "confirmed": False,
                    "source": "webhook",
                    "reason": "concurrent_payment_update",
                    "event_id": event_id,
                    "row_id": str(row_id) if row_id else None,
                },
            )
        await session.commit()
    return IGNORED