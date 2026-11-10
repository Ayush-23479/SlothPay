# Deploying the SlothPay demo (Render, sandbox only)

This puts the dashboard and the API on the internet so a judge can open a link and make a PayPal
**sandbox** payment. No real money moves: the app refuses to start in `demo` mode with any
PayPal address that is not `*.sandbox.paypal.com`.

`render.yaml` in the repo root describes everything. Render calls that file a Blueprint.

## What gets created

| Piece | Render type | Plan | Notes |
| --- | --- | --- | --- |
| `slothpay-db` | PostgreSQL | free | Payments and the audit trail. Expires 30 days after creation. |
| `slothpay-redis` | Key Value | free | Duplicate lock, rate limits, live-update fan-out. Empties on restart. |
| `slothpay-api` | Web service (Docker) | free | FastAPI. Runs migrations on start. Also runs the two small worker loops. |
| `slothpay-web` | Web service (Docker) | free | Next.js dashboard. Forwards `/api/...` to the API. |

## Free-plan limits that affect the demo

Checked against Render's free-plan page. Check it again before you submit, because plans change.

1. **Sleep.** A free web service that gets no traffic for 15 minutes is spun down. The next visit
   takes about a minute to wake it. The dashboard also wakes the API, so the first payment after a
   quiet period can be slow. Tell judges in the submission (see `docs/submission.md`).
2. **Monthly hours.** Each workspace gets 750 free instance hours per month, shared by every free
   web service. Two services awake all day and night use about 1,490 hours, so keeping both awake
   with a "pinger" would use up the hours around the middle of the month, and Render then
   **suspends all free web services until the next month**. So do not run a keep-alive pinger.
   Letting the services sleep when nobody is using them is what keeps the demo available all month.
   If you need it always awake, put the API on a paid plan.
3. **Database expiry.** A free PostgreSQL database expires 30 days after it is created. Render
   then gives 14 days to upgrade, during which the database cannot be reached, and then deletes
   it and its data. See "Keep the demo alive until judging ends" below.
4. **Key Value.** The free instance does not save its state. A restart empties it. That clears
   the duplicate-protection locks (the database still holds the permanent record and its unique
   constraint, so a repeated key is still caught) and the rate-limit counters.
5. **No workers.** Render's free plan covers web services, PostgreSQL, Key Value and static sites
   only. Nothing here can run as a background worker or cron job, which is why two of the three
   worker loops run inside the API (next section).

## Temporal: the decision for the hosted demo

**Decision: the hosted demo does not run Temporal.** Reasons:

- Render has no free background workers, so the Temporal worker would need a paid instance.
- Temporal needs a server the worker can reach. A hosted one (Temporal Cloud) needs an account,
  and the worker's connection code (`backend/workflows/worker.py`) only connects to a plain
  address. It has no API-key or TLS settings for Temporal Cloud yet.
- The plan's other fallback (a due-time column and a loop inside the API, Task 9) was not built.
  The Temporal workflows were built instead, so there is no automatic deadline timer without
  Temporal.

What still works without Temporal:

- SmartPay, duplicate protection, webhooks, lost-answer recovery, the Trust Passport and the
  dashboard.
- ConditionalPay: the buyer approves, the funds are **authorized** (held), and the payment waits
  for the condition. The owner presses **Verify: condition met** (capture) or **Fail: condition
  not met** (void) in the payment's details. Silence never captures money: nothing is captured
  unless a condition is verified.
- Starting the condition timer fails, and the failure is written to the audit trail as
  `CONDITION_WORKFLOW_NOT_STARTED`, so it is visible and not silent.

What does not work without Temporal: the **automatic expiry**. A held payment whose deadline
passes is not voided by itself. It stays `AWAITING_CONDITION` until someone verifies or cancels
it, or until PayPal lets the authorization expire on its own. In the hosted demo, show ConditionalPay
with those two buttons. Show automatic expiry from the local Docker Compose
stack, where Temporal runs (see the README, "Quick start").

To change this decision later: pay for a worker, run or rent a Temporal server, add its
connection settings to `backend/workflows/worker.py`, and uncomment the `worker` block at the
bottom of `render.yaml`.

## The two worker loops that do run in the API

Locally, Docker Compose runs two small loops as their own containers (the `workers` profile):

