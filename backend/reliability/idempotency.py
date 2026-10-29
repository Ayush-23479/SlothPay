"""Duplicate protection for the pay click (Task 5): the Rs 500 double-tap fix.

Layer 1, our own check:
  * the idempotency key: a repeat returns the existing payment instead of making a new one,
    and reusing a key with a different request (amount, currency, path, order id or
    condition) is rejected;
  * a short Redis lock so two clicks at the same moment cannot both pass;
  * the merchant order id: the same order id is the same purchase, a different order id is a
    separate purchase;
  * a similar-payment guard for callers that do NOT say which order this is (same buyer,
    amount and currency within 2 minutes while the first is still open);
  * the database UNIQUE (owner, key) and (owner, order id) constraints as the final backstop.
Layer 2, PayPal: a stable PayPal-Request-Id derived from the payment id and the operation
(derive_request_id), so repeating the same request returns PayPal's original result.

Money safety: if Redis is unreachable, register_intent raises instead of creating a payment
without the lock (fail closed).
"""

from __future__ import annotations

import json
import re
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from redis.exceptions import WatchError
from sqlalchemy.exc import IntegrityError

from backend.config.settings import get_settings
from backend.models.repository import (
    add_audit_event,
    create_intent,
    find_recent_intents,
    get_intent_by_key,
    get_intent_by_order_id,
    get_intent_created_evidence,
)

# Fixed forever: changing it would change every derived PayPal-Request-Id, which breaks
# PayPal's idempotency for payments that are still in flight.
REQUEST_ID_NAMESPACE = uuid.UUID("5b0d6c1e-3f4a-5c7e-9a21-7d8e4f6b2a10")

# Must match the database CHECK on payments.mode. "SMART"/"CONDITIONAL" are accepted as
# aliases and stored as SMARTPAY/CONDITIONALPAY.
PATHS = ("SMARTPAY", "CONDITIONALPAY")
_PATH_ALIASES = {"SMART": "SMARTPAY", "CONDITIONAL": "CONDITIONALPAY"}

# Statuses with no way out (Task 3: captured, voided, failed, cancelled).
FINAL_STATUSES = frozenset({"CAPTURED", "VOIDED", "FAILED", "CANCELLED"})

LOCK_TTL_SECONDS = 30
SIMILAR_WINDOW_SECONDS = 120
KEY_MIN_LENGTH = 8
KEY_MAX_LENGTH = 128
MAX_DESCRIPTION_LENGTH = 200
_MAX_AMOUNT = Decimal(10) ** 16  # payments.amount is NUMERIC(18, 2)
_AMOUNT_TEXT = re.compile(r"\d{1,16}(\.\d{1,2})?")
_TWO_PLACES = Decimal("0.01")


class PayloadMismatch(Exception):
    """The idempotency key (or order id) was already used with a different request."""


class IntentBusy(Exception):
    """Another request with the same key (or a same-amount twin) is being processed now."""


# --- Layer 2: stable PayPal request id ---------------------------------------------------


def derive_request_id(intent_id: str, operation: str) -> str:
    """The PayPal-Request-Id for one operation on one payment. Same inputs, same id."""
    return str(uuid.uuid5(REQUEST_ID_NAMESPACE, f"{intent_id}:{operation}"))


# --- Redis lock --------------------------------------------------------------------------


def lock_name(owner_id: str, key: str) -> str:
    return f"slothpay:lock:{owner_id}:{key}"


async def _release_if_owner(redis: Any, name: str, token: str) -> None:
    """Delete the lock only if it still holds our token (compare, then delete)."""
    async with redis.pipeline(transaction=True) as pipe:
        try:
            await pipe.watch(name)
            current = await pipe.get(name)
            if isinstance(current, bytes):
                current = current.decode()
            if current == token:
                pipe.multi()
                pipe.delete(name)
                await pipe.execute()
            else:
                await pipe.unwatch()
        except WatchError:
            # The key changed while we looked: it expired or someone else took it. Not ours.
            pass


@asynccontextmanager
async def intent_lock(
    redis: Any, owner_id: str, key: str, ttl_seconds: int = LOCK_TTL_SECONDS
) -> AsyncIterator[str]:
    """Short Redis lock: SET NX with an expiry and a random token.

    Raises IntentBusy at once if the lock is already held. Release only deletes the key if
    the token still matches, so a slow holder can never free someone else's lock.
    """
    name = lock_name(owner_id, key)
    token = secrets.token_hex(16)
    acquired = await redis.set(name, token, nx=True, ex=ttl_seconds)
    if not acquired:
        raise IntentBusy("Another request for this payment is in progress")
    try:
        yield token
    finally:
        await _release_if_owner(redis, name, token)


