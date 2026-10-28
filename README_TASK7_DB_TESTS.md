# Task 7 — Verified webhook inbox PostgreSQL acceptance

This patch adds **integration tests only**; it does not change payment states or make outbound PayPal financial calls.

Run on `feat/paypal-webhooks` with a **disposable, migrated local PostgreSQL database**. The tests commit uniquely identified event rows; do not run them against any shared or production DB. PayPal verification is mocked so no credentials/network are required.

```powershell
$env:SLOTHPAY_RUN_DB_TESTS = '1'
.\.venv\Scripts\python.exe -m ruff format tests/integration/test_paypal_webhook_inbox_db.py
.\.venv\Scripts\python.exe -m ruff check backend tests
.\.venv\Scripts\python.exe -m pytest -v -rs tests/integration/test_paypal_webhook_inbox_db.py
.\.venv\Scripts\python.exe -m pytest -q -rs tests/unit tests/integration
```

Acceptance: verified delivery committed; repeat and concurrent duplicate delivery deduped; invalid signature and verification outage unacknowledged; malformed payload rejected; database outage not acknowledged; no payment state updated by inbox ingestion.

**Still outstanding in Task 7:** provider-to-payment matching, state-machine/audit application after independent verification, webhook out-of-order processing, retries and real Sandbox webhook delivery. Do not merge Task 7 solely on these tests.
