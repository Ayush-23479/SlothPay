# Task 6 lifecycle completion candidate (review required)

This patch **adds** `backend/orchestration/lifecycle.py`, plus offline tests, to the existing `feat/payment-orchestration` branch. It does not overwrite the working `backend/orchestration/service.py` or its 6 original tests. Existing PostgreSQL tests remain valid.

## Features added

- `create_order()` creates a sandbox PayPal order, records `PaymentAttempt`, and persists the order ID, or marks the outcome UNCERTAIN.
- `verify_buyer_approval()` reads PayPal's order via GET and only transitions to APPROVED when the provider matches the stored ID and returns APPROVED.
- `execute_operation()` creates an exclusive compare-and-swap claim and a `PaymentAttempt` **within one commit** before posting an authorization, capture or void.
- Authorize/capture parse **nested** PayPal order responses, record the provider operation identifier and advance safe state transitions.
- ConditionalPay capture requires a fresh provider authorization read, verified condition, and sufficiently distant expiry.
- PayPal network ambiguity and non-definitive results are marked UNCERTAIN, without blind retry.
- Stable request ID fallback derived from payment UUID and operation. **Do not change IDs for any existing attempts.** Task 5's reviewed ID-generation algorithm must replace/align with this fallback before merge.

## Critical integration notes

1. Keep Task 5 under review. The fallback stable ID here is *not* a claim that Task 5 is complete. Review the exact ID scheme before Task 5 integration; preserve already-committed IDs.
2. No API routes are connected to the lifecycle yet. A trustworthy condition evaluator (Task 9) must produce `condition_verified`—never pass a client-provided boolean directly.
3. The caller must invoke each operation using a fresh DB session with no unrelated pending writes. All financial operations are sandbox-only.
4. Successful authorization is recorded as AUTHORIZED, then AWAITING_CONDITION in a distinct transition. A process crash in between remains recoverable.
5. `NOT_SENT`, `STARTED`, and `UNCERTAIN` attempts **must be reconciled**; do not blindly resubmit money operations. This is a prerequisite for merge readiness.
6. New unique order attempt uses the `PaymentAttempt(provider, request_id)` existing constraint. Ensure the `payment_attempts` table is migrated and present.
7. **Required before calling Task 6 complete**: run live-Postgres integration tests for every lifecycle function, concurrent duplicate order creates, crash states, approval spoofing, nested provider responses and migration compatibility; run the full suite. Unit tests in this ZIP are not a substitute for those tests.

## Installation (PowerShell)

```powershell
Set-Location "C:\Users\Lenovo\Desktop\SlothPay"
git switch feat/payment-orchestration
$zip = Join-Path $env:USERPROFILE "Downloads\SlothPay_Task6_Lifecycle_Patch.zip"
Test-Path $zip
Expand-Archive -LiteralPath $zip -DestinationPath . -Force
.\.venv\Scripts\python.exe -m ruff format backend/orchestration/lifecycle.py tests/unit/test_payment_lifecycle.py
.\.venv\Scripts\python.exe -m ruff check backend/orchestration tests/unit/test_payment_lifecycle.py
.\.venv\Scripts\python.exe -m pytest -q tests/unit/test_payment_lifecycle.py tests/unit/test_payment_orchestration.py
$env:SLOTHPAY_RUN_DB_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest -q -rs
```

Do not commit if tests fail. This is a **candidate implementation**, not a certified or fully integrated Task 6 release.
