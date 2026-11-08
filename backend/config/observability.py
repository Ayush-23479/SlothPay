from __future__ import annotations

import logging
import re
from typing import Any

from backend.config.settings import Settings

log = logging.getLogger("slothpay.observability")

LAYERS = frozenset(
    {"foundation", "gateway", "orchestration", "safety", "events", "delivery", "intelligence"}
)
LEVELS = frozenset({"debug", "info", "warning", "error", "fatal"})
TRACES_SAMPLE_RATE = 0.1
REDACTED = "[redacted]"
EMAIL_REDACTED = "[email]"
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}")
_SECRET_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-admin-api-key",
        "paypal-auth-assertion",
    }
)
_MAX_DEPTH = 12
_TAG_MAX = 200

_enabled = False


# ------------------------------------------------------------------------------ scrubbing
def _scrub_value(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return value
    if isinstance(value, str):
        return _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", _EMAIL.sub(EMAIL_REDACTED, value))
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _SECRET_HEADERS:
                out[key] = REDACTED
            else:
                out[key] = _scrub_value(item, depth + 1)
        return out
    if isinstance(value, list):
        return [_scrub_value(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(item, depth + 1) for item in value)
    return value


def _scrub_headers(headers: Any) -> Any:
    if isinstance(headers, dict):
        return {
            key: (REDACTED if str(key).lower() in _SECRET_HEADERS else value)
            for key, value in headers.items()
        }
    if isinstance(headers, list):  # [[name, value], ...]
        return [
            [item[0], REDACTED]
            if len(item) == 2 and str(item[0]).lower() in _SECRET_HEADERS
            else item
            for item in headers
        ]
    return headers


def scrub_event(event: dict[str, Any], hint: Any = None) -> dict[str, Any] | None:
    """Sentry before_send: strip secrets, request bodies and email-like text."""
    try:
        request = event.get("request")
        if isinstance(request, dict):
            if "headers" in request:
                request["headers"] = _scrub_headers(request["headers"])
            for key in ("data", "cookies", "query_string"):
                request.pop(key, None)
        event.pop("user", None)
        return _scrub_value(event)
    except Exception:
        # If scrubbing itself breaks, send nothing rather than something unscrubbed.
        return None


# ------------------------------------------------------------------------------ setup
def init_observability(settings: Settings) -> bool:
    """Start Sentry. Returns True when it is active; an empty DSN leaves everything a no-op."""
    global _enabled
    if not settings.SENTRY_DSN:
        _enabled = False
        return False
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment=settings.ENV,
            traces_sample_rate=TRACES_SAMPLE_RATE,
            send_default_pii=False,
            max_request_body_size="never",
            before_send=scrub_event,
        )
    except Exception as exc:  # monitoring must never stop the app from starting
        log.warning("Sentry could not start: %s", type(exc).__name__)
        _enabled = False
        return False
    _enabled = True
    return True


def is_enabled() -> bool:
    return _enabled


def _clean_tag(value: Any) -> str:
    return _EMAIL.sub(EMAIL_REDACTED, str(value))[:_TAG_MAX]


def tag_event(layer: str, operation: str, intent_id: str | None = None) -> None:
    """Tag whatever Sentry event happens next in this scope (layer, operation, intent_id)."""
    if not _enabled:
        return
    try:
        import sentry_sdk

        if layer in LAYERS:
            sentry_sdk.set_tag("layer", layer)
        sentry_sdk.set_tag("operation", _clean_tag(operation))
        if intent_id:
            sentry_sdk.set_tag("intent_id", _clean_tag(intent_id))
    except Exception as exc:
        log.debug("tag_event failed: %s", type(exc).__name__)


def report(message: str, level: str = "info", **tags: Any) -> None:
    """Send one Sentry message with tags (layer, operation, intent_id, anything else).

    Used for: UNCERTAIN entered, reconciled after uncertainty, duplicate suppressed,
    webhook rejected, AI fallback used.
    """
    if not _enabled:
        return
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            for name, value in tags.items():
                if value is None:
                    continue
                if name == "layer" and value not in LAYERS:
                    continue
                scope.set_tag(name, _clean_tag(value))
            sentry_sdk.capture_message(
                _clean_tag(message), level=level if level in LEVELS else "info"
            )
    except Exception as exc:
        log.debug("report failed: %s", type(exc).__name__)