# Submission pack

Deadline: 12 November 2026, 12:00 pm PT (1:30 am IST on 13 November). Submit by **11 November**.
Keep the hosted demo free and available until **15 December**.

## Checklist

- [ ] Hosted demo link works (open it from a private window; allow about a minute for the first load).
- [ ] Public GitHub repository link; the license shows in the repo's About section; no secrets
      (`python scripts/scan_secrets.py` is clean).
- [ ] Demo video: public on YouTube, under 3 minutes, no third-party logos or copyrighted music.
- [ ] Text description (below).
- [ ] AI tools list (below), edited to be true.
- [ ] Sandbox test instructions (below), including the demo login.
- [ ] Database expiry date in the calendar (`docs/deploy.md`).

## Links to paste into the form

| Item | Value |
| --- | --- |
| Hosted demo | `<HOSTED_URL>` |
| Code repository | `<REPO_URL>` |
| Demo video (YouTube) | `<YOUTUBE_URL>` |

## Tagline

A safety layer for PayPal payments whose answer gets lost: no double charge, no frozen money.

## Text description

SlothPay is a payment-safety layer for PayPal. It stops the classic failure where a payment's
answer is lost and the buyer pays twice or money is left frozen. Every payment has one stable
identity, every PayPal call carries a stable request id, and a move is claimed in the database
before PayPal is called. If an answer is lost, the payment becomes UNCERTAIN and SlothPay asks
PayPal what happened instead of guessing.

SmartPay captures at once. ConditionalPay holds funds and captures only when a condition is
verified, and silence never captures. Buyers and merchants see a plain status, the one safe next
step, a tamper-proof history and a Trust Passport.

Safe does not mean slow. SlothPay keeps its hot path short, reuses one PayPal connection, and
times every step, splitting SlothPay's own time from PayPal's time and the buyer's time. A
Speed panel and a Compare page show the cost of safety, and every number is labeled sandbox.

An optional AI helper explains payments in plain words but never moves money. An optional signed
webhook lets a trusted outside service verify a ConditionalPay condition, with replay protection.
Everything runs against the PayPal sandbox; no real money moves.

## What it does, in short

- No double charge: an idempotency key, a lock, an open-payment check, a stable PayPal request id
  and a claim in the database before any call.
- Lost-answer recovery: UNCERTAIN, then a read from PayPal at 0.3, 1, 2, 4 and 8 seconds.
- Two paths: SmartPay and ConditionalPay.
- Verified webhooks, a tamper-proof audit trail and a shareable Trust Passport.
- Measured speed and a side-by-side comparison with a plain checkout (order step).
- Break-it-on-purpose controls (sandbox, local) to show the recovery working.

## Challenges and what we learned

Lost answers are the hard case: if you retry, you can charge twice, and if you wait, money is
stuck. We learned to save before sending, to ask PayPal instead of guessing, and to keep
decisions in plain code with the AI only explaining. Speed work taught us to measure first and to
keep safety steps out of the shortcuts.

## What is next

A fix for declined captures (offer a new payment method), automatic expiry of held payments on
the hosted demo (needs a worker), a rate limit and an audited reset for the signed condition
webhook, and a verified real Zapier flow.

## What was built during the hackathon

*(Confirm and edit: state the date the repository was created and that all code in it was written
during the hackathon period.)* The backend (state machine, orchestration, duplicate protection,
reconciliation, webhooks, security, timing), the dashboard, the tests and the deployment files in
this repository.

## Built with

Python, FastAPI, PostgreSQL, Redis, SQLAlchemy and Alembic, Temporal (local only), Next.js,
TypeScript and Tailwind, Docker, Render, Sentry (optional), PayPal sandbox APIs.

## AI tools used and how

*(Edit to match what your team really used.)*

- Claude (Anthropic): coding assistant for code, tests and documentation, working from a 19-task
  plan with a prompt for each task. Every change was tested, read and reviewed before merging.
  See `docs/ai-development.md`.
- Gemma (optional, at run time): plain-language explanations and invoice reading from masked
  facts. It cannot move money, and the app works without it.

## How judges can test it (sandbox only)

1. Open the hosted demo. The first load can take about a minute (free hosting wakes up).
2. Log in with the demo login (`alice`, `bob` or `carol`; pick one). No password or secret is needed.
3. Create a SmartPay payment of a small amount and press **Create payment**.
4. On PayPal's page sign in with a **PayPal sandbox buyer** account and approve. Judges can use
   their own sandbox buyer from developer.paypal.com, Testing tools, Sandbox accounts.
   *(Optional: list a test buyer here only if you are happy to share it. These are sandbox-only
   accounts, but never put a real account or any API secret here.)*
5. The payment completes. Open it to see the timeline and the Trust Passport.
6. Try ConditionalPay: approve, then press **Verify: condition met** in the payment details.
7. Open the Speed panel on the dashboard to see SlothPay's own time next to PayPal's and the buyer's.
8. The hosted demo has no Temporal, no chaos panel and no Compare page. The video shows them
   running locally, and the README explains how to run them (Docker Compose quick start).

Running it yourself: the README has a Docker Compose quick start that needs only a PayPal sandbox
app.

## Honest limits

See README, "Security and Known Limitations". In short: sandbox only; a declined capture is a
known gap; a lost answer to *create order* can leave a payment waiting for a check; speed numbers
are sandbox and simulator figures; the hosted demo has no Temporal, so a held payment's deadline
does not expire by itself there; the signed webhook has not been tried with a live Zapier.