# --- validation --------------------------------------------------------------------------


def _canonical(value: Any) -> str:
    """Stable JSON text, so two equal conditions compare equal whatever their key order."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _validate_amount(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        raise ValueError("Amount must be a number or a numeric string")
    if isinstance(value, str):
        if not _AMOUNT_TEXT.fullmatch(value):
            raise ValueError("Amount must be a positive number with at most 2 decimal places")
        amount = Decimal(value)
    else:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("Amount must be a finite number")
    if amount <= 0:
        raise ValueError("Amount must be greater than 0")
    if amount.as_tuple().exponent < -2:
        raise ValueError("Amount can have at most 2 decimal places")
    if amount >= _MAX_AMOUNT:
        raise ValueError("Amount is too large")
    return amount.quantize(_TWO_PLACES)


def _validate_currency(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Currency must be a string")
    code = value.upper()
    if code not in get_settings().currency_allowlist:
        raise ValueError("Currency is not allowed")
    return code


def _validate_path(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Path must be a string")
    path = value.upper()
    path = _PATH_ALIASES.get(path, path)
    if path not in PATHS:
        raise ValueError("Path must be SMARTPAY or CONDITIONALPAY")
    return path


def _validate_request(owner_id: Any, idempotency_key: Any, payload: Any) -> dict[str, Any]:
    if not isinstance(owner_id, str) or not 1 <= len(owner_id) <= 128:
        raise ValueError("owner_id must be 1 to 128 characters")
    if not isinstance(idempotency_key, str) or not (
        KEY_MIN_LENGTH <= len(idempotency_key) <= KEY_MAX_LENGTH
    ):
        raise ValueError(
            f"idempotency_key must be {KEY_MIN_LENGTH} to {KEY_MAX_LENGTH} characters"
        )
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    description = payload.get("description")
    if description is not None and (
        not isinstance(description, str) or len(description) > MAX_DESCRIPTION_LENGTH
    ):
        raise ValueError(f"description must be text up to {MAX_DESCRIPTION_LENGTH} characters")
    condition = payload.get("condition")
    condition_json = None
    if condition is not None:
        if not isinstance(condition, dict):
            raise ValueError("condition must be a dict")
        try:
            condition_json = _canonical(condition)
        except (TypeError, ValueError) as exc:
            raise ValueError("condition must be plain JSON data") from exc
    explicit_order_id = payload.get("merchant_order_id")
    if explicit_order_id is not None and (
        not isinstance(explicit_order_id, str) or not 1 <= len(explicit_order_id) <= 128
    ):
        raise ValueError("merchant_order_id must be 1 to 128 characters")
    return {
        "owner_id": owner_id,
        "idempotency_key": idempotency_key,
        # No order id given: the key doubles as the order id (one purchase per key).
        "merchant_order_id": explicit_order_id if explicit_order_id is not None
        else idempotency_key,
        "explicit_order_id": explicit_order_id is not None,
        "path": _validate_path(payload.get("path")),
        "amount": _validate_amount(payload.get("amount")),
        "currency": _validate_currency(payload.get("currency")),
        "description": description,
        "condition": condition,
        "condition_json": condition_json,
    }


# --- Layer 1 -----------------------------------------------------------------------------


async def record_replay(session: Any, intent_id: str, reason: str = "same_key") -> None:
    """Audit a suppressed duplicate (reason 'same_key' or 'same_order'). Caller commits."""
    await add_audit_event(
        session,
        payment_id=intent_id,
        event_type="DUPLICATE_SUPPRESSED",
        actor="api",
        evidence={"source": "api", "confirmed": False, "reason": reason},
    )


async def similar_open_intent(
    session: Any,
    owner_id: str,
    amount: Any,
    currency: str,
    within_seconds: int = SIMILAR_WINDOW_SECONDS,
) -> dict[str, Any] | None:
    """Newest payment with the same owner, amount and currency, created within the window,
    whose status is not final. None if there is no such payment."""
    since = datetime.now(UTC) - timedelta(seconds=within_seconds)
    rows = await find_recent_intents(
        session,
        owner_id=owner_id,
        amount=_validate_amount(amount),
        currency=currency,
        since=since,
    )

    def created(row: dict[str, Any]) -> datetime:
        stamp = row["created_at"]
        return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)

    for row in sorted(rows, key=created, reverse=True):
        if row["status"] in FINAL_STATUSES or created(row) < since:
            continue
        return row
    return None


async def _same_request(session: Any, existing: dict[str, Any], request: dict[str, Any]) -> bool:
    """Is this request identical to the one that created `existing`?

    Compares amount, currency, path, merchant order id and the ConditionalPay condition
    (read back from the append-only INTENT_CREATED audit event). The free-text description
    is not compared: it is display text, not part of what is being paid for.
    """
    if (
        Decimal(existing["amount"]) != request["amount"]
        or existing["currency"] != request["currency"]
        or existing["path"] != request["path"]
        or existing["merchant_order_id"] != request["merchant_order_id"]
    ):
        return False
    evidence = await get_intent_created_evidence(session, existing["id"]) or {}
    stored = evidence.get("condition")
    stored_json = None if stored is None else _canonical(stored)
    return stored_json == request["condition_json"]


async def _replay_existing(
    session: Any, existing: dict[str, Any], request: dict[str, Any], reason: str
) -> tuple[dict[str, Any], bool]:
    if not await _same_request(session, existing, request):
        raise PayloadMismatch(
            "This idempotency key or order id was already used with a different amount, "
            "currency, path, order id or condition"
        )
    await record_replay(session, existing["id"], reason)
    await session.commit()
    return existing, True


async def _create_new(session: Any, request: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    owner_id = request["owner_id"]
    try:
        async with session.begin_nested():
            intent = await create_intent(
                session,
                owner_id=owner_id,
                idempotency_key=request["idempotency_key"],
                merchant_order_id=request["merchant_order_id"],
                path=request["path"],
                amount=request["amount"],
                currency=request["currency"],
            )
    except IntegrityError:
        # Another process inserted the same key or order id first: treat as a replay.
        existing = await get_intent_by_key(session, owner_id, request["idempotency_key"])
        reason = "same_key"
        if existing is None:
            existing = await get_intent_by_order_id(
                session, owner_id, request["merchant_order_id"]
            )
            reason = "same_order"
        if existing is None:
            raise
        return await _replay_existing(session, existing, request, reason)

    await add_audit_event(
        session,
        payment_id=intent["id"],
        event_type="INTENT_CREATED",
        actor="api",
        evidence={
            "source": "api",
            "confirmed": False,
            "path": request["path"],
            "amount": str(request["amount"]),
            "currency": request["currency"],
            "description": request["description"],
            "condition": request["condition"],
        },
        to_state=intent["status"],
    )
    await session.commit()
    return intent, False


async def register_intent(
    session: Any,
    redis: Any,
    owner_id: str,
    idempotency_key: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Create the payment for a pay click, or return the existing one.

    Returns (intent, replay). replay is True when no new payment was created.
      * Same key as an earlier request: that payment is returned, unless the request differs
        (amount, currency, path, merchant_order_id or condition), which raises PayloadMismatch.
      * payload["merchant_order_id"] given: it says which purchase this is. The same order
        id returns the existing payment (or PayloadMismatch if it differs); a different
        order id is a separate purchase and is never suppressed.
      * No merchant_order_id: a similar open payment (same buyer, amount and currency within
        2 minutes) is returned instead of creating a second one.
    Raises ValueError for bad input and IntentBusy when another request holds the lock.
    The session is committed before the lock is released.
    """
    request = _validate_request(owner_id, idempotency_key, payload)
    async with intent_lock(redis, owner_id, idempotency_key):
        try:
            existing = await get_intent_by_key(session, owner_id, idempotency_key)
            if existing is not None:
                return await _replay_existing(session, existing, request, "same_key")

            if request["explicit_order_id"]:
                same_order = await get_intent_by_order_id(
                    session, owner_id, request["merchant_order_id"]
                )
                if same_order is not None:
                    return await _replay_existing(session, same_order, request, "same_order")
                # The caller said which purchase this is, so a same-amount payment with a
                # different order id is a separate purchase: no similar-payment guard.
                return await _create_new(session, request)

            # No order id: a same-amount twin with a different key must not slip through at
            # the same moment either, so a second short lock covers owner + amount + currency.
            twin_key = f"similar:{request['amount']}:{request['currency']}"
            async with intent_lock(redis, owner_id, twin_key):
                similar = await similar_open_intent(
                    session, owner_id, request["amount"], request["currency"]
                )
                if similar is not None:
                    await add_audit_event(
                        session,
                        payment_id=similar["id"],
                        event_type="DUPLICATE_SUPPRESSED",
                        actor="api",
                        evidence={
                            "source": "api",
                            "confirmed": False,
                            "reason": "similar_intent",
                        },
                    )
                    await session.commit()
                    return similar, True
                return await _create_new(session, request)
        except Exception:
            await session.rollback()
            raise