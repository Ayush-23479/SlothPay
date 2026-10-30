
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

# IMPORTANT: these event names MUST be confirmed on the PayPal webhook event-names page
# (developer.paypal.com > Webhooks > event names) before the demo, and the webhook in the
# PayPal developer dashboard must be subscribed to exactly these types.
EVENT_MAP: dict[str, str] = {
    "CHECKOUT.ORDER.APPROVED": "ORDER_APPROVED",
    "PAYMENT.AUTHORIZATION.CREATED": "AUTHORIZATION_CREATED",
    "PAYMENT.AUTHORIZATION.VOIDED": "AUTHORIZATION_VOIDED",
    "PAYMENT.CAPTURE.PENDING": "CAPTURE_PENDING",
    "PAYMENT.CAPTURE.COMPLETED": "CAPTURE_COMPLETED",
    "PAYMENT.CAPTURE.DENIED": "CAPTURE_DENIED",
}

# The provider-status word each action means, in the vocabulary that
# state_machine.transitions.resolve_uncertain() understands.
PROVIDER_STATUS: dict[str, str] = {
    "AUTHORIZATION_CREATED": "AUTHORIZED",
    "AUTHORIZATION_VOIDED": "VOIDED",
    "CAPTURE_PENDING": "PENDING",
    "CAPTURE_COMPLETED": "COMPLETED",
    "CAPTURE_DENIED": "DENIED",
}

# Which provider operation each action is the answer to.
ACTION_OPERATION: dict[str, str] = {
    "AUTHORIZATION_CREATED": "authorize",
    "AUTHORIZATION_VOIDED": "void",
    "CAPTURE_PENDING": "capture",
    "CAPTURE_COMPLETED": "capture",
    "CAPTURE_DENIED": "capture",
}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _identifier(value: Any) -> str | None:
    if isinstance(value, str) and 1 <= len(value) <= 128:
        return value
    return None


def extract_order_id(event: dict) -> str | None:
    """The PayPal order id this event belongs to, or None.

    Looks in resource.supplementary_data.related_ids.order_id first (capture and
    authorization events carry it there), then resource.id for CHECKOUT.ORDER.* events.
    NOTE: the shape below is from PayPal's docs. The FIRST REAL sandbox payload must be
    logged and checked against it; adjust here if PayPal nests it differently.
    """
    if not isinstance(event, dict):
        return None
    resource = _dict(event.get("resource"))
    related = _dict(_dict(resource.get("supplementary_data")).get("related_ids"))
    order_id = _identifier(related.get("order_id"))
    if order_id:
        return order_id
    event_type = event.get("event_type")
    if isinstance(event_type, str) and event_type.startswith("CHECKOUT.ORDER."):
        return _identifier(resource.get("id"))
    return None


def extract_resource_id(event: dict) -> str | None:
    """resource.id: the capture id, authorization id or order id, depending on the event."""
    if not isinstance(event, dict):
        return None
    return _identifier(_dict(event.get("resource")).get("id"))


def extract_amount(event: dict) -> tuple[Decimal, str] | None:
    """(amount, currency) the event reports, or None when absent or malformed.

    Capture/authorization events carry resource.amount; CHECKOUT.ORDER.* events carry
    resource.purchase_units[0].amount.
    """
    resource = _dict(_dict(event).get("resource"))
    amount = resource.get("amount")
    if not isinstance(amount, dict):
        units = resource.get("purchase_units")
        if isinstance(units, list) and len(units) == 1:
            amount = _dict(units[0]).get("amount")
    amount = _dict(amount)
    value, currency = amount.get("value"), amount.get("currency_code")
    if not isinstance(value, str) or not isinstance(currency, str):
        return None
    try:
        return Decimal(value), currency.upper()
    except (InvalidOperation, ValueError):
        return None


# --------------------------------------------------------------------------- masking
# Personal data is dropped before anything is stored: emails, names, addresses, phones.
_SENSITIVE_KEYS = frozenset(
    {
        "email",
        "email_address",
        "payer_email",
        "payer_email_address",
        "name",
        "given_name",
        "surname",
        "full_name",
        "payer_name",
        "business_name",
        "address",
        "shipping",
        "shipping_address",
        "address_line_1",
        "address_line_2",
        "admin_area_1",
        "admin_area_2",
        "postal_code",
        "phone",
        "phone_number",
        "national_number",
    }
)
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def mask_payload(value: Any) -> Any:
    """A copy of the payload without emails, names and addresses (at any depth)."""
    if isinstance(value, dict):
        return {
            key: mask_payload(item)
            for key, item in value.items()
            if str(key).lower() not in _SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [mask_payload(item) for item in value]
    if isinstance(value, str) and _EMAIL.search(value):
        return _EMAIL.sub("[email removed]", value)
    return value