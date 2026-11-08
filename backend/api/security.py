from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import time
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.config.settings import Settings, get_settings

log = logging.getLogger("slothpay.security")

OWNER_COOKIE = "slothpay_owner"
OWNER_PATTERN = re.compile(r"[A-Za-z0-9_.@:-]{1,128}")
_SESSION_CONTEXT = b"slothpay-session-v1:"
DEFAULT_SESSION_SECONDS = 12 * 3600
WEBHOOK_PATH = "/webhooks/paypal"
WEBHOOK_MAX_BYTES = 256 * 1024
RATE_WINDOW_SECONDS = 60


# --------------------------------------------------------------------------- sessions
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_session(
    owner_id: str,
    secret: str,
    *,
    ttl_seconds: int = DEFAULT_SESSION_SECONDS,
    now: float | None = None,
) -> str:
    """A signed session token for one owner, valid for ttl_seconds."""
    if not secret:
        raise ValueError("A signing secret is required")
    if not OWNER_PATTERN.fullmatch(owner_id):
        raise ValueError("Invalid owner id")
    expiry = int((time.time() if now is None else now) + ttl_seconds)
    payload = _b64(f"{owner_id}:{expiry}".encode())
    mac = hmac.new(secret.encode(), _SESSION_CONTEXT + payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{_b64(mac)}"


def read_session(token: str | None, secret: str, *, now: float | None = None) -> str | None:
    """The owner id inside a valid, unexpired token, otherwise None. Never raises."""
    if not token or not secret or token.count(".") != 1:
        return None
    try:
        payload, _, mac_text = token.partition(".")
        expected = hmac.new(
            secret.encode(), _SESSION_CONTEXT + payload.encode(), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_unb64(mac_text), expected):
            return None
        owner_id, _, expiry_text = _unb64(payload).decode().rpartition(":")
        if int(expiry_text) <= (time.time() if now is None else now):
            return None
    except (ValueError, UnicodeError):
        return None
    return owner_id if OWNER_PATTERN.fullmatch(owner_id) else None


# --------------------------------------------------------------------------- responses
def _json_response(status: int, error: str, message: str, retry_after: int | None = None):
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    body = {"error": error, "message": message, "retry_after": retry_after}
    return JSONResponse(body, status_code=status, headers=headers)


# --------------------------------------------------------------------------- rate limits
class RateLimiter:
    """Fixed-window counter in Redis. hit() returns None when allowed, or the seconds to wait.

    If Redis is down the request is allowed (and a warning is logged): payments already depend
    on Redis for idempotency, and refusing every request would be worse than a missed limit."""

    def __init__(self, redis_factory: Callable[[], Any]) -> None:
        self._redis_factory = redis_factory

    async def hit(self, name: str, client: str, limit: int, window: int) -> int | None:
        if limit <= 0:
            return None
        key = f"slothpay:rl:{name}:{client}"
        try:
            redis = self._redis_factory()
            count = await redis.incr(key)
            if count == 1:
                await redis.expire(key, window)
            if count <= limit:
                return None
            ttl = await redis.ttl(key)
            if ttl is None or ttl < 0:  # a counter without an expiry would block forever
                await redis.expire(key, window)
                ttl = window
            return max(1, int(ttl))
        except Exception as exc:  # RedisError, connection refused, closed loop
            log.warning("rate limiter unavailable: %s", type(exc).__name__)
            return None


def rate_rules(settings: Settings) -> dict[tuple[str, str], tuple[str, int]]:
    return {
        ("POST", "/api/payments"): ("payments", settings.RATE_LIMIT_PAYMENTS_PER_MINUTE),
        ("POST", WEBHOOK_PATH): ("webhook", settings.RATE_LIMIT_WEBHOOK_PER_MINUTE),
        ("POST", "/api/demo-login"): ("login", settings.RATE_LIMIT_LOGIN_PER_MINUTE),
    }


class RateLimitMiddleware:
    """429 with Retry-After once a client sends too many requests to a limited route."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        redis_factory: Callable[[], Any],
        rules: dict[tuple[str, str], tuple[str, int]],
        window: int = RATE_WINDOW_SECONDS,
    ) -> None:
        self.app = app
        self.limiter = RateLimiter(redis_factory)
        self.rules = rules
        self.window = window

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rule = self.rules.get((scope["method"], scope["path"]))
        if rule is not None:
            name, limit = rule
            client = (scope.get("client") or ("unknown", 0))[0]
            wait = await self.limiter.hit(name, client, limit, self.window)
            if wait is not None:
                response = _json_response(
                    429, "rate_limited", "Too many requests. Please wait and try again.", wait
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------- body size cap
class _TooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """413 when a request body is bigger than the cap, whether or not it declares its length."""

    def __init__(
        self, app: ASGIApp, *, max_bytes: int, overrides: dict[str, int] | None = None
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.overrides = overrides or {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return
        limit = self.overrides.get(scope["path"], self.max_bytes)
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
            except ValueError:
                too_big = True
            if too_big:
                await self._refuse(scope, receive, send)
                return

        total = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > limit:
                    raise _TooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _TooLarge:
            if not started:
                await self._refuse(scope, receive, send)

    @staticmethod
    async def _refuse(scope: Scope, receive: Receive, send: Send) -> None:
        response = _json_response(413, "payload_too_large", "The request body is too large.")
        await response(scope, receive, send)


# --------------------------------------------------------------------------- headers
_DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


class SecurityHeadersMiddleware:
    """nosniff, no framing, no referrer, locked-down browser features, no caching of API data."""

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault(
                    "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"
                )
                headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
                if not path.startswith(_DOC_PATHS):
                    headers.setdefault(
                        "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
                    )
                if path.startswith(("/api/", "/webhooks/")):
                    headers.setdefault("Cache-Control", "no-store")
                if self.hsts:
                    headers.setdefault(
                        "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
                    )
            await send(message)

        await self.app(scope, receive, send_with_headers)


# --------------------------------------------------------------------------- login routes
router = APIRouter(prefix="/api", tags=["session"])


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: str | None = Field(default=None, min_length=1, max_length=32)


def _cookie_kwargs(settings: Settings) -> dict[str, Any]:
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": settings.secure_cookies,
        "path": "/",
    }


@router.post("/demo-login")
async def demo_login(body: LoginIn | None = None) -> JSONResponse:
    """Start a demo session. `user` picks one of the named demo users (DEMO_USERS); leaving it
    out creates a private guest, so two visitors never share payments."""
    settings = get_settings()
    secret = settings.PASSPORT_SIGNING_SECRET
    if not secret:
        return _json_response(503, "sessions_unavailable", "Sessions are not configured.")
    user = body.user if body else None
    if user is None:
        owner_id = f"demo-{uuid.uuid4().hex[:20]}"
    else:
        name = user.strip().lower()
        if name not in settings.demo_user_names:
            return _json_response(400, "invalid_user", "Unknown demo user.")
        owner_id = f"demo-{name}"
    ttl = settings.SESSION_TTL_HOURS * 3600
    token = make_session(owner_id, secret, ttl_seconds=ttl)
    expires_at = int(time.time()) + ttl
    response = JSONResponse({"owner_id": owner_id, "expires_at": expires_at})
    response.set_cookie(OWNER_COOKIE, token, max_age=ttl, **_cookie_kwargs(settings))
    return response


@router.post("/demo-logout")
async def demo_logout(request: Request) -> JSONResponse:
    settings = get_settings()
    response = JSONResponse({"status": "signed_out"})
    response.delete_cookie(OWNER_COOKIE, **_cookie_kwargs(settings))
    return response