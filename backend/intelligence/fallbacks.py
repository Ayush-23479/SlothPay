from __future__ import annotations

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
    DISPLAY_ACTION_NEEDED,
    DISPLAY_CLOSED,
    DISPLAY_COMPLETED,
    DISPLAY_PENDING,
    ERROR_ESCALATE,
    ERROR_FIX_INFO,
    ERROR_OUR_BUG,
    ERROR_REAPPROVE,
    ERROR_RETRY,
    ERROR_SWITCH_METHOD,
)

DISPLAY_SENTENCES = {
    DISPLAY_COMPLETED: "The payment is complete.",
    DISPLAY_PENDING: "The payment is in progress and no action is needed right now.",
    DISPLAY_ACTION_NEEDED: "The payment needs someone to take a step before it can finish.",
    DISPLAY_CLOSED: "The payment is closed and no money is moving.",
}
UNKNOWN_DISPLAY_SENTENCE = "The payment needs a person to look at it."

# error class -> (what happened, what to do next)
ERROR_TEXT = {
    ERROR_SWITCH_METHOD: (
        "PayPal could not use this payment method.",
        "Ask the buyer to choose a different payment method.",
    ),
    ERROR_FIX_INFO: (
        "Some of the payment details were not accepted.",
        "Ask the buyer to check and correct their details, then try again.",
    ),
    ERROR_REAPPROVE: (
        "The buyer's approval is no longer valid.",
        "Ask the buyer to approve the payment again in PayPal.",
    ),
    ERROR_OUR_BUG: (
        "SlothPay hit a problem on its side. Your money was not taken twice.",
        "Contact support and quote this payment so it can be checked.",
    ),
    ERROR_ESCALATE: (
        "PayPal needs a person to review this payment.",
        "Contact support and quote this payment so it can be checked.",
    ),
    ERROR_RETRY: (
        "PayPal did not answer in time, so the result is not known yet.",
        "Wait. SlothPay is checking with PayPal and will not send the request again by itself.",
    ),
}
UNKNOWN_ERROR_TEXT = ERROR_TEXT[ERROR_ESCALATE]

# rescue action -> one sentence saying why that action is the right one
ACTION_TEXT = {
    ACTION_WAIT: "Nothing is wrong yet; waiting lets the payment finish by itself.",
    ACTION_RECONCILE: "The result is not confirmed, so check with PayPal before doing anything.",
    ACTION_RETRY_SAME_REQUEST: (
        "PayPal confirmed the earlier request did not happen, so the same request can be resent."
    ),
    ACTION_CAPTURE: "The condition is met and the funds are held, so the payment can be collected.",
    ACTION_VOID: "The held funds are not needed, so release them back to the buyer.",
    ACTION_ASK_BUYER_TO_APPROVE: "The buyer has not approved the payment in PayPal yet.",
    ACTION_ASK_BUYER_NEW_METHOD: "The buyer needs to pay with a different method.",
    ACTION_CONTACT_SUPPORT: "Automatic checks could not settle this, so a person must review it.",
    ACTION_NONE: "Nothing more can be done for this payment.",
}
UNKNOWN_ACTION_TEXT = ACTION_TEXT[ACTION_CONTACT_SUPPORT]

INVOICE_UNAVAILABLE = "Automatic reading is unavailable. Please fill in the form by hand."
INVOICE_UNREADABLE = "Could not read a valid amount and currency. Please fill in the form by hand."