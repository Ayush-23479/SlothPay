"""Task 18 signed condition webhook, reusing ConditionalPay's guarded resolution path.

An external service must be explicitly trusted and hold CONDITION_WEBHOOK_SECRET.
The signing format is custom SlothPay HMAC, not Zapier's own native signature.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

from backend.api.deps import Api, ApiError, Caller, PaymentsUnavailable, not_found, translate_errors
from backend.api.security import OWNER_PATTERN
from backend.config.settings import get_settings
from backend.extensions.replay_guard import PROCESSED_TTL, ReplayGuard, ReplayRejected
from backend.extensions.signed_conditions import InvalidConditionEvent
from backend.extensions.verifier_interface import SignedWebhookVerifier
from backend.notifications.hub import get_redis

log = logging.getLogger("slothpay.extensions")
router = APIRouter(prefix="/api/extensions", tags=["extensions"])


def get_extension_redis() -> Redis:
    return get_redis()


@router.post("/zapier/verify")
async def verify_zapier_condition(
    request: Request,
    api: Api,
    redis: Annotated[Redis, Depends(get_extension_redis)],
    x_slothpay_timestamp: Annotated[str | None, Header()] = None,
    x_slothpay_event_id: Annotated[str | None, Header()] = None,
    x_slothpay_signature: Annotated[str | None, Header()] = None,
) -> dict[str, str | bool]:
    """Validate, deduplicate, and resolve one signed external condition event.

    Fail closed: ambiguous failures retain the payment reservation for seven days.
    A new signed event for that payment cannot resolve it again during this period.
    """
    secret = get_settings().CONDITION_WEBHOOK_SECRET
    if len(secret) < 32:
        raise ApiError(503, "extension_disabled", "Condition webhook is not configured")

    raw_body = await request.body()
    try:
        event = SignedWebhookVerifier(secret).verify(
            timestamp=x_slothpay_timestamp or "",
            event_id=x_slothpay_event_id or "",
            signature=x_slothpay_signature or "",
            raw_body=raw_body,
        )
    except InvalidConditionEvent as exc:
        raise ApiError(401, "invalid_extension_event", "Invalid signed event") from exc

    # The owner must be inside the signed body. Never trust an owner header or cookie.
    payload = json.loads(raw_body)
    owner_id = payload.get("owner_id")
    if not isinstance(owner_id, str) or not OWNER_PATTERN.fullmatch(owner_id):
        raise ApiError(400, "invalid_extension_event", "Signed owner is missing or invalid")

    # Ensure the payment belongs to the signed owner and is eligible BEFORE claiming it.
    with translate_errors():
        current = await api.get_payment(owner_id, event.payment_id)
    if current is None:
        raise not_found()
    if current.path != "CONDITIONALPAY" or current.status != "AWAITING_CONDITION":
        raise ApiError(409, "condition_not_ready", "Payment is not awaiting verification")

    guard = ReplayGuard(redis)
    payment_digest = hashlib.sha256(str(event.payment_id).encode("ascii")).hexdigest()
    payment_key = f"slothpay:task18:payment-resolution:{payment_digest}"
    try:
        # Event reservation prevents replay of the same delivery notification.
        await guard.claim(event.event_id)
        # Separate long-lived reservation prevents different event IDs from resolving
        # the same payment in parallel. No automatic release on errors or timeouts.
        reserved = await redis.set(payment_key, event.event_id, nx=True, ex=PROCESSED_TTL)
        if not reserved:
            raise ReplayRejected("This payment already has a verification attempt")
    except ReplayRejected as exc:
        raise ApiError(409, "extension_replay", "Event or payment already received") from exc
    except RedisError as exc:
        log.warning("extension replay storage unavailable: %s", type(exc).__name__)
        raise PaymentsUnavailable() from exc

    evidence = {
        "source": "external_signed_webhook",
        "provider": "zapier_compatible",
        "event_id": event.event_id,
        "signed_timestamp": event.timestamp,
        "confirmed": False,  # This is evidence from a trusted sender, NOT from PayPal.
    }
    # No direct PayPal call. Existing payment service makes all state transitions.
    with translate_errors():
        result = await api.verify_condition(
            Caller(owner_id=owner_id), event.payment_id, event.verified, evidence
        )
    if result is None:
        raise not_found()

    try:
        await guard.complete(event.event_id)
    except (RedisError, ReplayRejected) as exc:
        # Keep payment reservation. Do not attempt resolution again automatically.
        log.warning("extension resolution needs reconciliation: %s", type(exc).__name__)
        raise PaymentsUnavailable("Verification outcome requires reconciliation") from exc

    return {
        "accepted": True,
        "payment_id": str(event.payment_id),
        "status": result.status,
    }
