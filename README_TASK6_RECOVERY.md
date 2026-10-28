# Task 6 — Recovery safety milestone

Adds a **read-only** unresolved payment-attempt inspector and four PostgreSQL integration tests for duplicate/order concurrency and interrupted `STARTED` attempts. No automatic money operation retry and no automatic state reconciliation are introduced.

## Run

On `feat/payment-orchestration`, extract this ZIP into the SlothPay repository root. Use a disposable **local, migrated PostgreSQL test database**. These tests commit rows and do not remove them.

```powershell
$env:SLOTHPAY_RUN_DB_TESTS = "1"
.\.venv\Scripts\python.exe -m ruff format backend/orchestration/recovery.py tests/integration/test_payment_recovery_safety.py
.\.venv\Scripts\python.exe -m ruff check backend tests
.\.venv\Scripts\python.exe -m pytest -v -rs tests/integration/test_payment_recovery_safety.py
.\.venv\Scripts\python.exe -m pytest -q -rs tests/unit tests/integration
```

## Important limitations

* Passing these tests proves current **no-replay** behavior on these crash/concurrency cases, not a complete recovery system.
* Task 6 still needs verified provider-state reconciliation and a reviewed, auditable mechanism to advance `UNCERTAIN` / `STARTED` / `NOT_SENT` attempts. Never blindly retry POST operations.
* Do not replace Task 5's reviewed stable idempotency contract for existing attempts.
* Run PayPal Sandbox end-to-end checks before final approval.
