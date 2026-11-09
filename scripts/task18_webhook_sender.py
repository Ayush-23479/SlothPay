"""Task 18 local signed-webhook sender and validation utility.

Dry-run by default. No payment API request unless --send is given.
--send is restricted to localhost.
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.extensions.signed_conditions import sign_event, verify_event  # noqa: E402


def build_event(secret, payment_id, owner_id, verified):
    timestamp = int(time.time())
    event_id = "task18_" + uuid.uuid4().hex

    body = json.dumps(
        {
            "payment_id": str(payment_id),
            "owner_id": owner_id,
            "verified": verified,
        },
        separators=(",", ":"),
    ).encode("utf-8")

    signature = sign_event(secret, timestamp, event_id, body)

    headers = {
        "Content-Type": "application/json",
        "X-Slothpay-Timestamp": str(timestamp),
        "X-Slothpay-Event-Id": event_id,
        "X-Slothpay-Signature": signature,
    }

    return body, headers


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payment-id", default=None)
    parser.add_argument("--owner-id", default="task18_demo")
    parser.add_argument("--verified", choices=["true", "false"], default="true")
    parser.add_argument("--url", default="http://127.0.0.1:8000/api/extensions/zapier/verify")
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()

    secret = os.environ.get("CONDITION_WEBHOOK_SECRET", "")

    if len(secret) < 32:
        raise SystemExit("Set CONDITION_WEBHOOK_SECRET to at least 32 characters.")

    payment_id = uuid.UUID(args.payment_id) if args.payment_id else uuid.uuid4()

    body, headers = build_event(
        secret,
        payment_id,
        args.owner_id,
        args.verified == "true",
    )

    checked = verify_event(
        secret=secret,
        timestamp=headers["X-Slothpay-Timestamp"],
        event_id=headers["X-Slothpay-Event-Id"],
        signature=headers["X-Slothpay-Signature"],
        raw_body=body,
    )

    assert checked.payment_id == payment_id
    assert checked.verified == (args.verified == "true")

    print("TASK 18 SIGNED WEBHOOK")
    print("Local signature verification: PASS")
    print("Event ID:", headers["X-Slothpay-Event-Id"])
    print("Payment ID:", payment_id)
    print("Payload:", body.decode("utf-8"))
    print("Signing secret and signature: [hidden]")

    if not args.send:
        print("DRY RUN: No HTTP request made.")
        return

    parsed = urlparse(args.url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
        raise SystemExit("Sending is restricted to local HTTP endpoints.")

    if parsed.port != 8000 or parsed.path != "/api/extensions/zapier/verify":
        raise SystemExit("Only the local Task 18 endpoint on port 8000 is allowed.")

    request = Request(
        args.url,
        data=body,
        headers=headers,
        method="POST",
    )

    try:
        with urlopen(request, timeout=10) as response:
            print("HTTP status:", response.status)
            print("Response:", response.read().decode("utf-8"))
    except HTTPError as exc:
        print("HTTP status:", exc.code)
        print("Response:", exc.read().decode("utf-8"))
    except URLError as exc:
        print("Connection error:", exc.reason)


if __name__ == "__main__":
    main()
