from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.api.payments_service import _STATE_TITLES, mask_id
from backend.intelligence import fallbacks
from backend.state_machine.policy import display_status_for
from backend.state_machine.states import (
    ERROR_CLASSES,
    ERROR_ESCALATE,
    ERROR_RETRY,
    normalize_status,
)

TOKEN_TTL = timedelta(days=7)
# Sources that mean "PayPal told us". A webhook also needs a matching verified provider event.
PAYPAL_SOURCES = frozenset({"webhook", "paypal_get", "paypal_response", "reconcile"})
HIDDEN_EVENTS = frozenset({"AI_EXPLANATION", "RESCUE_PLAN_BUILT", "OPERATION_ATTEMPT_STARTED"})
_SAFE_TEXT = re.compile(r"[A-Za-z0-9 _.,:;'’()\-!?]{1,300}")

EVENT_TITLES = {
    "INTENT_CREATED": "Payment started",
    "DUPLICATE_SUPPRESSED": "Duplicate request blocked",
    "RECONCILIATION_CHECK": "Checked with PayPal",
    "RECONCILED": "Result confirmed with PayPal",
    "PROVIDER_OPERATION_OUTCOME_UNKNOWN": "PayPal's answer was lost",
    "PROVIDER_OPERATION_NOT_SENT": "Request never left SlothPay",
    "RESCUE_RETRY_CLAIMED": "Safe retry started",
    "RESCUE_ACTION_EXECUTED": "Rescue action run",
    "CONDITION_VERIFY_REQUESTED": "Condition check requested",
    "CONDITION_CAPTURE_REFUSED": "Collection refused",
    "WEBHOOK_IGNORED": "Notification ignored",
}
EVENT_EXPLANATIONS = {
    "INTENT_CREATED": "SlothPay wrote the payment down before contacting PayPal.",
    "DUPLICATE_SUPPRESSED": "The same request arrived again. No second payment was made.",
    "RECONCILIATION_CHECK": "SlothPay asked PayPal what happened instead of guessing.",
    "RECONCILED": "PayPal's own answer settled what happened.",
    "PROVIDER_OPERATION_OUTCOME_UNKNOWN": (
        "The answer from PayPal did not arrive, so the result was not assumed."
    ),
    "PROVIDER_OPERATION_NOT_SENT": "The request was stopped before it reached PayPal.",
    "RESCUE_RETRY_CLAIMED": "A resend was allowed only after it was proven nothing happened.",
    "RESCUE_ACTION_EXECUTED": "A rescue step was run.",
    "CONDITION_VERIFY_REQUESTED": "Someone said whether the condition was met.",
    "CONDITION_CAPTURE_REFUSED": "SlothPay would not take the money without a verified condition.",
    "WEBHOOK_IGNORED": "A notification was received but did not change this payment.",
}
SOURCE_LABELS = {
    "webhook": "PayPal webhook",
    "paypal_get": "PayPal (read)",
    "paypal_response": "PayPal (response)",
    "reconcile": "Reconciliation check",
    "api": "SlothPay API",
    "internal_client": "SlothPay",
    "internal_record": "SlothPay record",
    "rescue": "Rescue action",
    "intelligence": "AI explainer",
}
ACTOR_LABELS = {
    "orchestration": "SlothPay",
    "paypal_webhook": "PayPal webhook",
    "reconciliation": "Reconciliation check",
    "workflow": "SlothPay workflow",
    "api": "SlothPay API",
    "intelligence": "AI explainer",
}
OUTCOMES = {
    "CAPTURED": "The payment was completed.",
    "VOIDED": "The held money was released. Nothing was charged.",
    "CANCELLED": "The payment was cancelled. Nothing was charged.",
    "FAILED": "The payment did not go through. Nothing was charged.",
    "UNCERTAIN": "The result is being checked with PayPal. SlothPay will not charge twice.",
    "AWAITING_APPROVAL": "Waiting for the buyer to approve the payment in PayPal.",
    "AWAITING_CONDITION": "The money is held and waits for its condition.",
}
DEFAULT_OUTCOME = "The payment is in progress."
CAPTURED_UNCONFIRMED = "Marked as paid, but PayPal's confirmation is not recorded yet."


# ------------------------------------------------------------------------------ helpers
def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    return value if isinstance(value, str) else None


