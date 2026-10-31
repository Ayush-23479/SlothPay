

from __future__ import annotations

from backend.state_machine.states import (
    ALL_STATUSES,
    APPROVED,
    AUTHORIZE_REQUESTED,
    AUTHORIZED,
    AWAITING_APPROVAL,
    AWAITING_CONDITION,
    CANCELLED,
    CAPTURE_PENDING,
    CAPTURE_REQUESTED,
    CAPTURED,
    CONDITIONALPAY,
    FAILED,
    FINAL_STATUSES,
    INTENT_CREATED,
    OP_AUTHORIZE,
    OP_CAPTURE,
    OP_CREATE_ORDER,
    OP_VOID,
    ORDER_CREATED,
    SMARTPAY,
    UNCERTAIN,
    VOID_REQUESTED,
    VOIDED,
    normalize_path,
    normalize_status,
)

StatusMap = dict[str, frozenset[str]]


def _merge(*maps: StatusMap) -> StatusMap:
    merged: dict[str, set[str]] = {}
    for status_map in maps:
        for status, targets in status_map.items():
            merged.setdefault(status, set()).update(targets)
    return {status: frozenset(targets) for status, targets in merged.items()}


# Steps both paths share. UNCERTAIN is deliberately NOT listed as a source:
# it can only be left through resolve_uncertain() (see below).
TRANSITIONS_COMMON: StatusMap = {
    # create_order can be left uncertain straight from INTENT_CREATED.
    INTENT_CREATED: frozenset({ORDER_CREATED, FAILED, CANCELLED, UNCERTAIN}),
    ORDER_CREATED: frozenset({AWAITING_APPROVAL, FAILED, UNCERTAIN}),
    AWAITING_APPROVAL: frozenset({APPROVED, CANCELLED, FAILED}),
    CAPTURE_REQUESTED: frozenset({CAPTURED, CAPTURE_PENDING, FAILED, UNCERTAIN}),
    CAPTURE_PENDING: frozenset({CAPTURED, FAILED}),
}

# SmartPay: capture right after approval.
TRANSITIONS_SMARTPAY: StatusMap = {
    APPROVED: frozenset({CAPTURE_REQUESTED}),
}

# ConditionalPay: authorize, wait for a verified condition, then capture or void.
TRANSITIONS_CONDITIONALPAY: StatusMap = {
    APPROVED: frozenset({AUTHORIZE_REQUESTED}),
    AUTHORIZE_REQUESTED: frozenset({AUTHORIZED, FAILED, UNCERTAIN}),
    AUTHORIZED: frozenset({AWAITING_CONDITION}),
    AWAITING_CONDITION: frozenset({CAPTURE_REQUESTED, VOID_REQUESTED}),
    VOID_REQUESTED: frozenset({VOIDED, FAILED, UNCERTAIN}),
}

TRANSITIONS: dict[str, StatusMap] = {
    SMARTPAY: _merge(TRANSITIONS_COMMON, TRANSITIONS_SMARTPAY),
    CONDITIONALPAY: _merge(TRANSITIONS_COMMON, TRANSITIONS_CONDITIONALPAY),
}

# ----------------------------------------------------------- leaving UNCERTAIN
# What PayPal reported for the operation we were unsure about, mapped to the status
# we may move to. The reconciler (Task 8) turns PayPal's raw answer into one of these
# provider-status words. Anything not listed keeps the payment UNCERTAIN.
_CREATE_ORDER_RESOLUTIONS = {
    "CREATED": ORDER_CREATED,
    "PAYER_ACTION_REQUIRED": ORDER_CREATED,
    "APPROVED": ORDER_CREATED,
    "NOT_FOUND": INTENT_CREATED,  # the order was never created; retry is allowed
    "FAILED": FAILED,
}
_AUTHORIZE_RESOLUTIONS = {
    "AUTHORIZED": AUTHORIZED,
    "APPROVED": APPROVED,  # authorization did not happen; retry is allowed
    "DECLINED": FAILED,
    "DENIED": FAILED,
    "FAILED": FAILED,
}
_CAPTURE_RESOLUTIONS_COMMON = {
    "COMPLETED": CAPTURED,
    "PENDING": CAPTURE_PENDING,
    "DECLINED": FAILED,
    "DENIED": FAILED,
    "FAILED": FAILED,
}
_VOID_RESOLUTIONS = {
    "VOIDED": VOIDED,
    "AUTHORIZED": AWAITING_CONDITION,  # still held, void did not happen
    "FAILED": FAILED,
}

