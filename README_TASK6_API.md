# Task 6: internal operations API checkpoint

These routes are **admin-only, PayPal Sandbox only**, and not a merchant-facing payment API. They do **not** create a SlothPay payment intent: the payment must already be in the database.

## Install

1. Copy `backend/api/payment_operations.py` and `tests/unit/test_payment_operations_api.py` into your repository on `feat/payment-orchestration`.
2. In `backend/main.py` add `from backend.api.payment_operations import router as payment_operations_router` and, directly after `app = FastAPI(...)`, add `app.include_router(payment_operations_router)`.
3. Set a non-empty, strong `ADMIN_API_KEY` in your uncommitted `.env` file. Never commit credentials.
4. Run tests and Ruff before committing. Sandbox credentials are only needed for actual sandbox API calls.

## Security limitations

- No merchant authorization/multi-tenancy: do NOT expose these routes publicly. Place them behind a private network in addition to the admin secret.
- HTTP cannot supply `condition_verified=True`, because caller flags cannot prove seller fulfillment. Conditional capture is blocked until Task 9's trusted proof engine is wired in.
- No automatic retry after uncertain PayPal outcomes.
- Remaining Task 6 acceptance: provider reconciliation, recovery from STARTED/NOT_SENT attempts, Task 5 reviewed idempotency integration, concurrency edge-case tests, and reviewed end-to-end sandbox test.
