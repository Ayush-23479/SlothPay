# SlothPay Task 2 patch — instructions

This patch **adds** five SQLAlchemy models, a compare-and-swap transition helper, Alembic async configuration, initial PostgreSQL migration with append-only audit trigger, and PostgreSQL integration tests. It does **not** implement PayPal calls or validate payment-state policy rules.

## IMPORTANT: apply only to the existing `feat/database-payment-records` branch

1. From `C:\Users\Lenovo\Desktop\SlothPay`, run `git status`. Preserve/stash unrelated changes first.
2. Extract this ZIP into the repository root, preserving directories. Review files in VS Code before running anything.
3. In existing **untracked** `alembic.ini`, keep `script_location = %(here)s/database/migrations`. The placeholder `sqlalchemy.url = driver://...` is ignored because new `env.py` reads `DATABASE_URL` through `get_settings()`. Do not put passwords into alembic.ini. Stage `alembic.ini` as part of your commit.
4. Check that `.env` DATABASE_URL points to your local Docker PostgreSQL from Windows (`postgresql+asyncpg://...@localhost:5432/...`). If your `DATABASE_URL` uses a different password, leave it as-is; never post it or commit `.env`.
5. **Only on your local development database**, inspect `docker compose ps`, then run:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
$env:SLOTHPAY_RUN_DB_TESTS = "1"
.\.venv\Scripts\python.exe -m pytest -q tests/integration/test_payment_ledger.py
Remove-Item Env:SLOTHPAY_RUN_DB_TESTS
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check backend/models database/migrations tests/integration
```

6. Only after successful checks:

```powershell
git status
git add alembic.ini backend/models database/migrations tests/integration
git diff --cached --stat
git commit -m "feat(database): add payment ledger models migrations and safeguards"
git push origin feat/database-payment-records
```

## Important boundaries

- Existing 11 unit tests do not prove DB behavior. Integration tests require PostgreSQL and opt-in.
- The migration creates/drops these tables, so it MUST NOT be applied to an existing production/shared schema without review. No blind downgrade or `docker compose down -v`.
- Model money is `NUMERIC(18,2)` suitable for the demo's USD/EUR/GBP amounts; currency-specific exponent handling requires a later extension before accepting every supported PayPal currency.
- The append-only trigger blocks ordinary UPDATE/DELETE, not privileged database administrators. True tamper resistance needs stronger storage/audit guarantees.
- State transitions here prevent stale writes, but allowed-state policy and provider reconciliation must still be implemented separately.
