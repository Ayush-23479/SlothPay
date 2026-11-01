
from __future__ import annotations

from datetime import UTC, datetime, timedelta

TASK_QUEUE = "slothpay-payments"

# Longest ConditionalPay wait when the caller does not say (matches settings.CONDITION_MAX_HOURS).
# The workflow never reads settings: the client passes the real value in as an argument.
DEFAULT_MAX_HOURS = 48

# Workflow and activity names. Workflows call activities by NAME so workflow code never
# imports database or PayPal modules.
CONDITIONAL_WORKFLOW = "ConditionalPayWorkflow"
RECONCILE_WORKFLOW = "ReconcileWorkflow"
SILENCE_WORKFLOW = "SilenceWatcherWorkflow"

ACTIVITY_RESOLVE_CONDITION = "resolve_condition"
ACTIVITY_MARK_EXPIRED = "mark_condition_expired"
ACTIVITY_RECONCILE = "reconcile"

SIGNAL_CONDITION_RESULT = "condition_result"
SIGNAL_CANCEL = "cancel"
SIGNAL_WEBHOOK_CONFIRMED = "webhook_confirmed"

# Same schedule as Task 8's StatusReconciler (a test keeps the two from drifting apart).
RECONCILE_DELAYS: tuple[float, ...] = (0.3, 1.0, 2.0, 4.0, 8.0)
# Reconcile decisions that end a check loop (kept equal to backend.reliability.reconcile).
RECONCILE_STOP_DECISIONS = frozenset(
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
# Decisions that mean "nothing proven yet, look again". Anything else not above is a stop.
RECONCILE_RETRY_DECISIONS = frozenset({"UNKNOWN", "WAITING"})
# After a successful capture, wait this long for the webhook before checking PayPal ourselves.
SILENCE_WAIT_SECONDS = 10.0

# Why a condition was resolved. Anything but "verified" ends in a void (default deny).
REASON_VERIFIED = "verified"
REASON_FAILED = "failed"
REASON_CANCELLED = "cancelled"
REASON_DEADLINE = "deadline"
REASON_INVALID_DEADLINE = "invalid_deadline"


def conditional_workflow_id(intent_id: str) -> str:
    return f"conditional-{intent_id}"


def reconcile_workflow_id(intent_id: str) -> str:
    return f"reconcile-{intent_id}"


def silence_workflow_id(intent_id: str) -> str:
    return f"silence-{intent_id}"


def parse_deadline(deadline_iso: str) -> datetime | None:
    """A timezone-aware deadline, or None if the text is not a valid ISO timestamp."""
    if not isinstance(deadline_iso, str):
        return None
    try:
        parsed = datetime.fromisoformat(deadline_iso.strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def clamp_wait_seconds(now: datetime, deadline: datetime | None, max_hours: int) -> float:
    """Seconds to wait for the condition: never negative, never beyond max_hours.

    An unreadable deadline waits 0 seconds: with no valid deadline the safe outcome is a void.
    """
    ceiling = max(0, int(max_hours)) * 3600.0
    if deadline is None:
        return 0.0
    remaining = (deadline - now).total_seconds()
    return float(min(max(remaining, 0.0), ceiling))


def clamp_deadline(now: datetime, deadline_iso: str, max_hours: int) -> str:
    """The deadline as ISO text, pulled in to at most max_hours from now."""
    wait = clamp_wait_seconds(now, parse_deadline(deadline_iso), max_hours)
    return (now + timedelta(seconds=wait)).isoformat()