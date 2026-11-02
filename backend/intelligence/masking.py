from __future__ import annotations

import re

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
# PayPal-style ids with a prefix (ORDER-..., AUTH-..., CAP-..., PAYID-...) and bare 13 to 20
# character upper-case order/capture ids such as 5O190127TN364715T.
PREFIXED_ID = re.compile(r"\b(?:ORDER|AUTH|CAP|CAPTURE|PAYID|PAY|WH|EC)-[A-Za-z0-9-]{3,}", re.I)
BARE_ID = re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{13,20}\b")
UUID = re.compile(r"\b[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b", re.IGNORECASE)
SECRET = re.compile(
    r"(?:\bA21[A-Za-z0-9_-]{16,}|\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,}"
    r"|\b(?:secret|token|password|api[_-]?key)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)
LONG_TOKEN = re.compile(r"\b(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{28,}\b")
# Phone, card and account numbers: 10 or more digits, optionally spaced or hyphenated.
# Decimal amounts (1234.56) and dates (2026-11-01) are shorter or use dots, so they stay.
LONG_NUMBER = re.compile(r"(?<![\w.])\+?(?:\d[ \-()]?){9,}\d(?!\w)")

_STRICT = (
    (EMAIL, "[email]"),
    (URL, "[link]"),
    (SECRET, "[secret]"),
    (PREFIXED_ID, "[id]"),
    (UUID, "[id]"),
    (BARE_ID, "[id]"),
    (LONG_TOKEN, "[token]"),
    (LONG_NUMBER, "[number]"),
)
# Invoice text legitimately holds long reference numbers, so ids and tokens are kept there.
_INVOICE = (
    (EMAIL, "[email]"),
    (URL, "[link]"),
    (SECRET, "[secret]"),
    (PREFIXED_ID, "[id]"),
    (LONG_NUMBER, "[number]"),
)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def mask_text(text: object, *, strict: bool = True, limit: int | None = None) -> str:
    """The text with emails, links, ids, secrets and long numbers replaced by placeholders."""
    value = _CONTROL.sub(" ", text if isinstance(text, str) else str(text))
    for pattern, placeholder in _STRICT if strict else _INVOICE:
        value = pattern.sub(placeholder, value)
    return value[:limit] if limit is not None else value


def contains_sensitive(text: str) -> bool:
    """True if the text still holds an email, link, id, secret or long number."""
    return any(
        pattern.search(text)
        for pattern in (EMAIL, URL, SECRET, PREFIXED_ID, UUID, BARE_ID, LONG_TOKEN, LONG_NUMBER)
    )


def contains_contact_or_secret(text: str) -> bool:
    """The weaker check used on invoice prompts: contact details and secrets only."""
    return any(pattern.search(text) for pattern in (EMAIL, URL, SECRET, PREFIXED_ID, LONG_NUMBER))