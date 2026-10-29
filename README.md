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