- the **recovery worker** asks PayPal what happened to payments whose outcome was unknown, and
- the **webhook worker** applies verified PayPal webhook events from the inbox.

On Render's free plan there is nowhere to run them, so `RUN_BACKGROUND_WORKERS=true` starts the
same two functions inside the API process (`backend/orchestration/inprocess_workers.py`). They
use exactly the code the separate workers use. Remember that when the API is asleep the loops are
asleep too, and they catch up when it wakes. Use one API instance only.

## Steps

### 1. Put the code on GitHub

Render deploys from a GitHub repository. Merge the Task 17 branch (or deploy from the branch).
Check that no secret is committed: `python scripts/scan_secrets.py`.

### 2. Create the Blueprint

1. In the Render dashboard choose **New**, then **Blueprint**, and pick the repository.
2. Render reads `render.yaml` and lists the four resources. It asks for the values marked
   `sync: false`:
   - `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`: the **Sandbox** app credentials from
     developer.paypal.com, Apps & Credentials, Sandbox.
   - `PAYPAL_WEBHOOK_ID`: leave empty for now. You get it in step 4.
   - `SENTRY_DSN`, `GEMMA_BASE_URL`, `GEMMA_API_KEY`, `GEMMA_MODEL`: optional. Leave empty and the
     app runs without them (the AI falls back to fixed text).
3. Apply. The first build of each service takes a few minutes.

`PASSPORT_SIGNING_SECRET` and `ADMIN_API_KEY` are generated by Render. You never paste them.

### 3. Check the two addresses

Render names services `slothpay-api` and `slothpay-web`, so the addresses are
`https://slothpay-api.onrender.com` and `https://slothpay-web.onrender.com`. If either name was
already taken, Render adds a suffix. Open each service and read its real address. If an address
differs from what `render.yaml` assumed:

- on `slothpay-api`, set `FRONTEND_ORIGIN` to the real dashboard address (PayPal sends the buyer
  back there), and
- on `slothpay-web`, set `BACKEND_URL` to the real API address. This value is built into the
  dashboard, so Render rebuilds it.

Redeploy both. Do not add a trailing slash.

### 4. Register the PayPal webhook

1. developer.paypal.com, Apps & Credentials, open your **Sandbox** app, **Webhooks**, add a webhook.
2. URL: `https://<your-api-address>/webhooks/paypal`. This goes to the API, not the dashboard.
3. Events: `CHECKOUT.ORDER.APPROVED`, `PAYMENT.AUTHORIZATION.CREATED`,
   `PAYMENT.AUTHORIZATION.VOIDED`, `PAYMENT.CAPTURE.PENDING`, `PAYMENT.CAPTURE.COMPLETED`,
   `PAYMENT.CAPTURE.DENIED`. These are the events SlothPay understands
   (`backend/webhooks/events_map.py`).
4. Copy the **Webhook ID** into `PAYPAL_WEBHOOK_ID` on `slothpay-api` and redeploy it. Without it
   every webhook is rejected, which is the safe behaviour.

### 5. Check the hosted demo

1. `https://<api-address>/health` answers `{"status":"ok","env":"demo"}` (wait a minute if it was asleep).
2. `https://<api-address>/docs` answers 404. That is correct: the API explorer is off in demo.
3. Open the dashboard. The page loads and the cards fill in (there are no red connection errors).
4. Make a SmartPay payment of a small amount and approve it with the sandbox **buyer** account
   (developer.paypal.com, Testing tools, Sandbox accounts, the Personal account). It ends as
   `COMPLETED` and the Trust Passport shows a confirmed capture.
5. In the PayPal dashboard, Webhooks, check that deliveries to your URL show `200`.

## Settings used in the hosted demo

| Setting | Value | Why |
| --- | --- | --- |
| `ENV` | `demo` | Turns on the safety guards (sandbox-only host, strong secrets, no chaos, secure cookies, no API docs, no `X-Owner-Id` header). |
| `CHAOS_ENABLED` | `false` | The "Break it on purpose" panel is local only. See below. |
| `RUN_BACKGROUND_WORKERS` | `true` | Runs the recovery and webhook loops in the API. |
| `RATE_LIMIT_LOGIN_PER_MINUTE`, `RATE_LIMIT_PAYMENTS_PER_MINUTE` | `120`, `90` | Behind Render's proxy visitors can share an address, so the limits are looser than the local defaults. |
| `BACKEND_URL` (web, build time) | the API address | The dashboard forwards `/api/...` there. |
| `NEXT_PUBLIC_API_BASE_URL` (web, build time) | empty | The browser calls its own site, not the API. |

