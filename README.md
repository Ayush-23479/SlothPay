# SlothPay

**Sandbox-only payment orchestration for SmartPay, ConditionalPay, and safer recovery from uncertain provider outcomes.**

SlothPay is a FastAPI/PostgreSQL application built around the PayPal Sandbox. Its goal is to prevent duplicate financial operations and maintain auditable payment state when requests are repeated, a worker crashes, or the provider's response is ambiguous.

> **Project status:** Active development. Database ledger, payment state rules, PayPal Sandbox gateway, and Task 6 orchestration are implemented and locally tested. Task 5 idempotency review, provider-state reconciliation, full Sandbox acceptance, and further workflow integration remain open. **Not for live payments.**

## Payment flows

| Flow | Intended behavior | Current boundary |
| --- | --- | --- |
| **SmartPay** | Create a PayPal order, independently verify buyer approval, then capture | Orchestration and internal API implemented; end-to-end Sandbox verification pending |
| **ConditionalPay** | Create an authorization order, verify approval, authorize, then capture or void when the applicable condition is trusted | Authorization and guarded operations implemented; HTTP condition-based capture remains blocked pending trusted condition evaluation |

## Safety model

- **PayPal Sandbox only.** The gateway is designed for the sandbox endpoint, not live financial transactions.
- **Durable operation attempts.** A `PaymentAttempt` and an exclusive payment-state claim are committed before financial operations are sent to PayPal.
- **Concurrency control.** PostgreSQL compare-and-swap state transitions and unique request IDs help ensure competing operations cannot both execute.
- **Evidence-backed approval.** Buyer approval comes from PayPal's order details, not a frontend flag.
- **No blind charge retries.** `STARTED`, `NOT_SENT`, or `UNCERTAIN` attempts require investigation and provider-state reconciliation before any further action.
- **Append-only audit history.** Database-side protection prevents normal UPDATE and DELETE against the payment audit ledger.
- **Limited internal API.** Existing Task 6 endpoints are admin-only development endpoints; they do not provide merchant authentication or a public production-ready API.

**Important:** Stable request-ID behavior is currently implemented within the orchestration layer; the final Task 5 duplicate-protection contract is still under review. Do not change request IDs associated with previously recorded attempts.

## Tech stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy async, Alembic
- **Data stores:** PostgreSQL 16, Redis 7
- **Payment provider:** PayPal REST APIs (Sandbox)
- **Frontend:** Next.js
- **Planned / in-progress:** Temporal workflows, trusted condition verification, webhooks, reconciliation and AI-assisted analysis
- **Testing:** pytest, pytest-asyncio, Ruff

## Local setup (Windows PowerShell)

Prerequisites: Python 3.12, Docker Desktop, Node.js/npm, and (for PayPal API calls) PayPal Sandbox app credentials.

```powershell
# From the repository root
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env    # edit .env locally; never commit it

docker compose up -d postgres redis
.\.venv\Scripts\python.exe -m alembic upgrade head

# Run backend in another terminal
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload
```

The health endpoint is `http://localhost:8000/health`. Your `.env` must use the local sandbox configuration. The admin-only Task 6 routes additionally require a strong, private `ADMIN_API_KEY`. Keep these routes on a private network.

Start the frontend separately:

```powershell
Set-Location frontend
npm install
npm run dev
```

Open `http://localhost:3000`.

## Tests

Run integration tests **only against a disposable, migrated local PostgreSQL database**. Some integration tests commit rows and do not clean them up automatically.

```powershell
$env:SLOTHPAY_RUN_DB_TESTS = "1"
.\.venv\Scripts\python.exe -m pytest -q -rs tests/unit tests/integration
.\.venv\Scripts\python.exe -m ruff check backend tests
Remove-Item Env:SLOTHPAY_RUN_DB_TESTS -ErrorAction SilentlyContinue
```

**Last verified result (Task 6 branch, October 2026):** 241 unit/integration tests passed with one third-party Starlette deprecation warning; Ruff passed. This result does not establish live PayPal Sandbox end-to-end acceptance.

## How to run the tests

Everything below runs from the repository root with the virtual environment active. Integration tests create and drop their own throw-away databases, but they need a **local** PostgreSQL (and Redis for the Compose stack). They refuse to run against a remote database.