def _evidence(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("evidence")
    return value if isinstance(value, dict) else {}


def _status_label(status: str | None) -> str | None:
    normalized = normalize_status(status) or status
    return (
        _STATE_TITLES.get(normalized, normalized.replace("_", " ").capitalize())
        if normalized
        else None
    )


def _source_label(row: dict[str, Any]) -> str:
    source = _evidence(row).get("source")
    if isinstance(source, str) and source in SOURCE_LABELS:
        return SOURCE_LABELS[source]
    actor = row.get("actor")
    if isinstance(actor, str):
        if actor.startswith("owner:"):
            return "Account owner"
        if actor in ACTOR_LABELS:
            return ACTOR_LABELS[actor]
    return "SlothPay"


def verified_webhook_ids(provider_events: list[dict[str, Any]]) -> set[str]:
    return {
        str(event.get("event_id"))
        for event in provider_events
        if event.get("verified") is True and event.get("event_id")
    }


def is_confirmed(row: dict[str, Any], verified_ids: set[str]) -> bool:
    """True only for PayPal-sourced rows: a verified webhook, or a successful PayPal read."""
    evidence = _evidence(row)
    source = evidence.get("source")
    if evidence.get("confirmed") is not True or source not in PAYPAL_SOURCES:
        return False
    if source == "webhook":
        return str(evidence.get("event_id")) in verified_ids
    return True


# --------------------------------------------------------------------------- hash chain
def _canonical(entry: dict[str, Any]) -> str:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _entry_hash(previous: str, entry: dict[str, Any]) -> str:
    return hashlib.sha256((previous + _canonical(entry)).encode()).hexdigest()


def add_hash_chain(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give each entry entry_hash = sha256(previous_hash + canonical json of the entry)."""
    previous = ""
    chained = []
    for entry in entries:
        body = {key: value for key, value in entry.items() if key != "entry_hash"}
        digest = _entry_hash(previous, body)
        chained.append({**body, "entry_hash": digest})
        previous = digest
    return chained


def verify_chain(timeline: list[dict[str, Any]]) -> bool:
    """Recompute every hash. Changing, removing or reordering any entry breaks it."""
    previous = ""
    for entry in timeline:
        if not isinstance(entry, dict) or "entry_hash" not in entry:
            return False
        body = {key: value for key, value in entry.items() if key != "entry_hash"}
        if not hmac.compare_digest(
            _entry_hash(previous, body).encode(), str(entry["entry_hash"]).encode()
        ):
            return False
        previous = entry["entry_hash"]
    return True


# ------------------------------------------------------------------------- the pieces
def _timeline(events: list[dict[str, Any]], verified_ids: set[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for row in events:
        event_type = str(row.get("event_type") or "")
        if event_type in HIDDEN_EVENTS:
            continue
        to_state = normalize_status(row.get("to_state")) or row.get("to_state")
        from_state = normalize_status(row.get("from_state")) or row.get("from_state")
        if event_type == "STATE_TRANSITION" and from_state and from_state == to_state:
            continue  # "Payment started" -> "Payment started" tells the reader nothing
        change = None
        if from_state and to_state:
            title = _STATE_TITLES.get(to_state, EVENT_TITLES.get(event_type, "Status changed"))
            explanation = (
                f"Status changed from “{_status_label(from_state)}” to “{_status_label(to_state)}”."
            )
            change = {"from": from_state, "to": to_state}
        elif event_type == "STATE_TRANSITION" and to_state:
            title = _STATE_TITLES.get(to_state, "Status changed")
            explanation = f"The payment moved to “{_status_label(to_state)}”."
            change = {"from": None, "to": to_state}
        else:
            title = EVENT_TITLES.get(event_type) or event_type.replace("_", " ").capitalize()
            explanation = EVENT_EXPLANATIONS.get(event_type, "SlothPay recorded this step.")
        entries.append(
            {
                "time": _iso(row.get("created_at")),
                "title": title,
                "status_change": change,
                "source": _source_label(row),
                "confirmed": is_confirmed(row, verified_ids),
                "explanation": explanation,
            }
        )
    return entries


def _ai_texts(events: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """The latest AI_EXPLANATION text per error class, as the buyer was shown it."""
    found: dict[str, dict[str, str]] = {}
    for row in events:
        if row.get("event_type") != "AI_EXPLANATION":
            continue
        evidence = _evidence(row)
        details = evidence.get("details") if isinstance(evidence.get("details"), dict) else {}
        text = evidence.get("text") if isinstance(evidence.get("text"), dict) else {}
        action, source = details.get("action"), details.get("source")
        message, step = text.get("message"), text.get("next_step")
        if (
            isinstance(action, str)
            and action.startswith("explained_")
            and source in {"model", "template"}
            and isinstance(message, str)
            and isinstance(step, str)
            and _SAFE_TEXT.fullmatch(message)
            and _SAFE_TEXT.fullmatch(step)
        ):
            found[action.removeprefix("explained_")] = {
                "message": message,
                "next_step": step,
                "source": source,
            }
    return found


def _error(
    row: dict[str, Any],
    error_class: str,
    ai: dict[str, dict[str, str]],
    did: str,
    *,
    operation: str | None = None,
    plain: str | None = None,
    step: str | None = None,
) -> dict[str, Any]:
    if plain is None:
        known = error_class if error_class in ERROR_CLASSES else ERROR_ESCALATE
        chosen = ai.get(known)
        if chosen:
            plain, step, source = chosen["message"], chosen["next_step"], chosen["source"]
        else:
            plain, step = fallbacks.ERROR_TEXT.get(known, fallbacks.UNKNOWN_ERROR_TEXT)
            source = "template"
    else:
        source = "template"
    return {
        "time": _iso(row.get("created_at")),
        "operation": operation or "payment",
        "error_class": error_class,
        "plain_explanation": plain,
        "next_step": step,
        "explanation_source": source,
        "what_slothpay_did": did,
    }


def _operation(row: dict[str, Any]) -> str | None:
    value = _evidence(row).get("operation")
    return value if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,32}", value) else None


def _errors(
    events: list[dict[str, Any]], provider_events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    ai = _ai_texts(events)
    errors: list[dict[str, Any]] = []
    reconciled_at = [row["created_at"] for row in events if row.get("event_type") == "RECONCILED"]
    for row in events:
        event_type = row.get("event_type")
        to_state = normalize_status(row.get("to_state"))
        if event_type == "PROVIDER_OPERATION_OUTCOME_UNKNOWN":
            settled = any(
                isinstance(when, datetime)
                and isinstance(row.get("created_at"), datetime)
                and when >= row["created_at"]
                for when in reconciled_at
            )
            did = (
                "Did not send the request again. Asked PayPal what happened, using the same "
                "request id."
            )
            if settled:
                did += " PayPal's answer then settled it."
            errors.append(_error(row, ERROR_RETRY, ai, did, operation=_operation(row)))
        elif event_type == "PROVIDER_OPERATION_NOT_SENT":
            errors.append(
                _error(
                    row,
                    "NOT_SENT",
                    ai,
                    "Kept the same request id, so a resend cannot create a duplicate.",
                    operation=_operation(row),
                    plain="The request never left SlothPay, so PayPal received nothing.",
                    step="It is safe to try again.",
                )
            )
        elif event_type == "CONDITION_CAPTURE_REFUSED":
            errors.append(
                _error(
                    row,
                    "CONDITION_NOT_MET",
                    ai,
                    "Refused to take the money.",
                    plain="The money was not collected because the condition was not verified.",
                    step="Verify the condition first, or let the hold be released.",
                )
            )
        elif event_type == "STATE_TRANSITION" and to_state == "FAILED":
            raw = _evidence(row).get("error_class")
            error_class = raw if raw in ERROR_CLASSES else ERROR_ESCALATE
            errors.append(
                _error(
                    row,
                    error_class,
                    ai,
                    "Stopped the payment and showed what to do next. Nothing was charged.",
                    operation=_operation(row),
                )
            )
    for event in provider_events:
        if event.get("verified") is False:
            errors.append(
                {
                    "time": _iso(event.get("received_at")),
                    "operation": "webhook",
                    "error_class": "WEBHOOK_UNVERIFIED",
                    "plain_explanation": (
                        "A notification could not be verified as coming from PayPal."
                    ),
                    "next_step": "Nothing to do. It was not used.",
                    "explanation_source": "template",
                    "what_slothpay_did": "Ignored it. It changed nothing.",
                }
            )
    errors.sort(key=lambda item: item["time"] or "")
    return errors


def _protections(events: list[dict[str, Any]], attempts: list[dict[str, Any]]) -> dict[str, Any]:
    types = [row.get("event_type") for row in events]
    ids: list[str] = []
    per_operation: dict[str, set[str]] = {}
    for attempt in attempts:
        request_id = attempt.get("request_id")
        if isinstance(request_id, str) and request_id:
            masked = mask_id(request_id)
            if masked and masked not in ids:
                ids.append(masked)
            per_operation.setdefault(str(attempt.get("operation")), set()).add(request_id)
    # A "blind" retry is the same operation sent again under a NEW request id. SlothPay reuses
    # one stable id per operation, so the honest number is normally 0.
    blind = sum(max(0, len(found) - 1) for found in per_operation.values())
    return {
        "duplicate_replays": types.count("DUPLICATE_SUPPRESSED"),
        "paypal_request_ids_used": ids,
        "blind_retries": blind,
        "safe_retries": types.count("RESCUE_RETRY_CLAIMED"),
        "reconciled": "RECONCILED" in types,
    }


def _condition(intent: dict[str, Any], condition: dict[str, Any] | None) -> dict[str, Any] | None:
    if intent.get("path") != "CONDITIONALPAY":
        return None
    condition = condition or {}
    status = str(condition.get("status") or "PENDING").upper()
    evidence = condition.get("evidence") if isinstance(condition.get("evidence"), dict) else {}
    source = evidence.get("source")
    if status == "VERIFIED":
        summary = "The condition was confirmed as met."
    elif status == "PENDING":
        summary = "No evidence yet. The money waits for the condition."
    else:
        summary = "The condition was not met, so the money was not taken."
    if isinstance(source, str) and re.fullmatch(r"[A-Za-z0-9_ .-]{1,40}", source):
        summary += f" Source: {source}."
    description = condition.get("description")
    return {
        "required": description[:200] if isinstance(description, str) and description else None,
        "status": status,
        "verified_at": _iso(condition.get("verified_at")),
        "evidence_summary": summary,
    }


def _outcome(status: str, timeline: list[dict[str, Any]]) -> str:
    if status == "CAPTURED" and not any(
        entry["confirmed"] and (entry["status_change"] or {}).get("to") == "CAPTURED"
        for entry in timeline
    ):
        return CAPTURED_UNCONFIRMED
    return OUTCOMES.get(status, DEFAULT_OUTCOME)


# ------------------------------------------------------------------------------ builder
def build_passport(
    intent: dict[str, Any],
    events: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    condition: dict[str, Any] | None,
    provider_events: list[dict[str, Any]],
    *,
    shared: bool = False,
) -> dict[str, Any]:
    """The passport for one payment. `shared=True` is the view for anyone holding a link."""
    ordered = sorted(
        events, key=lambda row: row.get("created_at") or datetime.min.replace(tzinfo=UTC)
    )
    status = normalize_status(intent.get("status")) or str(intent.get("status") or "")
    verified_ids = verified_webhook_ids(provider_events)
    entries = _timeline(ordered, verified_ids)
    passport = {
        "view": "shared" if shared else "owner",
        "summary": {
            "payment_id": mask_id(str(intent.get("id"))),
            "path": intent.get("path"),
            "amount": intent.get("amount"),
            "currency": intent.get("currency"),
            "display_status": display_status_for(status, {}),
            "final_outcome": _outcome(status, entries),
        },
        "timeline": entries,
        "condition": _condition(intent, condition),
        "errors": _errors(ordered, provider_events),
        "protections": _protections(ordered, attempts),
    }
    owner = intent.get("owner_id")
    if shared and isinstance(owner, str) and owner:
        # Nothing above is built from the owner id, but a shared page must never carry it, so
        # scrub any stray occurrence as a last line of defence (before the hashes are made).
        passport = json.loads(json.dumps(passport).replace(owner, "[owner]"))
    timeline = add_hash_chain(passport["timeline"])
    passport["timeline"] = timeline
    passport["integrity"] = {
        "chain_valid": verify_chain(timeline),
        "head": timeline[-1]["entry_hash"] if timeline else None,
    }
    return passport


# --------------------------------------------------------------------------- share link
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: str, payload: str) -> str:
    return _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())


def make_token(intent_id: uuid.UUID | str, secret: str, now: datetime) -> tuple[str, datetime]:
    """base64url(intent_id:expiry) + '.' + HMAC-SHA256(secret), valid for 7 days."""
    if not secret:
        raise ValueError("PASSPORT_SIGNING_SECRET is not set")
    expires = now + TOKEN_TTL
    payload = _b64(f"{intent_id}:{int(expires.timestamp())}".encode())
    return f"{payload}.{_sign(secret, payload)}", expires


def read_token(token: str, secret: str, now: datetime) -> uuid.UUID | None:
    """The payment id inside a valid, unexpired token; None for anything else."""
    if not secret or not isinstance(token, str) or token.count(".") != 1 or len(token) > 400:
        return None
    payload, _, signature = token.partition(".")
    if not hmac.compare_digest(_sign(secret, payload).encode(), signature.encode()):
        return None
    try:
        raw = _unb64(payload).decode()
        intent_text, _, expiry_text = raw.rpartition(":")
        if int(expiry_text) <= int(now.timestamp()):
            return None
        return uuid.UUID(intent_text)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None