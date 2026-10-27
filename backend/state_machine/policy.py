

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from backend.state_machine.states import (
    ACTION_ASK_BUYER_NEW_METHOD,
    ACTION_ASK_BUYER_TO_APPROVE,
    ACTION_CAPTURE,
    ACTION_CONTACT_SUPPORT,
    ACTION_NONE,
    ACTION_RECONCILE,
    ACTION_RETRY_SAME_REQUEST,
    ACTION_VOID,
    ACTION_WAIT,
    APPROVED,
    AUTHORIZE_REQUESTED,
    AUTHORIZED,
    AWAITING_APPROVAL,
    AWAITING_CONDITION,
    CANCELLED,
    CAPTURE_PENDING,
    CAPTURE_REQUESTED,
    CAPTURED,
    CONDITIONAL_ONLY_STATUSES,
    CONDITIONALPAY,
    DISPLAY_ACTION_NEEDED,
    DISPLAY_CLOSED,
    DISPLAY_COMPLETED,
    DISPLAY_PENDING,
    ERROR_ESCALATE,
    ERROR_FIX_INFO,
    ERROR_OUR_BUG,
    ERROR_REAPPROVE,
    ERROR_SWITCH_METHOD,
    FAILED,
    FINAL_STATUSES,
    INTENT_CREATED,
    ORDER_CREATED,
    SMARTPAY,
    UNCERTAIN,
    VOID_REQUESTED,
    VOIDED,
    normalize_path,
    normalize_status,
)

Context = Mapping[str, Any] | None

# For a FAILED payment, the error class decides who must do something next.
# This is the only case where a final status offers an action, and it never
# touches PayPal: it only tells the buyer or an operator what to do.
_FAILED_ERROR_ACTIONS = {
    ERROR_SWITCH_METHOD: ACTION_ASK_BUYER_NEW_METHOD,
    ERROR_FIX_INFO: ACTION_ASK_BUYER_NEW_METHOD,
    ERROR_REAPPROVE: ACTION_ASK_BUYER_TO_APPROVE,
    ERROR_OUR_BUG: ACTION_CONTACT_SUPPORT,
    ERROR_ESCALATE: ACTION_CONTACT_SUPPORT,
}

# Statuses where a retry of the same PayPal request may be offered, but only after
# reconciliation proved the operation did not happen (context retry_allowed=True).
_RETRY_STATUSES = frozenset({INTENT_CREATED, APPROVED, AWAITING_CONDITION})

_WAIT_ONLY = frozenset(
    {
        INTENT_CREATED,
        ORDER_CREATED,
        AUTHORIZE_REQUESTED,
        AUTHORIZED,
        CAPTURE_REQUESTED,
        CAPTURE_PENDING,
        VOID_REQUESTED,
    }
)


def display_status_for(status: str, context: Context = None) -> str:
    """The one status the buyer sees.

    context may hold: reconcile_exhausted (bool), error_class (str | None).
    """
    ctx = context or {}
    s = normalize_status(status)
    if s is None:
        return DISPLAY_ACTION_NEEDED  # unknown status: show it to a human

    if s == CAPTURED:
        return DISPLAY_COMPLETED
    if s in (VOIDED, CANCELLED):
        return DISPLAY_CLOSED
    if s == FAILED:
        if ctx.get("error_class") in _FAILED_ERROR_ACTIONS:
            return DISPLAY_ACTION_NEEDED
        return DISPLAY_CLOSED
    if s == AWAITING_APPROVAL:
        return DISPLAY_ACTION_NEEDED  # the buyer must approve in PayPal
    if s == UNCERTAIN:
        return DISPLAY_ACTION_NEEDED if ctx.get("reconcile_exhausted") else DISPLAY_PENDING
    return DISPLAY_PENDING


def allowed_actions(path: str, status: str, context: Context = None) -> list[str]:
    """Actions that may be taken right now. Default deny: unknown input returns ['NONE'].

    context may hold: reconcile_exhausted, error_class, condition_verified, retry_allowed.
    CAPTURE is only offered from APPROVED on SmartPay, or from AWAITING_CONDITION on
    ConditionalPay once the condition is verified. VOID is only offered from
    AWAITING_CONDITION on ConditionalPay.
    """
    ctx = context or {}
    p = normalize_path(path)
    s = normalize_status(status)
    if p is None or s is None:
        return [ACTION_NONE]
    if p == SMARTPAY and s in CONDITIONAL_ONLY_STATUSES:
        return [ACTION_NONE]  # status that cannot exist on this path

    if s == FAILED:
        action = _FAILED_ERROR_ACTIONS.get(ctx.get("error_class"))
        return [action] if action else [ACTION_NONE]
    if s in FINAL_STATUSES:
        return [ACTION_NONE]

    if s == UNCERTAIN:
        return [ACTION_CONTACT_SUPPORT] if ctx.get("reconcile_exhausted") else [ACTION_RECONCILE]

    actions: list[str]
    if s in _WAIT_ONLY:
        actions = [ACTION_WAIT]
    elif s == AWAITING_APPROVAL:
        actions = [ACTION_ASK_BUYER_TO_APPROVE]
    elif s == APPROVED:
        actions = [ACTION_CAPTURE] if p == SMARTPAY else [ACTION_WAIT]
    elif s == AWAITING_CONDITION and p == CONDITIONALPAY:
        if ctx.get("condition_verified") is True:
            actions = [ACTION_CAPTURE, ACTION_VOID]
        else:
            actions = [ACTION_WAIT, ACTION_VOID]
    else:
        return [ACTION_NONE]

    if ctx.get("retry_allowed") is True and s in _RETRY_STATUSES:
        actions.append(ACTION_RETRY_SAME_REQUEST)
    return actions