| What | Command |
| --- | --- |
| Unit tests (fast, no services) | `.\.venv\Scripts\python.exe -m pytest -q tests/unit` |
| Integration tests, including the Task 15 scenarios | `$env:SLOTHPAY_RUN_DB_TESTS = "1"` then `.\.venv\Scripts\python.exe -m pytest -q -rs tests/integration` |
| One Task 15 scenario file | `.\.venv\Scripts\python.exe -m pytest -q tests/integration/test_lost_answer.py` |
| Stress run against the fake PayPal (100 payments) | `.\.venv\Scripts\python.exe scripts/stress_fake.py` |
| Same, different seed or size | `.\.venv\Scripts\python.exe scripts/stress_fake.py --payments 300 --seed 7` |
| Real sandbox, human approves the buyer step | `$env:SLOTHPAY_RUN_SANDBOX = "1"` then `.\.venv\Scripts\python.exe -m pytest -s tests/e2e/test_sandbox_manual.py` |
| Lint | `.\.venv\Scripts\python.exe -m ruff check backend tests scripts` |
| Frontend | `cd frontend`, then `npm run lint` and `npm run build` |

The `SLOTHPAY_RUN_*` variables apply only to the current PowerShell window; clear them with `Remove-Item Env:SLOTHPAY_RUN_DB_TESTS`. Temporal tests are skipped unless `TEMPORAL_TEST_ADDRESS` points at a running dev server, and a skipped test is not a passed test.

**The fake PayPal (`tests/fake_paypal/app.py`).** A small FastAPI app that mimics only the PayPal endpoints SlothPay uses. It replays the stored answer for a repeated `PayPal-Request-Id`, supports the order states CREATED, APPROVED, COMPLETED and VOIDED, can delay, drop an answer after the work is done, or return 503 at random from a fixed seed, and reports counts of orders and captures at `GET /_stats`. The real `PayPalClient` talks to it in-process through `httpx.ASGITransport`. `PAYPAL_BASE_URL` is **not** used, because the client refuses every URL except the exact PayPal sandbox host and that safeguard stays on.

**Reading the results honestly.** Anything produced with the fake PayPal is a *simulator* result and says so in its output. It shows that SlothPay's own logic does not duplicate payments. It says nothing about the real PayPal sandbox. The manual sandbox test is the only place that reports real sandbox behaviour, and it needs a person to approve the payment.

## Security checks

```bash
python scripts/scan_secrets.py     # secrets scan, exit 1 on a hit
```

See `docs/TASK16_SECURITY.md` for what is enforced and the deployment caveats. After pulling Task 16 run `alembic upgrade head` once on your local database.

## Repository layout

| Path | Responsibility |
| --- | --- |
| `backend/config` | Environment-based settings and sandbox safeguards |
| `backend/models` | Payments, operation attempts, provider events, and audit ledger |
| `backend/state_machine` | States, policy rules, and transition guards |
| `backend/payments` | Low-level PayPal Sandbox client |
| `backend/orchestration` | SmartPay, ConditionalPay, claims, lifecycle, and recovery inspection |
| `backend/api` | Internal HTTP endpoints |
| `backend/webhooks` | PayPal webhook handling (on its dedicated feature branch until merged) |
| `backend/reliability` | Idempotency and reconciliation work |
| `backend/workflows` | Planned Temporal workflows |
| `database` | Alembic migrations and SQL |
| `frontend` | Next.js application |
| `tests` | Unit and PostgreSQL integration tests |

## Development progress

| Area | Status |
| --- | --- |
| Payment ledger and migrations | Implemented and tested |
| State transition rules | Implemented and tested |
| PayPal Sandbox API client | Implemented with offline tests |
| SmartPay / ConditionalPay lifecycle | Implemented and locally tested |
| Task 6 internal operations API | Implemented with unit tests |
| PostgreSQL concurrency and no-replay tests | Passing |
| Recovery inspection of unresolved attempts | Read-only implementation tested |
| Task 5 final idempotency integration | Pending review |
| Provider-state reconciliation and release of stuck operations | Pending |
| Trusted conditional settlement workflow | Pending |
| Real Sandbox end-to-end acceptance | Pending |

## Security and operational limits

This repository is **not a production payment service**. Never commit `.env`, PayPal credentials, access tokens, or admin keys. Never expose internal admin endpoints directly to the public internet. Do not automatically retry an ambiguous capture or assume that a local timeout means PayPal did not process it. Preserve historical payment-attempt IDs and audit records during future integrations.

## License

See [LICENSE](LICENSE), if present in the repository.