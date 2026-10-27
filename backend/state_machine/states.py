

from __future__ import annotations

# --------------------------------------------------------------------- paths
SMARTPAY = "SMARTPAY"
CONDITIONALPAY = "CONDITIONALPAY"
PATHS = (SMARTPAY, CONDITIONALPAY)
PATH_ALIASES = {"SMART": SMARTPAY, "CONDITIONAL": CONDITIONALPAY}

# ------------------------------------------------------------------ statuses
INTENT_CREATED = "INTENT_CREATED"
ORDER_CREATED = "ORDER_CREATED"
AWAITING_APPROVAL = "AWAITING_APPROVAL"
APPROVED = "APPROVED"
AUTHORIZE_REQUESTED = "AUTHORIZE_REQUESTED"
AUTHORIZED = "AUTHORIZED"
AWAITING_CONDITION = "AWAITING_CONDITION"
CAPTURE_REQUESTED = "CAPTURE_REQUESTED"
CAPTURE_PENDING = "CAPTURE_PENDING"
CAPTURED = "CAPTURED"
VOID_REQUESTED = "VOID_REQUESTED"
VOIDED = "VOIDED"
UNCERTAIN = "UNCERTAIN"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

ALL_STATUSES = frozenset(
    {
        INTENT_CREATED,
        ORDER_CREATED,
        AWAITING_APPROVAL,
        APPROVED,
        AUTHORIZE_REQUESTED,
        AUTHORIZED,
        AWAITING_CONDITION,
        CAPTURE_REQUESTED,
        CAPTURE_PENDING,
        CAPTURED,
        VOID_REQUESTED,
        VOIDED,
        UNCERTAIN,
        FAILED,
        CANCELLED,
    }
)

# Final statuses have no exits.
FINAL_STATUSES = frozenset({CAPTURED, VOIDED, FAILED, CANCELLED})

# Statuses that only exist on the ConditionalPay path.
CONDITIONAL_ONLY_STATUSES = frozenset(
    {AUTHORIZE_REQUESTED, AUTHORIZED, AWAITING_CONDITION, VOID_REQUESTED, VOIDED}
)

# The Task 2 table defaults new rows to 'CREATED'. Treat it as INTENT_CREATED.
STATUS_ALIASES = {"CREATED": INTENT_CREATED}

# ---------------------------------------------------- operation left uncertain
OP_CREATE_ORDER = "create_order"
OP_AUTHORIZE = "authorize"
OP_CAPTURE = "capture"
OP_VOID = "void"
PENDING_OPERATIONS = (OP_CREATE_ORDER, OP_AUTHORIZE, OP_CAPTURE, OP_VOID)

# ---------------------------------------------------------- display statuses
DISPLAY_COMPLETED = "COMPLETED"
DISPLAY_PENDING = "PENDING"
DISPLAY_ACTION_NEEDED = "ACTION_NEEDED"
DISPLAY_CLOSED = "CLOSED"
DISPLAY_STATUSES = (DISPLAY_COMPLETED, DISPLAY_PENDING, DISPLAY_ACTION_NEEDED, DISPLAY_CLOSED)

# ------------------------------------------------------------ provider errors
ERROR_RETRY = "RETRY"
ERROR_SWITCH_METHOD = "SWITCH_METHOD"
ERROR_FIX_INFO = "FIX_INFO"
ERROR_REAPPROVE = "REAPPROVE"
ERROR_OUR_BUG = "OUR_BUG"
ERROR_ESCALATE = "ESCALATE"
ERROR_CLASSES = (
    ERROR_RETRY,
    ERROR_SWITCH_METHOD,
    ERROR_FIX_INFO,
    ERROR_REAPPROVE,
    ERROR_OUR_BUG,
    ERROR_ESCALATE,
)

# ------------------------------------------------------------------- actions
ACTION_WAIT = "WAIT"
ACTION_RECONCILE = "RECONCILE"
ACTION_ASK_BUYER_TO_APPROVE = "ASK_BUYER_TO_APPROVE"
ACTION_RETRY_SAME_REQUEST = "RETRY_SAME_REQUEST"
ACTION_CAPTURE = "CAPTURE"
ACTION_VOID = "VOID"
ACTION_ASK_BUYER_NEW_METHOD = "ASK_BUYER_NEW_METHOD"
ACTION_CONTACT_SUPPORT = "CONTACT_SUPPORT"
ACTION_NONE = "NONE"
ACTIONS = (
    ACTION_WAIT,
    ACTION_RECONCILE,
    ACTION_ASK_BUYER_TO_APPROVE,
    ACTION_RETRY_SAME_REQUEST,
    ACTION_CAPTURE,
    ACTION_VOID,
    ACTION_ASK_BUYER_NEW_METHOD,
    ACTION_CONTACT_SUPPORT,
    ACTION_NONE,
)


# ------------------------------------------------------------------- helpers
def normalize_path(path: object) -> str | None:
    """Return 'SMARTPAY' or 'CONDITIONALPAY', or None when the path is unknown."""
    if not isinstance(path, str):
        return None
    value = path.strip().upper()
    value = PATH_ALIASES.get(value, value)
    return value if value in PATHS else None


def normalize_status(status: object) -> str | None:
    """Return a known status name, or None when the status is unknown."""
    if not isinstance(status, str):
        return None
    value = status.strip().upper()
    value = STATUS_ALIASES.get(value, value)
    return value if value in ALL_STATUSES else None


def is_final(status: object) -> bool:
    return normalize_status(status) in FINAL_STATUSES