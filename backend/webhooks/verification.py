"""Fail-closed PayPal Sandbox webhook verification via PayPal's REST endpoint.

No webhook event is trusted based on headers or payload alone.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import httpx

from backend.config.settings import get_settings

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"
HEADERS = {
    "paypal-auth-algo": "auth_algo",
    "paypal-cert-url": "cert_url",
    "paypal-transmission-id": "transmission_id",
    "paypal-transmission-sig": "transmission_sig",
    "paypal-transmission-time": "transmission_time",
}


class WebhookVerificationUnavailable(Exception):
    """PayPal could not verify this delivery; do not acknowledge it as accepted."""


async def verify_paypal_webhook(
    headers: Mapping[str, str],
    raw_body: bytes,
    *,
    http: httpx.AsyncClient | None = None,
) -> bool:
    """Return True only on an explicit SUCCESS from PayPal's verification API.

    A mock httpx client can be passed by unit tests. The caller owns that client.
    """
    settings = get_settings()
    if (
        not settings.PAYPAL_WEBHOOK_ID
        or not settings.PAYPAL_CLIENT_ID
        or not settings.PAYPAL_CLIENT_SECRET
    ):
        raise WebhookVerificationUnavailable("PayPal webhook verification is not configured")
    if settings.PAYPAL_BASE_URL.rstrip("/") != SANDBOX_BASE_URL:
        raise WebhookVerificationUnavailable("Only the PayPal Sandbox host is allowed")
    try:
        event = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        return False
    normalized = {str(k).lower(): str(v) for k, v in headers.items()}
    fields = {}
    for header, name in HEADERS.items():
        value = normalized.get(header)
        if not value or len(value) > 4096:
            return False
        fields[name] = value
    if not isinstance(event, dict):
        return False

    async def request_verify(client: httpx.AsyncClient) -> bool:
        try:
            token_response = await client.post(
                "/v1/oauth2/token",
                auth=(settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET),
                headers={"Accept": "application/json"},
                data={"grant_type": "client_credentials"},
            )
            token_response.raise_for_status()
            token = token_response.json().get("access_token")
            if not isinstance(token, str) or not token:
                raise ValueError("Missing OAuth access token")
            response = await client.post(
                "/v1/notifications/verify-webhook-signature",
                headers={"Authorization": f"Bearer {token}"},
                json={**fields, "webhook_id": settings.PAYPAL_WEBHOOK_ID, "webhook_event": event},
            )
            response.raise_for_status()
            value = response.json().get("verification_status")
            return value == "SUCCESS"
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            raise WebhookVerificationUnavailable("PayPal verification request failed") from exc

    if http is not None:
        return await request_verify(http)
    async with httpx.AsyncClient(base_url=SANDBOX_BASE_URL, timeout=3.0) as client:
        return await request_verify(client)