### Why the dashboard forwards `/api`

The two services have different `*.onrender.com` addresses, and browsers treat those as different
sites. The login cookie is `SameSite=Lax`, so a browser would not send it from the dashboard to
the API, and nothing would load. Forwarding `/api/...` through the dashboard (`frontend/next.config.ts`)
makes the cookie belong to the dashboard's own address. PayPal's webhook still goes straight to
the API.

### Chaos mode on the hosted demo

Chaos mode lets anyone cause failures (lost answers, slow calls, declines) on purpose. It is
refused in `demo` mode. If you want judges to use it on the hosted site, switch `ENV` to
`sandbox`, set `CHAOS_ENABLED=true`, and set `NEXT_PUBLIC_ADMIN_API_KEY` on `slothpay-web` to the
same value as `ADMIN_API_KEY`. Be aware that:

- `NEXT_PUBLIC_*` values are visible to everyone, so anyone could use the key,
- `sandbox` does not enforce the strong-secret checks that `demo` does, and
- chaos affects only calls to the PayPal sandbox.

The safer default is chaos off on the hosted site and chaos shown in the video and locally.

## Keep the demo alive until judging ends

The free database expires 30 days after it is created. Write down the creation date now:

| Item | Date |
| --- | --- |
| Database created | (fill in) |
| Database expires (created + 30 days) | (fill in) |

Example: a database created on 10 November expires on 10 December, five days before judging
ends, so it needs upgrading or recreating by about 3 December.
| Last day of judging | 15 December |

If the database would expire before judging ends, do one of these **before** the expiry date:

- **Upgrade** the database to a paid plan in the Render dashboard (keeps the data), or
- **Recreate** it: delete the free database and create a new one. Only one free database can be
  active per workspace, so delete first. You lose the payments in it. Point `DATABASE_URL` at the
  new one and redeploy the API: it runs the migrations on start and creates empty tables.

Put the date in your calendar, a week early. An expired database cannot be reached, so the demo
would show errors.

Also keep the sandbox credentials the same. If you rotate the PayPal secret, update it on
`slothpay-api` and redeploy.

## If something is wrong

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| API deploy fails with "Refusing to start" | A safety guard. The log says which one. | Fix the named setting (PayPal host, secret length, `CHAOS_ENABLED`). |
| Dashboard loads but cards stay grey | `BACKEND_URL` points to the wrong address, or the API is waking up. | Check the API address, then redeploy the web service. Wait a minute if it was asleep. |
| Login works but the next call is 401 | The dashboard is calling the API directly (the build did not get `BACKEND_URL`). | Set `BACKEND_URL` on `slothpay-web` and redeploy. |
| Buyer returns from PayPal to a wrong page | `FRONTEND_ORIGIN` on the API is wrong. | Set it to the real dashboard address. |
| Webhooks never apply | Wrong `PAYPAL_WEBHOOK_ID`, or the webhook URL points at the dashboard. | Use the API address and the ID shown for that webhook. |
| "Payments are temporarily unavailable" | The database or Key Value store is not reachable. | Check both resources in Render. |
| Everything is down for the rest of the month | The 750 free hours were used. | Wait for the month to reset, or upgrade the API. Do not run a pinger. |

## Task 18: Signed condition webhooks

Set `CONDITION_WEBHOOK_SECRET` to a random secret of at least 32 characters.
Generate it with `python -c 'import secrets; print(secrets.token_hex(32))'`.
Store the value privately in your local environment or deployment secrets.
The Render Blueprint generates the secret for new deployments.

Endpoint: `POST /api/extensions/zapier/verify`.
When the secret is missing or shorter than 32 characters, the endpoint returns HTTP 503.
The caller must authenticate with the custom HMAC-SHA256 signature.
The signature covers the timestamp, event ID, and exact request-body bytes.
This is not a native Zapier signature.
Only a trusted signer that independently verifies the condition may authorize a payment.
The synthetic-only local relay must not be publicly exposed.

Replay reservations last seven days. A processing failure may retain a reservation.
An operator must investigate uncertain outcomes before permitting retries.
