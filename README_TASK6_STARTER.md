# Task 6 — first safety milestone (NOT the complete task)

This starter creates `backend/orchestration/service.py`. It implements a claim-before-execute core using your existing Task 2, 3, and 4 interfaces. It deliberately does **not** create orders or expose API endpoints. Task 5 is not yet merged, so callers must supply stable request IDs; do not expose this method to external requests before integration.

**Important limitations to resolve before PR/merge:**
- Persist payment attempts, PayPal order/authorization IDs and reconcile unknown responses.
- Parse PayPal Orders v2 response wrappers safely instead of using a top-level status shortcut.
- Prove DB claim commit precedes network call in integration tests; concurrent losing worker never calls PayPal.
- Validate buyer approval and condition verification server-side (never take booleans from UI).
- The generic `PayPalError` handling needs classification rather than treating all errors as failure/success.
- Need full SmartPay create/order/approve and ConditionalPay authorize/wait/settle flows.
- Real PayPal authorization field compatibility remains unverified.
- Never use this starter to execute financial operations until those boundaries are completed and tested.

## Install

On `feat/payment-orchestration`, extract the ZIP into the SlothPay root. Inspect `git diff`, then run:

```powershell
.\.venv\Scripts\python.exe -m ruff check backend/orchestration
.\.venv\Scripts\python.exe -m pytest -q
```

No need to create `tests/unit/test_payment_orchestration.py` yet. The next milestone is to build an isolated fake session and gateway tests to verify the claim-before-network-call invariant.

## New offline tests

`tests/unit/test_payment_orchestration.py` verifies the atomic-claim ordering, stale-claim blocking, conditional expiry/condition guards, uncertain outcomes and void flow with mocked provider calls. These tests do **not** prove real DB concurrency or complete order lifecycle.

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/unit/test_payment_orchestration.py
```
