
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

# Make "backend" importable when this file is run as: python scripts/check_paypal.py
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SANDBOX_HOST = "api-m.sandbox.paypal.com"
TIMEOUT = 15.0

results: list[tuple[str, str, str]] = []  # (status, check, detail)


def record(status: str, check: str, detail: str = "") -> None:
    results.append((status, check, detail))
    mark = {"PASS": "[PASS]", "FAIL": "[FAIL]", "SKIP": "[SKIP]", "WARN": "[WARN]"}[status]
    print(f"{mark} {check}" + (f": {detail}" if detail else ""))


def error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    name = body.get("name") or body.get("error") or "error"
    issue = (body.get("details") or [{}])[0].get("issue") or body.get("error_description") or ""
    debug = response.headers.get("paypal-debug-id", "")
    suffix = f" debug_id={debug}" if debug else ""
    return f"HTTP {response.status_code} {name} {issue}{suffix}".strip()


def create_order(
    client: httpx.Client, base: str, token: str, intent: str, currency: str, request_id: str
) -> httpx.Response:
    return client.post(
        f"{base}/v2/checkout/orders",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": request_id,
        },
        json={
            "intent": intent,
            "purchase_units": [
                {
                    "reference_id": "slothpay-check",
                    "amount": {"currency_code": currency, "value": "1.00"},
                }
            ],
        },
    )


def approval_link(order: dict) -> str:
    for link in order.get("links", []):
        if link.get("rel") in ("payer-action", "approve"):
            return link.get("href", "")
    return ""


def run(args: argparse.Namespace, client: httpx.Client, settings) -> int:
    base = settings.PAYPAL_BASE_URL.rstrip("/")
    host = urlparse(base).hostname or ""

    # 0. configuration
    if not settings.PAYPAL_CLIENT_ID or not settings.PAYPAL_CLIENT_SECRET:
        record("FAIL", "Credentials in .env", "PAYPAL_CLIENT_ID or PAYPAL_CLIENT_SECRET is empty")
        return 1
    record("PASS", "Credentials in .env", "client id and secret are set")
    if host == SANDBOX_HOST:
        record("PASS", "Base URL", base)
    else:
        record("FAIL", "Base URL", f"{base} (must be https://{SANDBOX_HOST})")
        return 1

    # 1. token
    response = client.post(
        f"{base}/v1/oauth2/token",
        auth=(settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
    )
    if response.status_code != 200 or "access_token" not in response.json():
        record("FAIL", "1. Access token", error_text(response))
        return 1
    body = response.json()
    token = body["access_token"]
    record(
        "PASS",
        "1. Access token",
        f"valid for about {int(body.get('expires_in', 0)) // 3600} hours "
        f"(app_id {body.get('app_id', '?')})",
    )

    # 2. create a SmartPay-style order (intent CAPTURE)
    request_id = str(uuid.uuid4())
    response = create_order(client, base, token, "CAPTURE", args.currency, request_id)
    if response.status_code not in (200, 201):
        record("FAIL", f"2. Create order (CAPTURE, {args.currency})", error_text(response))
        order = None
    else:
        order = response.json()
        record(
            "PASS",
            f"2. Create order (CAPTURE, {args.currency})",
            f"id {order.get('id')} status {order.get('status')}",
        )

    # 3. read the order back
    if order:
        response = client.get(
            f"{base}/v2/checkout/orders/{order['id']}", headers={"Authorization": f"Bearer {token}"}
        )
        if response.status_code == 200:
            record("PASS", "3. Read order back", f"status {response.json().get('status')}")
        else:
            record("FAIL", "3. Read order back", error_text(response))

        # 4. idempotency: the same PayPal-Request-Id must return the same order
        response = create_order(client, base, token, "CAPTURE", args.currency, request_id)
        if response.status_code in (200, 201) and response.json().get("id") == order["id"]:
            record(
                "PASS",
                "4. Idempotency (same Request-Id, same order)",
                "PayPal returned the original order",
            )
        elif response.status_code in (200, 201):
            record(
                "FAIL",
                "4. Idempotency (same Request-Id, same order)",
                "a DIFFERENT order id came back: duplicate protection layer 2 would not work",
            )
        else:
            record("FAIL", "4. Idempotency (same Request-Id, same order)", error_text(response))

    # 5. ConditionalPay-style order (intent AUTHORIZE)
    response = create_order(client, base, token, "AUTHORIZE", args.currency, str(uuid.uuid4()))
    if response.status_code in (200, 201):
        auth_order = response.json()
        record(
            "PASS",
            f"5. Create order (AUTHORIZE, {args.currency})",
            f"id {auth_order.get('id')} status {auth_order.get('status')}",
        )
    else:
        record("FAIL", f"5. Create order (AUTHORIZE, {args.currency})", error_text(response))
        auth_order = None

    # 6. webhooks
    response = client.get(
        f"{base}/v1/notifications/webhooks", headers={"Authorization": f"Bearer {token}"}
    )
    hooks: list[dict] = []
    if response.status_code == 200:
        hooks = response.json().get("webhooks", [])
        record("PASS", "6. Webhook API access", f"{len(hooks)} webhook(s) registered for this app")
    else:
        record("FAIL", "6. Webhook API access", error_text(response))

    webhook_id = settings.PAYPAL_WEBHOOK_ID
    if not webhook_id:
        record("SKIP", "7. PAYPAL_WEBHOOK_ID", "empty in .env (fine until Task 7)")
    else:
        response = client.get(
            f"{base}/v1/notifications/webhooks/{webhook_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code == 200:
            hook = response.json()
            events = ", ".join(e.get("name", "?") for e in hook.get("event_types", [])) or "none"
            record(
                "PASS", "7. PAYPAL_WEBHOOK_ID exists", f"url {hook.get('url')}; events: {events}"
            )
        else:
            record("FAIL", "7. PAYPAL_WEBHOOK_ID exists", error_text(response))

    record(
        "SKIP",
        "8. Webhook signature verification",
        "needs a real sandbox event; tested in Task 7 (simulator events cannot be verified)",
    )

    # approval links
    links = [
        ("CAPTURE order", approval_link(order or {})),
        ("AUTHORIZE order", approval_link(auth_order or {})),
    ]
    shown = [(label, link) for label, link in links if link]
    if shown:
        print(
            "\nOptional: open a link and log in with your sandbox BUYER account to test approval."
        )
        print("Approving is not required, and nothing is captured by this script.")
        for label, link in shown:
            print(f"  {label}: {link}")

    failed = [r for r in results if r[0] == "FAIL"]
    print(
        "\nSummary: "
        f"{sum(r[0] == 'PASS' for r in results)} passed, {len(failed)} failed, "
        f"{sum(r[0] == 'SKIP' for r in results)} skipped"
    )
    return 1 if failed else 0


def main(argv: list[str] | None = None, client: httpx.Client | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check PayPal sandbox credentials")
    parser.add_argument("--currency", default="USD", help="currency for the test orders (try INR)")
    args = parser.parse_args(argv)
    args.currency = args.currency.upper()

    from backend.config.settings import get_settings

    settings = get_settings()
    owns_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        return run(args, client, settings)
    except httpx.HTTPError as exc:
        record("FAIL", "Network", f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        if owns_client:
            client.close()


if __name__ == "__main__":
    sys.exit(main())