UNCERTAIN_RESOLUTIONS: dict[str, dict[str, dict[str, str]]] = {
    SMARTPAY: {
        OP_CREATE_ORDER: _CREATE_ORDER_RESOLUTIONS,
        OP_CAPTURE: {**_CAPTURE_RESOLUTIONS_COMMON, "APPROVED": APPROVED},
    },
    CONDITIONALPAY: {
        OP_CREATE_ORDER: _CREATE_ORDER_RESOLUTIONS,
        OP_AUTHORIZE: _AUTHORIZE_RESOLUTIONS,
        OP_CAPTURE: {**_CAPTURE_RESOLUTIONS_COMMON, "AUTHORIZED": AWAITING_CONDITION},
        OP_VOID: _VOID_RESOLUTIONS,
    },
}


def resolve_uncertain(path: str, pending_operation: str, provider_status: str) -> str:
    """Return the status to move to once PayPal has told us what happened.

    Examples: ('SMARTPAY', 'capture', 'COMPLETED') -> CAPTURED;
              ('CONDITIONALPAY', 'void', 'VOIDED') -> VOIDED;
              ('CONDITIONALPAY', 'authorize', 'AUTHORIZED') -> AUTHORIZED.
    An unknown path, operation or provider status returns UNCERTAIN (no move).
    """
    p = normalize_path(path)
    if p is None or not isinstance(pending_operation, str) or not isinstance(provider_status, str):
        return UNCERTAIN
    table = UNCERTAIN_RESOLUTIONS[p].get(pending_operation.strip().lower(), {})
    return table.get(provider_status.strip().upper(), UNCERTAIN)


def _uncertain_targets(path: str, pending_operation: str | None) -> frozenset[str]:
    if pending_operation is None:
        return frozenset()
    table = UNCERTAIN_RESOLUTIONS.get(path, {}).get(str(pending_operation).strip().lower(), {})
    return frozenset(table.values())


# ------------------------------------------------------------------ validation
def validate_transition(
    path: str,
    current: str,
    next_status: str,
    pending_operation: str | None = None,
) -> tuple[bool, str]:
    """Return (True, '') if the move is allowed, otherwise (False, reason).

    Failure message: 'Invalid transition: X -> Y for path P'.
    From UNCERTAIN the move is only allowed when pending_operation is given and
    next_status is a status resolve_uncertain() can return for that operation.
    """
    p = normalize_path(path)
    if p is None:
        return False, f"Unknown path: {path!r}"
    cur = normalize_status(current)
    nxt = normalize_status(next_status)
    if cur is None or nxt is None:
        return False, f"Unknown status in transition: {current!r} -> {next_status!r}"

    message = f"Invalid transition: {cur} -> {nxt} for path {p}"

    if cur in FINAL_STATUSES:
        return False, f"{message} ({cur} is final)"

    if cur == UNCERTAIN:
        if pending_operation is None:
            return False, f"{message} (UNCERTAIN is left only through resolve_uncertain)"
        if nxt in _uncertain_targets(p, pending_operation):
            return True, ""
        return False, f"{message} (not a resolution of pending operation {pending_operation!r})"

    if nxt in TRANSITIONS[p].get(cur, frozenset()):
        return True, ""
    return False, message


def next_statuses(path: str, current: str, pending_operation: str | None = None) -> frozenset[str]:
    """All statuses that validate_transition() would accept from `current`."""
    p = normalize_path(path)
    cur = normalize_status(current)
    if p is None or cur is None or cur in FINAL_STATUSES:
        return frozenset()
    if cur == UNCERTAIN:
        return _uncertain_targets(p, pending_operation)
    return TRANSITIONS[p].get(cur, frozenset())


__all__ = [
    "ALL_STATUSES",
    "CAPTURED",
    "TRANSITIONS",
    "UNCERTAIN_RESOLUTIONS",
    "next_statuses",
    "resolve_uncertain",
    "validate_transition",
]