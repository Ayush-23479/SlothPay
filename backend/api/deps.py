
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Protocol

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from backend.api.schemas import ErrorOut, EventOut, PaymentOut, RescueOut
from backend.config.settings import get_settings
from backend.models.repository import StalePaymentVersion
from backend.notifications.hub import EventHub, get_hub
from backend.orchestration.lifecycle import LifecycleSafetyError
from backend.orchestration.recovery import RescueActionNotAllowed
from backend.payments.paypal_client import PayPalError, PayPalNotSent, PayPalOutcomeUnknown
from backend.reliability.idempotency import (
    KEY_MAX_LENGTH,
    KEY_MIN_LENGTH,
    IntentBusy,
    PayloadMismatch,
)

log = logging.getLogger("slothpay.api")

OWNER_HEADER = "X-Owner-Id"
OWNER_COOKIE = "slothpay_owner"
ADMIN_HEADER = "X-Admin-Api-Key"
_OWNER_PATTERN = re.compile(r"[A-Za-z0-9_.@:-]{1,128}")


# ------------------------------------------------------------------------------ errors
class ApiError(Exception):
    """An error with a status code and the stable shape of ErrorOut."""

    def __init__(
        self,
        status_code: int,
        error: str,
        message: str,
        *,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = error
        self.message = message
        self.retry_after = retry_after


class NotAllowed(ApiError):
    """The payment is in a state where this action is not permitted (HTTP 409)."""

    def __init__(self, error: str, message: str) -> None:
        super().__init__(409, error, message)


class PaymentsUnavailable(ApiError):
    """The payment backend is not configured or not reachable (HTTP 503)."""

    def __init__(self, message: str = "Payments are temporarily unavailable") -> None:
        super().__init__(503, "payments_unavailable", message)


def not_found() -> ApiError:
    """One 404 for a missing payment and for another owner's payment: they look identical."""
    return ApiError(404, "not_found", "Payment not found")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _handle(_request: Request, exc: ApiError) -> JSONResponse:
        body = ErrorOut(error=exc.error, message=exc.message, retry_after=exc.retry_after)
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        return JSONResponse(body.model_dump(), status_code=exc.status_code, headers=headers)


@contextmanager
def translate_errors() -> Iterator[None]:
    """Turn domain failures into ApiError. Messages are fixed text: never raw exception text
    from PayPal or the database."""
    try:
        yield
    except ApiError:
        raise
    except PayloadMismatch as exc:
        raise ApiError(409, "payload_mismatch", str(exc)) from exc
    except IntentBusy as exc:
        raise ApiError(
            409, "intent_busy", "Another request for this payment is in progress", retry_after=1
        ) from exc
    except StalePaymentVersion as exc:
        raise ApiError(409, "payment_changed", "The payment changed; reload and try again") from exc
    except (LifecycleSafetyError, RescueActionNotAllowed) as exc:
        raise ApiError(409, "not_allowed", "That action is not allowed right now") from exc
    except (PayPalNotSent, PayPalOutcomeUnknown, PayPalError) as exc:
        log.warning("PayPal call failed: %s", type(exc).__name__)
        raise ApiError(
            503,
            "paypal_unavailable",
            "PayPal could not confirm the request. Nothing is retried automatically; "
            "check the payment status.",
        ) from exc
    except (RedisError, SQLAlchemyError) as exc:
        log.exception("storage unavailable")
        raise PaymentsUnavailable() from exc


# ------------------------------------------------------------------------------ callers
@dataclass(frozen=True)
class Caller:
    owner_id: str | None
    is_admin: bool = False


def sign_owner(owner_id: str, secret: str) -> str:
    """Cookie value for a demo identity: <owner>.<hmac>. Task 16 replaces this."""
    mac = hmac.new(secret.encode(), owner_id.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{owner_id}.{mac}"


def _owner_from_cookie(value: str | None, secret: str) -> str | None:
    if not value or not secret or "." not in value:
        return None
    owner_id, _, mac = value.rpartition(".")
    expected = sign_owner(owner_id, secret).rpartition(".")[2]
    if hmac.compare_digest(mac, expected) and _OWNER_PATTERN.fullmatch(owner_id):
        return owner_id
    return None


def _is_admin(key: str | None) -> bool:
    configured = get_settings().ADMIN_API_KEY
    return bool(configured and key and hmac.compare_digest(configured, key))


def _caller(request: Request, admin_key: str | None, allow_admin: bool) -> Caller:
    settings = get_settings()
    owner_id = request.headers.get(OWNER_HEADER)
    if owner_id is not None and not _OWNER_PATTERN.fullmatch(owner_id):
        raise ApiError(401, "unauthorized", "Missing or invalid owner identity")
    if owner_id is None:
        owner_id = _owner_from_cookie(
            request.cookies.get(OWNER_COOKIE), settings.PASSPORT_SIGNING_SECRET
        )
    admin = allow_admin and _is_admin(admin_key)
    if owner_id is None and not admin:
        raise ApiError(401, "unauthorized", "Missing or invalid owner identity")
    return Caller(owner_id=owner_id, is_admin=admin)


def current_owner(request: Request) -> str:
    """The owner making the request (demo identity for now; Task 16 hardens it).

    Read from the X-Owner-Id header, or from the signed `slothpay_owner` cookie, which is what
    a browser EventSource uses because it cannot set headers."""
    caller = _caller(request, None, allow_admin=False)
    assert caller.owner_id is not None
    return caller.owner_id


def current_caller(
    request: Request, x_admin_api_key: Annotated[str | None, Header()] = None
) -> Caller:
    """An owner, or an administrator holding the admin key (used where admins may act)."""
    return _caller(request, x_admin_api_key, allow_admin=True)


def require_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    if idempotency_key is None or not idempotency_key.strip():
        raise ApiError(400, "missing_idempotency_key", "The Idempotency-Key header is required")
    key = idempotency_key.strip()
    if not KEY_MIN_LENGTH <= len(key) <= KEY_MAX_LENGTH:
        raise ApiError(
            400,
            "invalid_idempotency_key",
            f"Idempotency-Key must be {KEY_MIN_LENGTH} to {KEY_MAX_LENGTH} characters",
        )
    return key


def payment_uuid(payment_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(payment_id)
    except ValueError:
        raise not_found() from None


# ------------------------------------------------------------------------ service seam
class PaymentApi(Protocol):
    """Everything the routes need. Methods return None when the payment does not exist or
    belongs to another owner: the route turns both into the same 404."""

    async def create_payment(
        self, owner_id: str, key: str, payload: dict[str, Any]
    ) -> tuple[PaymentOut, bool]: ...

    async def list_payments(
        self, owner_id: str, limit: int, cursor: str | None
    ) -> tuple[list[PaymentOut], str | None]: ...

    async def get_payment(self, owner_id: str, payment_id: uuid.UUID) -> PaymentOut | None: ...

    async def list_events(
        self, owner_id: str, payment_id: uuid.UUID, after: str | None, limit: int
    ) -> list[EventOut] | None: ...

    async def latest_event_cursor(self, owner_id: str, payment_id: uuid.UUID) -> str | None: ...

    async def buyer_returned(self, owner_id: str, payment_id: uuid.UUID) -> PaymentOut | None: ...

    async def verify_condition(
        self, caller: Caller, payment_id: uuid.UUID, verified: bool, evidence: dict[str, Any]
    ) -> PaymentOut | None: ...

    async def cancel(self, owner_id: str, payment_id: uuid.UUID) -> PaymentOut | None: ...

    async def rescue_plan(self, owner_id: str, payment_id: uuid.UUID) -> RescueOut | None: ...

    async def rescue_action(
        self, owner_id: str, payment_id: uuid.UUID, action: str
    ) -> dict[str, Any] | None: ...


def get_api(request: Request) -> PaymentApi:
    api = getattr(request.app.state, "payment_api", None)
    if api is None:
        raise PaymentsUnavailable("The payment service is not running")
    return api


def get_event_hub() -> EventHub:
    return get_hub()


Owner = Annotated[str, Depends(current_owner)]
AnyCaller = Annotated[Caller, Depends(current_caller)]
Api = Annotated[PaymentApi, Depends(get_api)]
Hub = Annotated[EventHub, Depends(get_event_hub)]
IdempotencyKey = Annotated[str, Depends(require_idempotency_key)]
PaymentId = Annotated[uuid.UUID, Depends(payment_uuid)]