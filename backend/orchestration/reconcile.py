"""Conservative, read-only PayPal evidence gathering for unresolved Task 6 attempts.

Never POSTs or replays a payment. Provider reads alone do not mutate the ledger.
A separate reviewed reconciliation decision must use compare-and-swap plus audit.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config.settings import get_settings
from backend.models.attempt import PaymentAttempt
from backend.models.audit import PaymentAudit
from backend.models.payment import Payment
from backend.models.reconciliation import PaymentReconciliation
from backend.models.repository import transition_payment
from backend.payments.paypal_client import PayPalError, PayPalNotSent, PayPalOutcomeUnknown
from backend.state_machine.states import FINAL_STATUSES, UNCERTAIN
from backend.state_machine.transitions import resolve_uncertain, validate_transition

UNRESOLVED = frozenset({"STARTED", "UNCERTAIN", "NOT_SENT", "NOT_OCCURRED", "PENDING"})
MAX_RECONCILIATION_CHECKS = 5


@dataclass(frozen=True)
class ReconciliationObservation:
    payment_id: str
    attempt_id: str
    operation: str
    request_id: str
    attempt_status: str
    payment_status: str
    provider_status: str | None
    provider_resource_id: str | None
    decision: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _one_resource(order: dict[str, Any], kind: str) -> dict[str, Any] | None:
    units = order.get("purchase_units")
    if not isinstance(units, list) or len(units) != 1:
        return None
    unit = units[0]
    if not isinstance(unit, dict):
        return None
    payments = unit.get("payments")
    if not isinstance(payments, dict):
        return None
    resources = payments.get(kind)
    if not isinstance(resources, list) or len(resources) != 1:
        return None
    resource = resources[0]
    if not isinstance(resource, dict):
        return None
    return resource


def classify_provider_observation(
    *,
    operation: str,
    payment_mode: str,
    order: dict[str, Any] | None,
    authorization: dict[str, Any] | None = None,
) -> tuple[str | None, str | None, str, str]:
    """Classify *positive* provider evidence; lack of evidence means manual review.

    A completed order by itself does not prove a particular capture/authorization.
    Never infer nonexecution from a 404, APPROVED or absent nested resource.
    """
    if operation == "create_order":
        if order and order.get("status") in {
            "CREATED",
            "PAYER_ACTION_REQUIRED",
            "APPROVED",
            "COMPLETED",
        }:
            return (
                str(order["status"]),
                str(order["id"]),
                "EVIDENCE_FOUND",
                "Stored order exists at PayPal",
            )
        return None, None, "MANUAL_REVIEW", "Order creation has no definitive lookup evidence"

    if operation == "authorize":
        resource = _one_resource(order or {}, "authorizations")
        if resource and resource.get("status") in {
            "CREATED",
            "CAPTURED",
            "PARTIALLY_CAPTURED",
            "VOIDED",
        }:
            rid = resource.get("id")
            if isinstance(rid, str) and rid:
                return (
                    str(resource["status"]),
                    rid,
                    "EVIDENCE_FOUND",
                    "Single authorization linked to stored order",
                )
        return (
            None,
            None,
            "MANUAL_REVIEW",
            "Authorization result cannot be independently established",
        )

    if operation == "capture":
        resource = _one_resource(order or {}, "captures")
        if resource and resource.get("status") in {"COMPLETED", "PENDING", "DECLINED", "DENIED"}:
            rid = resource.get("id")
            if isinstance(rid, str) and rid:
                return (
                    str(resource["status"]),
                    rid,
                    "EVIDENCE_FOUND",
                    "Single capture linked to stored order",
                )
        return None, None, "MANUAL_REVIEW", "Capture outcome not proven by GET order"

    if operation == "void" and payment_mode == "CONDITIONALPAY":
        if authorization and authorization.get("status") == "VOIDED":
            rid = authorization.get("id")
            if isinstance(rid, str) and rid:
                return "VOIDED", rid, "EVIDENCE_FOUND", "Authorization is confirmed voided"
        return None, None, "MANUAL_REVIEW", "Void outcome has no definitive provider evidence"

    return None, None, "MANUAL_REVIEW", "Unsupported observation"


async def inspect_provider_evidence(
    session: AsyncSession,
    paypal: Any,
    *,
    payment_id: uuid.UUID,
) -> list[ReconciliationObservation]:
    """Perform safe GETs, never payment POSTs or database writes.

    Designed for offline/admin inspection. Follow-up state updates require an
    independently reviewed reconciliation policy, CAS transition and audit.
    """
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise ValueError("Payment does not exist")
    rows = (
        (
            await session.execute(
                select(PaymentAttempt)
                .where(
                    PaymentAttempt.payment_id == payment_id, PaymentAttempt.status.in_(UNRESOLVED)
                )
                .order_by(PaymentAttempt.started_at, PaymentAttempt.id)
            )
        )
        .scalars()
        .all()
    )

    order: dict[str, Any] | None = None
    lookup_issue = None
    if payment.paypal_order_id:
        try:
            order = await paypal.get_order(payment.paypal_order_id)
            if not isinstance(order, dict) or order.get("id") != payment.paypal_order_id:
                order = None
                lookup_issue = "Order ID mismatch or malformed PayPal response"
        except (PayPalError, PayPalNotSent, PayPalOutcomeUnknown):
            lookup_issue = "PayPal order lookup failed; outcome unknown"
    else:
        lookup_issue = "No stored PayPal order ID; request ID is not a lookup key"

    observations: list[ReconciliationObservation] = []
    for attempt in rows:
        auth = None
        if attempt.operation == "void":
            authorization_row = await session.scalar(
                select(PaymentAttempt).where(
                    PaymentAttempt.payment_id == payment_id,
                    PaymentAttempt.operation == "authorize",
                    PaymentAttempt.status == "SUCCEEDED",
                )
            )
            if authorization_row and authorization_row.provider_operation_id:
                try:
                    auth = await paypal.get_authorization(authorization_row.provider_operation_id)
                    if (
                        not isinstance(auth, dict)
                        or auth.get("id") != authorization_row.provider_operation_id
                    ):
                        auth = None
                except (PayPalError, PayPalNotSent, PayPalOutcomeUnknown):
                    auth = None
        status, rid, decision, reason = classify_provider_observation(
            operation=attempt.operation,
            payment_mode=payment.mode,
            order=order,
            authorization=auth,
        )
        if decision != "EVIDENCE_FOUND" and lookup_issue:
            reason = lookup_issue + "; " + reason
        observations.append(
            ReconciliationObservation(
                payment_id=str(payment.id),
                attempt_id=str(attempt.id),
                operation=attempt.operation,
                request_id=attempt.request_id,
                attempt_status=attempt.status,
                payment_status=payment.status,
                provider_status=status,
                provider_resource_id=rid,
                decision=decision,
                reason=reason,
            )
        )
    return observations


def _amount_matches(resource: dict[str, Any], payment: Payment) -> bool:
    amount = resource.get("amount")
    if not isinstance(amount, dict):
        return False
    value, currency = amount.get("value"), amount.get("currency_code")
    if not isinstance(value, str) or not isinstance(currency, str):
        return False
    try:
        return Decimal(value) == payment.amount and currency.upper() == payment.currency
    except (InvalidOperation, ValueError):
        return False


async def _fetch_operation_evidence(
    session: AsyncSession,
    paypal: Any,
    payment: Payment,
    attempt: PaymentAttempt,
) -> tuple[str | None, str | None, str, str]:
    """Fetch and validate one operation-specific PayPal result; absence is inconclusive."""
    try:
        if attempt.operation == "void":
            auth_rows = (
                await session.scalars(
                    select(PaymentAttempt).where(
                        PaymentAttempt.payment_id == payment.id,
                        PaymentAttempt.operation == "authorize",
                        PaymentAttempt.status == "SUCCEEDED",
                        PaymentAttempt.provider_operation_id.is_not(None),
                    )
                )
            ).all()
            if len(auth_rows) != 1:
                return None, None, "MANUAL_REVIEW", "No unique confirmed authorization ID"
            authorization_id = auth_rows[0].provider_operation_id
            authorization = await paypal.get_authorization(authorization_id)
            if (
                not isinstance(authorization, dict)
                or authorization.get("id") != authorization_id
                or not _amount_matches(authorization, payment)
            ):
                return None, None, "MANUAL_REVIEW", "Authorization identity or amount mismatch"
            status = authorization.get("status")
            if status == "VOIDED":
                return "VOIDED", authorization_id, "EVIDENCE_FOUND", "Void confirmed by PayPal"
            if status == "CREATED":
                return (
                    "CREATED",
                    authorization_id,
                    "NOT_OCCURRED",
                    "PayPal confirms the authorization remains active",
                )
            return None, None, "MANUAL_REVIEW", "Authorization status is not conclusive for void"

        order_id = payment.paypal_order_id
        if attempt.operation == "create_order" and not order_id:
            order_id = attempt.provider_operation_id
        if not order_id:
            return None, None, "MANUAL_REVIEW", "No persisted PayPal order ID to query"
        order = await paypal.get_order(order_id)
        if not isinstance(order, dict) or order.get("id") != order_id:
            return None, None, "MANUAL_REVIEW", "PayPal order ID mismatch or malformed response"

        if attempt.operation == "create_order":
            units = order.get("purchase_units")
            if (
                order.get("status") not in {"CREATED", "PAYER_ACTION_REQUIRED", "APPROVED"}
                or not isinstance(units, list)
                or len(units) != 1
                or not isinstance(units[0], dict)
                or not _amount_matches(units[0], payment)
            ):
                return None, None, "MANUAL_REVIEW", "Order creation is not independently confirmed"
            return str(order["status"]), order_id, "EVIDENCE_FOUND", "PayPal order is confirmed"

        if attempt.operation == "authorize":
            resource = _one_resource(order, "authorizations")
            if (
                resource is None
                or resource.get("status") != "CREATED"
                or not isinstance(resource.get("id"), str)
                or not resource["id"]
                or not _amount_matches(resource, payment)
            ):
                return None, None, "MANUAL_REVIEW", "Authorization is not uniquely confirmed"
            return (
                "AUTHORIZED",
                resource["id"],
                "EVIDENCE_FOUND",
                "Authorization confirmed by PayPal",
            )

        if attempt.operation == "capture":
            if attempt.provider_operation_id:
                capture = await paypal.get_capture(attempt.provider_operation_id)
                if not isinstance(capture, dict):
                    return None, None, "MANUAL_REVIEW", "Capture response is malformed"
                related = capture.get("supplementary_data")
                related_ids = related.get("related_ids") if isinstance(related, dict) else None
                if (
                    capture.get("id") != attempt.provider_operation_id
                    or not isinstance(related_ids, dict)
                    or related_ids.get("order_id") != order_id
                    or not _amount_matches(capture, payment)
                ):
                    return None, None, "MANUAL_REVIEW", "Capture identity or amount mismatch"
                status = capture.get("status")
                if status in {"COMPLETED", "PENDING", "DECLINED", "DENIED"}:
                    return (
                        str(status),
                        str(capture["id"]),
                        "EVIDENCE_FOUND",
                        "Capture confirmed by ID",
                    )
                return None, None, "MANUAL_REVIEW", "Capture status is inconclusive"

            resource = _one_resource(order, "captures")
            if (
                resource is None
                or not isinstance(resource.get("id"), str)
                or not resource["id"]
                or not _amount_matches(resource, payment)
            ):
                return None, None, "MANUAL_REVIEW", "Capture is not uniquely confirmed"
            status = resource.get("status")
            if status in {"COMPLETED", "PENDING", "DECLINED", "DENIED"}:
                return (
                    str(status),
                    str(resource["id"]),
                    "EVIDENCE_FOUND",
                    "Capture confirmed by order",
                )
            return None, None, "MANUAL_REVIEW", "Capture status is inconclusive"
    except (PayPalError, PayPalNotSent, PayPalOutcomeUnknown):
        return None, None, "MANUAL_REVIEW", "PayPal lookup unavailable; outcome remains unknown"

    return None, None, "MANUAL_REVIEW", "Unsupported operation"


def _observation(
    payment: Payment,
    attempt: PaymentAttempt,
    decision: str,
    reason: str,
    provider_status: str | None = None,
    provider_resource_id: str | None = None,
) -> ReconciliationObservation:
    return ReconciliationObservation(
        payment_id=str(payment.id),
        attempt_id=str(attempt.id),
        operation=attempt.operation,
        request_id=attempt.request_id,
        attempt_status=attempt.status,
        payment_status=payment.status,
        provider_status=provider_status,
        provider_resource_id=provider_resource_id,
        decision=decision,
        reason=reason,
    )


async def reconcile_attempt(
    session: AsyncSession,
    paypal: Any,
    *,
    attempt_id: uuid.UUID,
    force: bool = False,
    now: datetime | None = None,
) -> ReconciliationObservation:
    """Run one bounded PayPal lookup and atomically persist evidence or the next due check."""
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=UTC)
    run = await session.scalar(
        select(PaymentReconciliation)
        .where(PaymentReconciliation.attempt_id == attempt_id)
        .with_for_update()
    )
    attempt = await session.get(PaymentAttempt, attempt_id)
    if attempt is None:
        raise ValueError("Payment attempt does not exist")
    payment = await session.get(Payment, attempt.payment_id)
    if payment is None:
        raise ValueError("Payment does not exist")
    if attempt.status not in {"STARTED", "UNCERTAIN", "PENDING"}:
        if run is not None and run.completed_at is None:
            run.completed_at = current_time
            run.last_reason = "Attempt was resolved by another trusted operation"
            await session.commit()
        return _observation(payment, attempt, "NOT_RECONCILABLE", "Attempt is not unresolved")

    if run is None:
        run = PaymentReconciliation(
            attempt_id=attempt.id,
            payment_id=payment.id,
            check_count=0,
            next_check_at=current_time,
        )
        session.add(run)
        await session.flush()
    if run.completed_at is not None:
        return _observation(
            payment,
            attempt,
            "EXHAUSTED",
            run.last_reason or "Check schedule complete",
        )
    if not force and run.next_check_at > current_time:
        return _observation(payment, attempt, "WAITING", "Next check is not due")
    if run.check_count >= MAX_RECONCILIATION_CHECKS:
        run.completed_at = current_time
        run.last_reason = "Five provider checks completed without a conclusive outcome"
        await session.commit()
        return _observation(payment, attempt, "EXHAUSTED", run.last_reason)

    snapshot_version = payment.version
    snapshot_attempt_status = attempt.status
    provider_status, resource_id, decision, reason = await _fetch_operation_evidence(
        session, paypal, payment, attempt
    )

    locked_payment = await session.scalar(
        select(Payment)
        .where(Payment.id == payment.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    locked_attempt = await session.scalar(
        select(PaymentAttempt)
        .where(PaymentAttempt.id == attempt.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_payment is None or locked_attempt is None:
        raise ValueError("Payment or attempt disappeared during reconciliation")

    run.check_count += 1
    run.last_reason = reason[:160]
    delays = get_settings().reconciliation_delays_seconds

    def finish_check(*, complete: bool) -> None:
        if complete or run.check_count >= MAX_RECONCILIATION_CHECKS:
            run.completed_at = current_time
        else:
            run.next_check_at = current_time + timedelta(seconds=delays[run.check_count])

    if (
        locked_payment.version != snapshot_version
        or locked_attempt.status != snapshot_attempt_status
    ):
        finish_check(complete=True)
        run.last_reason = "Payment or attempt changed while the PayPal lookup was in flight"
        await session.commit()
        return _observation(
            locked_payment,
            locked_attempt,
            "STALE_EVIDENCE",
            run.last_reason,
            provider_status,
            resource_id,
        )

    if locked_payment.status in FINAL_STATUSES:
        finish_check(complete=True)
        run.last_reason = "Payment is already in a final state"
        await session.commit()
        return _observation(
            locked_payment,
            locked_attempt,
            "FINAL_STATE",
            run.last_reason,
            provider_status,
            resource_id,
        )

    if decision == "NOT_OCCURRED":
        target_status = resolve_uncertain(
            locked_payment.mode, locked_attempt.operation, "AUTHORIZED"
        )
        current = locked_payment.status
        steps = [target_status]
        if current == "VOID_REQUESTED" and target_status == "AWAITING_CONDITION":
            steps.insert(0, UNCERTAIN)
        for step in steps:
            if current == step:
                continue
            allowed, transition_reason = validate_transition(
                locked_payment.mode,
                current,
                step,
                pending_operation=locked_attempt.operation if current == UNCERTAIN else None,
            )
            if not allowed:
                finish_check(complete=False)
                run.last_reason = transition_reason
                await session.commit()
                return _observation(
                    locked_payment,
                    locked_attempt,
                    "CONFLICT",
                    transition_reason,
                    provider_status,
                    resource_id,
                )
            version = await transition_payment(
                session,
                payment_id=locked_payment.id,
                expected_version=locked_payment.version,
                expected_status=current,
                new_status=step,
                actor="reconciliation",
                evidence={
                    "source": "paypal_get",
                    "confirmed": True,
                    "operation": locked_attempt.operation,
                    "request_id": locked_attempt.request_id,
                    "provider_status": provider_status,
                    "provider_resource_id": resource_id,
                    "reason": reason,
                },
            )
            locked_payment.version = version
            locked_payment.status = step
            current = step
        locked_attempt.status = "NOT_OCCURRED"
        finish_check(complete=True)
        session.add(
            PaymentAudit(
                payment_id=locked_payment.id,
                event_type="PROVIDER_EVIDENCE_CONFIRMED",
                actor="reconciliation",
                evidence={
                    "source": "paypal_get",
                    "confirmed": True,
                    "decision": decision,
                    "operation": locked_attempt.operation,
                    "request_id": locked_attempt.request_id,
                    "provider_status": provider_status,
                    "reason": reason,
                },
            )
        )
        await session.commit()
        return _observation(
            locked_payment,
            locked_attempt,
            decision,
            reason,
            provider_status,
            resource_id,
        )

    if decision != "EVIDENCE_FOUND" or provider_status is None:
        finish_check(complete=False)
        session.add(
            PaymentAudit(
                payment_id=locked_payment.id,
                event_type="RECONCILIATION_CHECK",
                actor="reconciliation",
                evidence={
                    "source": "paypal_get",
                    "confirmed": False,
                    "operation": locked_attempt.operation,
                    "request_id": locked_attempt.request_id,
                    "check_number": run.check_count,
                    "reason": reason,
                },
            )
        )
        await session.commit()
        outcome = "EXHAUSTED" if run.completed_at else "UNKNOWN"
        return _observation(
            locked_payment, locked_attempt, outcome, reason, provider_status, resource_id
        )

    target_status = resolve_uncertain(
        locked_payment.mode, locked_attempt.operation, provider_status
    )
    if target_status == UNCERTAIN:
        finish_check(complete=False)
        run.last_reason = "PayPal status has no safe state-machine resolution"
        await session.commit()
        return _observation(
            locked_payment,
            locked_attempt,
            "UNKNOWN",
            run.last_reason,
            provider_status,
            resource_id,
        )

    steps = [target_status]
    if locked_attempt.operation == "authorize" and target_status == "AUTHORIZED":
        steps.append("AWAITING_CONDITION")
    current = locked_payment.status
    for step in steps:
        if current == step:
            continue
        allowed, transition_reason = validate_transition(
            locked_payment.mode,
            current,
            step,
            pending_operation=locked_attempt.operation if current == UNCERTAIN else None,
        )
        if not allowed:
            finish_check(complete=False)
            run.last_reason = transition_reason
            await session.commit()
            return _observation(
                locked_payment,
                locked_attempt,
                "CONFLICT",
                transition_reason,
                provider_status,
                resource_id,
            )
        version = await transition_payment(
            session,
            payment_id=locked_payment.id,
            expected_version=locked_payment.version,
            expected_status=current,
            new_status=step,
            actor="reconciliation",
            evidence={
                "source": "paypal_get",
                "confirmed": True,
                "attempt_id": str(locked_attempt.id),
                "operation": locked_attempt.operation,
                "request_id": locked_attempt.request_id,
                "provider_status": provider_status,
                "provider_resource_id": resource_id,
            },
        )
        locked_payment.version = version
        locked_payment.status = step
        current = step

    if locked_attempt.operation == "create_order":
        locked_payment.paypal_order_id = resource_id
    if locked_attempt.operation != "void" and resource_id:
        locked_attempt.provider_operation_id = resource_id
    if target_status == "CAPTURE_PENDING":
        locked_attempt.status = "PENDING"
        finish_check(complete=False)
    else:
        locked_attempt.status = "FAILED" if target_status == "FAILED" else "SUCCEEDED"
        finish_check(complete=True)
    session.add(
        PaymentAudit(
            payment_id=locked_payment.id,
            event_type="PROVIDER_EVIDENCE_CONFIRMED",
            actor="reconciliation",
            evidence={
                "source": "paypal_get",
                "confirmed": True,
                "operation": locked_attempt.operation,
                "request_id": locked_attempt.request_id,
                "provider_status": provider_status,
                "provider_resource_id": resource_id,
            },
        )
    )
    await session.commit()
    return _observation(
        locked_payment,
        locked_attempt,
        "EVIDENCE_APPLIED",
        reason,
        provider_status,
        resource_id,
    )


async def process_due_reconciliations(
    session: AsyncSession,
    paypal: Any,
    *,
    limit: int = 50,
    now: datetime | None = None,
) -> list[ReconciliationObservation]:
    """Run one nonblocking batch of due checks; a scheduler may invoke this periodically."""
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=UTC)
    attempt_ids = list(
        await session.scalars(
            select(PaymentReconciliation.attempt_id)
            .where(
                PaymentReconciliation.completed_at.is_(None),
                PaymentReconciliation.next_check_at <= current_time,
                PaymentReconciliation.check_count < MAX_RECONCILIATION_CHECKS,
            )
            .order_by(PaymentReconciliation.next_check_at, PaymentReconciliation.attempt_id)
            .limit(limit)
        )
    )
    return [
        await reconcile_attempt(session, paypal, attempt_id=attempt_id, now=current_time)
        for attempt_id in attempt_ids
    ]
