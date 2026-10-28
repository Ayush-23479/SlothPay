# SlothPay Task 7 — Webhook Inbox Starter

Scope: verify PayPal Sandbox webhooks through PayPal REST postback, then durably insert only verified events into `provider_events` with database-enforced deduplication. This does **not** update payment state or execute financial operations. Review carefully before enabling external traffic.

## Integration step

In `backend/main.py`, add the import:

```python
from backend.webhooks.router import router as paypal_webhook_router
```

Immediately after `app = FastAPI(...)` inside `create_app()` add:

```python
app.include_router(paypal_webhook_router)
```

Ensure `PAYPAL_WEBHOOK_ID` in your ignored `.env` matches the actual Sandbox webhook registration. Never commit credentials.

## Run

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/unit/test_paypal_webhook_verification.py
.\.venv\Scripts\python.exe -m ruff check backend/webhooks tests/unit/test_paypal_webhook_verification.py
.\.venv\Scripts\python.exe -m pytest -q -rs
```

Next milestone: test router responses, real PostgreSQL event deduplication with concurrent deliveries, replay safety, and out-of-order processing. Then add **validated** state reconciliation only after Task 6 is complete. Do not merge this starter PR into main prematurely.

Note: PayPal webhook simulator is not supported by the REST postback signature verification API; test with a genuine Sandbox registered webhook or mock the verification service in tests.
