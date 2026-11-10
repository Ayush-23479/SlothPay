from __future__ import annotations

import asyncio
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from backend.config.settings import get_settings
from backend.payments.hooks import FailureInjector, TransportLost, default_injector
from backend.reliability import timing

SANDBOX_URL = "https://api-m.sandbox.paypal.com"
# Task 4 rule: every PayPal call has a 3 second timeout.
DEFAULT_TIMEOUT_SECONDS = 3.0
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class PayPalError(Exception):
    """A definitive API error response from PayPal (not proof of payment outcome).

    Carries what later tasks need to classify the error:
    - status_code: the HTTP status
    - body: the parsed JSON error body ({} if PayPal sent none)
    - issue: body["details"][0]["issue"], e.g. "INSTRUMENT_DECLINED" (or None)
    - error_name: body["name"], e.g. "UNPROCESSABLE_ENTITY" (or None)
    - debug_id: the PayPal-Debug-Id response header (or None)
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        body: dict[str, Any] | None = None,
        debug_id: str | None = None,
    ):
        self.status_code = status_code
        self.body: dict[str, Any] = body or {}
        self.debug_id = debug_id
        details = self.body.get("details")
        first = details[0] if isinstance(details, list) and details else None
        issue = first.get("issue") if isinstance(first, dict) else None
        self.issue: str | None = issue if isinstance(issue, str) else None
        name = self.body.get("name")
        self.error_name: str | None = name if isinstance(name, str) else None
        suffix = ""
        if self.issue:
            suffix += f" issue={self.issue}"
        if debug_id:
            suffix += f" debug_id={debug_id}"
        super().__init__(f"PayPal HTTP {status_code}: {message}{suffix}")


class PayPalOutcomeUnknown(Exception):
    """An operation may have happened; reconcile it before considering retry."""

    def __init__(self, message: str, *, debug_id: str | None = None):
        super().__init__(message)
        self.debug_id = debug_id


class PayPalNotSent(Exception):
    """The request never reached PayPal's money endpoint (for example the OAuth
    token could not be fetched). Nothing happened, so the caller may safely retry.
    """


def _operation_name(method: str, path: str) -> str:
    """A short, id-free name for a request, such as capture_order or get_order."""
    parts = [part for part in path.split("/") if part]
    kind = (
        "order"
        if "orders" in parts
        else "authorization"
        if "authorizations" in parts
        else "capture"
    )
    if method == "GET":
        return f"get_{kind}"
    last = parts[-1] if parts else "request"
    if last == "orders":
        return "create_order"
    if last in {"capture", "authorize", "void"}:
        return f"{last}_{kind}"
    return f"{method.lower()}_{last}"


class PayPalClient:
    """Low-level API client. Callers must enforce payment policy before money operations."""

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        injector: FailureInjector | None = None,
    ) -> None:
        settings = get_settings()
        self._id = client_id if client_id is not None else settings.PAYPAL_CLIENT_ID
        self._secret = client_secret if client_secret is not None else settings.PAYPAL_CLIENT_SECRET
        url = (base_url if base_url is not None else settings.PAYPAL_BASE_URL).rstrip("/")
        if url != SANDBOX_URL:
            raise ValueError("Only PayPal's exact HTTPS sandbox API hostname is allowed")
        if not self._id or not self._secret:
            raise ValueError("Missing PayPal sandbox client ID or secret")
        if timeout <= 0:
            raise ValueError("HTTP timeout must be positive")
        self._http = httpx.AsyncClient(base_url=url, transport=transport, timeout=timeout)
        # Controlled failures (Task 12). A no-op unless chaos is allowed and switched on.
        self._injector = injector if injector is not None else default_injector()
        self._token: str | None = None
        self._token_expiry = 0.0
        self._token_lock = asyncio.Lock()

    async def __aenter__(self) -> PayPalClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _identifier(value: str, label: str) -> str:
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise ValueError(f"Invalid {label}")
        return value

    @staticmethod
    def _web_url(value: str, label: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) > 512
            or not re.fullmatch(r"https?://[^\s<>\"']+", value)
        ):
            raise ValueError(f"Invalid {label}")
        return value

    @staticmethod
    def _money(value: str, currency: str) -> dict[str, str]:
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("Currency must be an uppercase three-letter code")
        try:
            amount = Decimal(value)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Invalid monetary amount") from exc
        if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
            raise ValueError("Amount must be positive with at most two decimal places")
        # This basic gateway targets two-decimal currencies such as USD. Callers
        # must implement PayPal-specific currency precision/availability policy.
        return {"currency_code": currency, "value": f"{amount:.2f}"}

    @staticmethod
    def _json_dict(response: httpx.Response) -> dict[str, Any]:
        """Parse a response body as a JSON object; {} if it is empty or not one."""
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def token_seconds_left(self) -> float:
        """Seconds until the saved token must be replaced (0 when there is none)."""
        if not self._token:
            return 0.0
        return max(0.0, self._token_expiry - time.monotonic())

    async def refresh_token(self) -> str:
        """Fetch a new token now, even if the saved one is still valid. A shared client calls
        this in the background so no payment ever waits for the login round trip."""
        return await self._oauth_token(force=True)

    async def _oauth_token(self, *, force: bool = False) -> str:
        if not force and self._token and time.monotonic() < self._token_expiry:
            return self._token
        async with self._token_lock:
            if not force and self._token and time.monotonic() < self._token_expiry:
                return self._token
            fetch_started = time.perf_counter()
            try:
                response = await self._http.post(
                    "/v1/oauth2/token",
                    auth=(self._id, self._secret),
                    headers={"Accept": "application/json"},
                    data={"grant_type": "client_credentials"},
                )
            except httpx.RequestError as exc:
                # No money request has been sent yet, so this is NOT "outcome unknown".
                raise PayPalNotSent("PayPal OAuth network failure; request not sent") from exc
            if not 200 <= response.status_code < 300:
                raise PayPalError(
                    response.status_code,
                    "OAuth authentication failed",
                    body=self._json_dict(response),
                    debug_id=response.headers.get("PayPal-Debug-Id"),
                )
            try:
                payload = response.json()
                token = payload["access_token"]
                ttl = int(payload["expires_in"])
                if not isinstance(token, str) or not token or ttl <= 0:
                    raise ValueError("Invalid token")
            except (ValueError, TypeError, KeyError) as exc:
                raise PayPalNotSent("Malformed PayPal OAuth response; request not sent") from exc
            self._token = token
            self._token_expiry = time.monotonic() + max(0, ttl - 60)
            timing.record_provider("oauth_token", (time.perf_counter() - fetch_started) * 1000)
            return token

    async def check_authentication(self) -> bool:
        await self._oauth_token()
        return True

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        request_id: str | None = None,
        financial: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        # Validate everything locally first, so bad input never causes a network call.
        if financial and not request_id:
            raise ValueError("Financial operations require a stable persisted request_id")
        if request_id:
            request_id = self._identifier(request_id, "request_id")

        token = await self._oauth_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        operation = _operation_name(method, path)
        try:
            headers.update(await self._injector.before_send(operation))
        except TransportLost as exc:
            # The request never left, so nothing happened and a retry is safe.
            raise PayPalNotSent("Connection lost before the request was sent") from exc
        extra: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        timing.note_send(operation)
        sent_at = time.perf_counter()
        try:
            response = await self._http.request(
                method, path, json=json_body, headers=headers, **extra
            )
        except httpx.RequestError as exc:
            raise PayPalOutcomeUnknown(
                "PayPal network outcome unknown: reconcile before any retry"
            ) from exc
        finally:
            # How long PayPal took, kept apart from SlothPay's own time (Task 19).
            timing.record_provider(operation, (time.perf_counter() - sent_at) * 1000)
        try:
            await self._injector.after_send(operation)
        except TransportLost as exc:
            # PayPal got the request but we lost the answer: the classic unknown outcome.
            raise PayPalOutcomeUnknown(
                "PayPal network outcome unknown: reconcile before any retry"
            ) from exc
        debug_id = response.headers.get("PayPal-Debug-Id")
        if response.status_code >= 500:
            raise PayPalOutcomeUnknown(
                f"PayPal server returned HTTP {response.status_code}; reconcile before retry",
                debug_id=debug_id,
            )
        if not 200 <= response.status_code < 300:
            raise PayPalError(
                response.status_code,
                "API request rejected",
                body=self._json_dict(response),
                debug_id=debug_id,
            )
        if not response.content:
            return {"http_status": response.status_code}
        try:
            result = response.json()
        except ValueError as exc:
            raise PayPalOutcomeUnknown(
                "Malformed response: reconcile provider state", debug_id=debug_id
            ) from exc
        if not isinstance(result, dict):
            raise PayPalOutcomeUnknown("Unexpected response shape", debug_id=debug_id)
        return result

    async def create_order(
        self,
        *,
        intent: str,
        amount: str,
        currency: str,
        request_id: str,
        return_url: str | None = None,
        cancel_url: str | None = None,
    ) -> dict[str, Any]:
        if intent not in {"CAPTURE", "AUTHORIZE"}:
            raise ValueError("Order intent must be CAPTURE or AUTHORIZE")
        body: dict[str, Any] = {
            "intent": intent,
            "purchase_units": [{"amount": self._money(amount, currency)}],
        }
        if return_url is not None:
            # Where PayPal sends the buyer after approving (or cancelling) in the sandbox.
            context = {
                "return_url": self._web_url(return_url, "return_url"),
                "cancel_url": self._web_url(cancel_url or return_url, "cancel_url"),
                "user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING",
            }
            body["payment_source"] = {"paypal": {"experience_context": context}}
        return await self._request(
            "POST", "/v2/checkout/orders", json_body=body, request_id=request_id, financial=True
        )

    async def create_order_naive(
        self,
        *,
        amount: str,
        currency: str,
        return_url: str | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """A deliberately UNSAFE order call for the Compare page: no PayPal-Request-Id, so a
        repeat creates a second order, and a long timeout. It exists only to show what happens
        without SlothPay. Nothing in the payment path calls it."""
        body: dict[str, Any] = {
            "intent": "CAPTURE",
            "purchase_units": [{"amount": self._money(amount, currency)}],
        }
        if return_url is not None:
            context = {
                "return_url": self._web_url(return_url, "return_url"),
                "cancel_url": self._web_url(return_url, "cancel_url"),
                "user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING",
            }
            body["payment_source"] = {"paypal": {"experience_context": context}}
        return await self._request(
            "POST", "/v2/checkout/orders", json_body=body, request_id=None, timeout=timeout
        )

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/v2/checkout/orders/{self._identifier(order_id, 'order_id')}"
        )

    async def authorize_order(self, order_id: str, *, request_id: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/v2/checkout/orders/{self._identifier(order_id, 'order_id')}/authorize",
            json_body={},
            request_id=request_id,
            financial=True,
        )

    async def capture_order(self, order_id: str, *, request_id: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/v2/checkout/orders/{self._identifier(order_id, 'order_id')}/capture",
            json_body={},
            request_id=request_id,
            financial=True,
        )

    async def get_authorization(self, authorization_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/v2/payments/authorizations/" + self._identifier(authorization_id, "authorization_id"),
        )

    async def get_capture(self, capture_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/v2/payments/captures/" + self._identifier(capture_id, "capture_id"),
        )

    async def capture_authorization(
        self, authorization_id: str, *, request_id: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v2/payments/authorizations/"
            + self._identifier(authorization_id, "authorization_id")
            + "/capture",
            json_body={},
            request_id=request_id,
            financial=True,
        )

    async def void_authorization(self, authorization_id: str, *, request_id: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v2/payments/authorizations/"
            + self._identifier(authorization_id, "authorization_id")
            + "/void",
            request_id=request_id,
            financial=True,
        )