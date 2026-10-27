# Task 4 — SlothPay PayPal Sandbox Gateway

## Scope
`backend/payments/paypal_client.py` provides OAuth, create/get orders, authorize/capture orders, get/capture/void authorizations.

- **Sandbox only**, exact HTTPS hostname enforced.
- Secrets read from existing `backend.config.settings` / `.env`.
- Stable caller-supplied `PayPal-Request-Id` for writes. Store the identifier and operation attempt in PostgreSQL before invoking the gateway.
- **No automatic retries.** Network errors and HTTP 5xx surface as `PayPalOutcomeUnknown`; reconcile before retrying.
- Financial methods are **low-level**. Do not expose them as public routes or call them without verified buyer approval, validated payment state, atomic persisted capture-or-void claim, verified condition, and authorization validity from provider.
- Sandbox OAuth alone does not prove order/capture operations work; use an approved sandbox buyer flow to test that later.

## Apply
Copy this patch into your SlothPay repo on branch `feat/paypal-sandbox-gateway`, after checking `git status` and inspecting for conflicting pre-existing files. No existing repo files need overwriting except an empty placeholder `backend/payments/__init__.py` if you want exports (not required).

## Test
From project root:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/unit/test_paypal_client.py
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check backend/payments/paypal_client.py tests/unit/test_paypal_client.py
```

## Live sandbox smoke test
Avoid making capture or void calls until orchestration controls are complete. OAuth can be tested with your existing verified PowerShell script. To create an order, use a unique, persisted request ID and proceed only with your own sandbox buyer account and test purchase. Do not display the credentials or OAuth token.

## Review task 3
After finishing Task 4, `git fetch origin; git switch review/task3; git merge --ff-only origin/feat/state-machine-policy` and rerun tests. Keep Task 4 and Task 3 branches separate.
