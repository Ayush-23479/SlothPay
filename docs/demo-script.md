# Demo script (under 3 minutes)

The video must be under 3 minutes and public on YouTube. Record locally (Docker Compose with
`--profile workers`, `CHAOS_ENABLED=true`, Temporal running) so the chaos panel is available.
The hosted demo has chaos off. Use the sandbox buyer account (README, "How to log in as the
sandbox buyer"). **Do not demo "Decline"**: a declined capture is a known gap.

Rehearse once and keep the PayPal approval page short. Speak the words in quotes.

| Time | On screen | Say |
| --- | --- | --- |
| 0:00 | Dashboard | "You tap Pay on a slow connection. PayPal takes the money, but the answer is lost. You tap again. Now Rs 500 is frozen and you may be charged twice. SlothPay stops that." |
| 0:20 | Create a SmartPay payment, approve as the sandbox buyer | "SmartPay. Every payment has one stable identity. Approved in the PayPal sandbox, captured once, and the status says Completed." |
| 0:45 | Open the payment: timeline and Trust Passport | "Every step is on a timeline that nobody can edit, and the Trust Passport shows what PayPal confirmed." |
| 1:05 | Chaos panel: **Kill network**, then pay | "Now I lose PayPal's answer on purpose. SlothPay does not guess and does not charge again. The payment is Pending: checking with PayPal." |
| 1:30 | Payment turns Completed by itself | "It asked PayPal what really happened and moved to the true state. No second charge." |
| 1:50 | Chaos panel: **Double-click test (5 clicks)** | "Five clicks at once. One payment, four stopped duplicates. The safety numbers show it." |
| 2:15 | ConditionalPay: authorize, approve, press **Verify: condition met** | "ConditionalPay holds the funds. Silence never captures. Only when the owner marks the condition met is the money captured." |
| 2:40 | Safety numbers | "All against the PayPal sandbox. No real money. Thank you." |

## Before recording

1. Each preset fires once for the next payment. Press **Turn off** between takes so nothing is left armed.
2. Use a fresh payment amount each take, because the same amount from the same owner within 2
   minutes returns the open payment.
3. Keep the browser zoom large enough to read.
4. Upload as **Public** (not Unlisted) on YouTube and check the length is under 3:00.