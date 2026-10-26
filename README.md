# SlothPay

AI-powered payment orchestration, conditional payments and safe transaction recovery on top of PayPal (**sandbox only**).

SlothPay makes a payment safe even when the internet freezes, the buyer taps twice, or PayPal's answer gets lost. It has two payment paths that never run one after another for the same payment:

- **SmartPay:** the buyer approves, PayPal captures immediately.
- **ConditionalPay:** the buyer approves, PayPal authorizes (holds) the money, SlothPay waits for a verified condition, then captures or voids.

Duplicate protection has two layers: our own idempotency key, and a stable PayPal-Request-Id so repeating a request returns the original result. SlothPay never blindly retries a charge after a timeout; it asks PayPal what happened first.

> Status: foundation only (Task 1). Payment logic is added task by task.

## Prerequisites

- Python 3.12
- Node.js (current LTS) and npm
- Docker Desktop (for PostgreSQL, Redis and Temporal)
- A free PayPal developer account with a sandbox app (developer.paypal.com)

## Quick start (backend, no Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # then edit .env
python -m pytest
uvicorn backend.main:app --reload
# in another terminal:
curl http://localhost:8000/health
```

## Run the whole local stack with Docker Compose

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
curl http://localhost:8000/health
```

Services: PostgreSQL 16 (5432), Redis 7 (6379), Temporal dev server (7233, UI on 8233), API (8000).

## Frontend

```bash
cd frontend
npm install
npm run build
npm run dev      # http://localhost:3000
```

## Folder map

| Folder | Purpose |
| --- | --- |
| backend/config | Settings and configuration |
| backend/models | Database models and repository (Task 2) |
| backend/state_machine | Payment states and rules (Task 3) |
| backend/payments | PayPal gateway (Task 4) |
| backend/reliability | Duplicate protection, reconciliation, rescue (Tasks 5, 8, 12) |
| backend/orchestration | SmartPay and ConditionalPay flows (Task 6) |
| backend/webhooks | PayPal webhooks (Task 7) |
| backend/workflows | Temporal workflows (Task 9) |
| backend/api | HTTP routes (Task 10) |
| backend/notifications | Live updates over SSE (Task 10) |
| backend/intelligence | AI analysis with fallbacks (Task 11) |
| database | Migrations and SQL views |
| frontend | Next.js dashboard and Trust Passport |
| tests | Unit, integration and end-to-end tests |
| docs | Architecture and submission docs |

## Safety notes

- PayPal sandbox only. The app refuses to start in demo or production mode with a non-sandbox PayPal URL.
- Never commit secrets. Only `.env.example` is committed.
- Controlled-failure tools (chaos) are off by default.

## License

MIT, see [LICENSE](LICENSE).
