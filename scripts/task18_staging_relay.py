"""Local-only Task 18 signing relay for one synthetic payment.

NOT a production service. Never expose this process to the internet.
"""

import hmac
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.extensions.signed_conditions import sign_event  # noqa: E402

app = FastAPI(title="SlothPay Task 18 Local Test Relay")

TEST_PAYMENT_ID = "00000000-0000-4000-8000-000000000001"
TEST_OWNER = "task18_demo"

DESTINATION = (
    "http://127.0.0.1:8000/api/extensions/zapier/verify"
)


@app.post("/ingest")
async def ingest(
    request: Request,
    x_slothpay_relay_token: str | None = Header(default=None),
):
    token = os.environ.get("TASK18_RELAY_TOKEN", "")
    secret = os.environ.get("CONDITION_WEBHOOK_SECRET", "")

    if len(token) < 32 or len(secret) < 32:
        raise HTTPException(503, "Relay is not configured")

    if not x_slothpay_relay_token or not hmac.compare_digest(
        token, x_slothpay_relay_token
    ):
        raise HTTPException(401, "Unauthorized")

    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "Invalid JSON") from exc

    if not isinstance(payload, dict):
        raise HTTPException(400, "Expected JSON object")

    # Only permit our synthetic demonstration record.
    if set(payload) != {"payment_id", "owner_id", "verified"}:
        raise HTTPException(400, "Unexpected payload fields")

    if (
        payload["payment_id"] != TEST_PAYMENT_ID
        or payload["owner_id"] != TEST_OWNER
        or type(payload["verified"]) is not bool
    ):
        raise HTTPException(403, "Only synthetic test events allowed")

    timestamp = int(time.time())
    event_id = "relay_" + uuid.uuid4().hex

    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = sign_event(secret, timestamp, event_id, body)

    headers = {
        "Content-Type": "application/json",
        "X-Slothpay-Timestamp": str(timestamp),
        "X-Slothpay-Event-Id": event_id,
        "X-Slothpay-Signature": signature,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                DESTINATION,
                content=body,
                headers=headers,
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            502, "Local SlothPay backend unreachable"
        ) from exc

    # The synthetic payment must never exist.
    # Treat anything other than not_found as unexpected.
    try:
        result = response.json()
    except ValueError:
        raise HTTPException(502, "Unexpected backend response") from None

    if (
        response.status_code != 404
        or not isinstance(result, dict)
        or result.get("error") != "not_found"
    ):
        raise HTTPException(
            502, "Unexpected payment verification outcome"
        )

    return {
        "relay": "authenticated",
        "signature": "generated",
        "backend_status": response.status_code,
        "payment_result": "not_found",
        "test_only": True,
    }
