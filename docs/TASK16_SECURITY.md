# Task 16: security and privacy hardening

## What is enforced now

| Area | Rule | Where | Test |
| --- | --- | --- | --- |
| Identity | Signed, expiring session cookie (HMAC-SHA256, HttpOnly, SameSite=Lax, Secure when ENV is not local) issued by `POST /api/demo-login`. The `X-Owner-Id` header only works when `ENV=local`. | `backend/api/security.py`, `backend/api/deps.py` | `tests/unit/test_security.py` |
| Ownership | Another owner's payment is a 404 identical to a missing one. `/api/metrics` now needs a session. | `backend/api/deps.py`, `routes_metrics.py` | `test_security.py`, `test_api.py` |
| Rate limits | Redis fixed window per client address: payments 30/min, webhook 300/min, login 20/min. 429 with `Retry-After`. Fails open if Redis is down. | `RateLimitMiddleware` | `test_security.py` |
| Request size | 64 KB body cap (webhook route 256 KB), enforced for declared and streamed bodies. | `BodySizeLimitMiddleware` | `test_security.py` |
| Headers | nosniff, frame deny, no-referrer, CSP `default-src 'none'`, permissions policy, `no-store` on API routes, HSTS outside local. | `SecurityHeadersMiddleware` | `test_security.py` |
| CORS | One exact origin (`FRONTEND_ORIGIN`), credentials allowed, never a wildcard. | `backend/main.py` | `test_security.py` |
| Startup | Refuses to start in demo or production if `PAYPAL_BASE_URL` is not an https host under `.sandbox.paypal.com`, if `ADMIN_API_KEY` or `PASSPORT_SIGNING_SECRET` is shorter than 32 characters or looks like a placeholder, or if `CHAOS_ENABLED` is true outside local and sandbox. Unknown `ENV` names are refused. | `backend/config/settings.py` | `test_security.py`, `test_settings.py` |
| Docs | `/docs`, `/redoc`, `/openapi.json` are off in demo and production. | `backend/main.py` | |
| Secrets | `python scripts/scan_secrets.py` exits 1 on PayPal secrets or tokens, bearer tokens, API keys, private keys, secret-looking assignments, and untracked/ignored `.env` problems. Never prints a secret. | `scripts/scan_secrets.py` | `test_security.py` |
| Privacy | PayPal payloads are masked before storage (emails, names, addresses, phones). Sentry events lose emails, bearer tokens, cookies, bodies and users. | `webhooks/events_map.py`, `config/observability.py` | `test_security.py` |
| Webhooks | The exact raw bytes are verified. Failures return fixed text, never the reason. | `webhooks/receiver.py` | `test_security.py` |
| Audit trail | UPDATE and DELETE were blocked per row. TRUNCATE (including `TRUNCATE payments CASCADE`) is now blocked by migration `0006_audit_block_truncate`. | `database/migrations/versions/0006_*.py` | `tests/integration/test_audit_trail_db.py` |

## Run it

```bash
python scripts/scan_secrets.py
python -m ruff check backend tests scripts database
python -m pytest -q tests/unit
SLOTHPAY_RUN_DB_TESTS=1 python -m pytest -q tests/unit tests/integration
alembic upgrade head        # applies migration 0006 to your Compose database
```

## Caveats I could not verify (read before deploying in Task 17)

1. **Cross-site cookies.** The cookie is `SameSite=Lax`, as the plan says. If the frontend and the API are on different registrable domains in production (for example two Render hostnames), browsers will not send the cookie on fetch calls. Put both behind one domain, or proxy `/api` through the frontend, or change the cookie to `SameSite=None; Secure`. Not tested against a real deployment.
2. **Client address behind a proxy.** Rate limits use the connection address. Behind Render or any proxy that is the proxy's address, so every visitor would share one budget. Start uvicorn with `--proxy-headers` and set `FORWARDED_ALLOW_IPS` to the proxy range before relying on the limits. Not tested.
3. **Dependencies.** `requirements.txt` already pins exact versions. I could not run `pip-audit` or `npm audit` from the build sandbox (no access to the advisory databases), so known vulnerabilities in those versions are unchecked. Run both before submission.
4. **Secrets in git history.** The scan reads the working tree only. It does not search old commits. If a secret was ever committed, rotate it.
5. **Session secret.** Sessions and Trust Passport links share `PASSPORT_SIGNING_SECRET`, separated by a context prefix so one cannot be used as the other. Changing the secret signs everyone out and invalidates every share link.
6. **Sentry.** Scrubbing is tested against synthetic events, not a live Sentry project.
7. **Rate limit on failure.** If Redis is down the limiter allows requests (and logs a warning) instead of blocking payments.