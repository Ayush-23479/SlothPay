"""Validate signed external condition-verification events.

Validation alone never triggers a capture or void.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

MAX_EVENT_AGE_SECONDS = 300
MAX_BODY_BYTES = 16 * 1024
_SIGNATURE_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class InvalidConditionEvent(ValueError):
    """An external event failed validation."""


@dataclass(frozen=True)
class ConditionEvent:
    event_id: str
    payment_id: uuid.UUID
    verified: bool
    timestamp: int


def sign_event(
    secret: str,
    timestamp: int,
    event_id: str,
    raw_body: bytes,
) -> str:
    """HMAC-SHA256 over timestamp, event identifier and exact body bytes."""
    if not secret:
        raise ValueError("A signing secret is required")
    message = (
        str(timestamp).encode("ascii")
        + b"."
        + event_id.encode("ascii")
        + b"."
        + raw_body
    )
    return hmac.new(
        secret.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()


def verify_event(
    *,
    secret: str,
    timestamp: str,
    event_id: str,
    signature: str,
    raw_body: bytes,
    now: int | None = None,
) -> ConditionEvent:
    """Reject missing, expired, malformed and incorrectly signed events."""
    if not secret:
        raise InvalidConditionEvent("Verifier is not configured")

    if not isinstance(raw_body, bytes) or len(raw_body) > MAX_BODY_BYTES:
        raise InvalidConditionEvent("Invalid body size")

    if not re.fullmatch(r"[0-9]{1,12}", timestamp):
        raise InvalidConditionEvent("Invalid timestamp")

    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", event_id):
        raise InvalidConditionEvent("Invalid event identifier")

    if not _SIGNATURE_PATTERN.fullmatch(signature):
        raise InvalidConditionEvent("Invalid signature format")

    issued_at = int(timestamp)
    current = int(time.time()) if now is None else now

    if abs(current - issued_at) > MAX_EVENT_AGE_SECONDS:
        raise InvalidConditionEvent("Event timestamp outside allowed window")

    expected = sign_event(secret, issued_at, event_id, raw_body)

    if not hmac.compare_digest(
        signature.encode("ascii"),
        expected.encode("ascii"),
    ):
        raise InvalidConditionEvent("Signature verification failed")

    try:
        payload: Any = json.loads(raw_body)
        if not isinstance(payload, dict):
            raise ValueError("Expected an object")

        if type(payload.get("verified")) is not bool:
            raise ValueError("Expected a Boolean verification result")

        payment_id = uuid.UUID(payload["payment_id"])
        if str(payment_id) != payload["payment_id"]:
            raise ValueError("Noncanonical payment identifier")

    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise InvalidConditionEvent("Invalid condition payload") from exc

    return ConditionEvent(
        event_id=event_id,
        payment_id=payment_id,
        verified=payload["verified"],
        timestamp=issued_at,
    )
