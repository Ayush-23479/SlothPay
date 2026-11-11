# Demo script (under 3 minutes)

The video must be under 3 minutes and public on YouTube. This script runs about 2:50, so keep
each take tight. Record locally (Docker Compose with `--profile workers`, `CHAOS_ENABLED=true`,
Temporal running) so the chaos panel and the Compare page are available. The hosted demo has
chaos off. Use the sandbox buyer account (README, "How to log in as the sandbox buyer").
**Do not demo "Decline"**: a declined capture is a known gap.

Do not show third-party logos or play copyrighted music. Speak the words in quotes. Read the
real numbers from the screen; never say a number you did not see.

| Time | On screen | Say |
| --- | --- | --- |
| 0:00 | Dashboard | "You tap Pay on a slow connection. PayPal takes the money, but the answer is lost. You tap again. Now Rs 500 is frozen and you may be charged twice. SlothPay stops that." |
| 0:20 | Create a SmartPay payment, approve as the sandbox buyer | "SmartPay. Every payment has one stable identity. Approved in the PayPal sandbox, captured once, and the status says Completed." |
| 0:45 | Open the payment: timeline and Trust Passport | "Every step is on a timeline nobody can edit, and the Trust Passport shows what PayPal confirmed." |
| 1:00 | Chaos panel: **Kill network**, then pay | "Now I lose PayPal's answer on purpose. SlothPay does not guess and does not charge again. The payment is Pending: checking with PayPal." |
| 1:25 | Payment turns Completed by itself | "It asked PayPal what really happened and moved to the true state. No second charge." |
| 1:40 | Chaos panel: **Double-click test (5 clicks)** | "Five clicks at once. One payment, the rest stopped as duplicates." |
| 1:55 | ConditionalPay: authorize, approve, press **Verify: condition met** | "ConditionalPay holds the funds. Silence never captures. Only when the condition is verified is the money captured." |
| 2:20 | Speed panel | "Safety should not make payments slow. This panel splits every payment into SlothPay's own time, PayPal's time and the buyer's time. These are sandbox numbers." *(Point at the SlothPay row and read the figure shown.)* |
| 2:40 | Safety numbers | "All against the PayPal sandbox. No real money. Thank you." |

If you are running short, drop the double-click row (1:40). If you have spare seconds, show the
Compare page for 10 seconds instead: "Without a request id a plain checkout waits, then resends
and can create two orders. SlothPay does not."

## Before recording

1. Each preset fires once for the next payment. Press **Turn off** between takes so nothing is left armed.
2. Use a fresh payment amount each take, because the same amount from the same owner within 2
   minutes returns the open payment.
3. Make at least a few payments first so the Speed panel has numbers to show.
4. Keep the browser zoom large enough to read.
5. If the hosted demo is used for any part, open it first: a free service sleeps and the first
   load takes about a minute.
6. Upload as **Public** (not Unlisted) on YouTube and check the length is under 3:00.
