from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.state_machine.policy import allowed_actions
from backend.state_machine.states import (
    ACTION_CAPTURE,
    ACTION_VOID,
    AUTHORIZE_REQUESTED,
    CAPTURE_REQUESTED,
    VOID_REQUESTED,
    normalize_path,
    normalize_status,
)
from backend.state_machine.transitions import validate_transition


class OperationNotPermitted(Exception):
    """The requested PayPal operation is not allowed from the current state."""


# The status that must be claimed (persisted) before each operation is sent to PayPal.
CLAIM_STATUS = {
    "authorize": AUTHORIZE_REQUESTED,
    "capture": CAPTURE_REQUESTED,
    "void": VOID_REQUESTED,
}

# Operations that also need a matching action in allowed_actions().
_POLICY_ACTION = {"capture": ACTION_CAPTURE, "void": ACTION_VOID}

# Never capture an authorization that is about to expire.
DEFAULT_MIN_REMAINING = timedelta(minutes=5)


def authorization_capturable(
    authorization: Mapping[str, Any] | None,
    now: datetime | None = None,
    min_remaining: timedelta = DEFAULT_MIN_REMAINING,
) -> bool:
    """True only if a PayPal authorization read says funds can still be captured.

    Pass the body of a fresh GET on the authorization (status and expiration_time fields).
    Fail closed: a missing field, an unexpected status, an unparsable or past expiry time,
    or less than `min_remaining` left all return False.
    The caller puts the result into allowed_actions() context as authorization_valid.
    """
    if not isinstance(authorization, Mapping):
        return False
    if str(authorization.get("status", "")).strip().upper() != "CREATED":
        return False
    raw = authorization.get("expiration_time")
    if not isinstance(raw, str):
        return False
    try:
        expires = datetime.fromisoformat(raw.strip())
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return expires - current >= min_remaining


def claim_status_for(
    path: str,
    current: str,
    operation: str,
    context: Mapping[str, Any] | None = None,
) -> str:
    """Return the status to claim before calling PayPal, or raise OperationNotPermitted.

    Use it as step 2 of claim-then-execute:
      1. read the payment (status, version) and, for capture, the authorization from PayPal;
      2. new_status = claim_status_for(...)
      3. transition_payment(expected_version, expected_status=current, new_status) + COMMIT;
         StalePaymentVersion means another worker won: do NOT call PayPal, reconcile instead;
      4. only now call PayPal with the stable request id.
    """
    p = normalize_path(path)
    s = normalize_status(current)
    op = str(operation).strip().lower()
    if p is None or s is None:
        raise OperationNotPermitted(f"Unknown path or status: {path!r}, {current!r}")
    if op not in CLAIM_STATUS:
        raise OperationNotPermitted(f"Unknown operation: {operation!r}")

    action = _POLICY_ACTION.get(op)
    if action is not None and action not in allowed_actions(p, s, context):
        raise OperationNotPermitted(f"{op} is not permitted from {s} on {p}")

    target = CLAIM_STATUS[op]
    ok, reason = validate_transition(p, s, target)
    if not ok:
        raise OperationNotPermitted(reason)
    return target