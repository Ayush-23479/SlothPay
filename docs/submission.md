# Submission pack

Deadline: 12 November 2026, 12:00 pm PT (1:30 am IST on 13 November). Submit by **11 November**.
Keep the hosted demo free and available until **15 December**.

## Checklist

- [ ] Hosted demo link works (open it from a private window).
- [ ] Public GitHub repository link; no secrets (`python scripts/scan_secrets.py` is clean).
- [ ] Demo video, public on YouTube, under 3 minutes.
- [ ] Text description (below).
- [ ] AI tools list (below), edited to be true.
- [ ] Sandbox test instructions (below).
- [ ] Database expiry date in the calendar (`docs/deploy.md`).

## Text description

SlothPay is a payment-safety layer for PayPal. It stops the classic failure where a payment's
answer is lost and the buyer pays twice or money is left frozen. Every payment has one stable
identity, every PayPal call carries a stable request id, and a move is claimed in the database
before PayPal is called. If an answer is lost, the payment becomes UNCERTAIN and SlothPay asks
PayPal what happened instead of guessing. SmartPay captures at once. ConditionalPay holds funds
and captures only when the owner marks a condition met. Buyers and merchants see a plain status,
the one safe next step, a tamper-proof history and a Trust Passport. An optional AI helper
explains payments in plain words but never moves money. Everything runs against the PayPal
sandbox; no real money moves.

## What was built during the hackathon

*(Confirm and edit: state the date the repository was created and that all code in it was written
during the hackathon period.)* The backend (state machine, orchestration, duplicate protection,
reconciliation, webhooks, security), the dashboard, the tests and the deployment files in this
repository.

## AI tools used and how

*(Edit to match what your team really used.)*

- Claude (Anthropic): coding assistant for code, tests and documentation, working from a task plan.
  Every change was tested and read before commit.
- Gemma (optional, at run time): plain-language explanations and invoice reading from masked
  facts. It cannot move money, and the app works without it.

## How judges can test it (sandbox only)

1. Open the hosted demo. The first load can take about a minute (free hosting wakes up).
2. Create a SmartPay payment of a small amount and press **Create payment**.
3. On PayPal's page sign in with a **PayPal sandbox buyer** account and approve. Judges can use
   their own sandbox buyer from developer.paypal.com, Testing tools, Sandbox accounts.
   *(Optional: list a test buyer here only if you are happy to share it. These are sandbox-only
   accounts, but never put a real account or any API secret here.)*
4. The payment completes. Open it to see the timeline and the Trust Passport.
5. Try ConditionalPay: approve, then press **Verify: condition met** in the payment details.
6. The hosted demo has no Temporal and no chaos panel. The video shows both locally.

## Honest limits

See README, "Limits you should know about".