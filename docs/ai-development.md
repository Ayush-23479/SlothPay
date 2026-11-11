# How AI was used to build SlothPay

SlothPay was built with an AI coding assistant working from a written **task plan**: 19 numbered
tasks, each with a goal, a tier (Must, Should or Optional), the tasks it depends on, a "done
when" test, and a prompt. This page explains the method and shows what each prompt asked for.
The assistant wrote code; people chose the design, ran every test, read every change and
reviewed every pull request.

*(Edit the first paragraph if your team used other tools or a different split of work.)*

## The method

1. **One task at a time.** Each prompt was pasted after a shared context block, then the work was
   done on its own branch and merged by pull request after review.
2. **A shared context first.** Before every task the assistant got the same facts: the repo
   layout, the chosen stack, the payment statuses, the duplicate-protection design, the PayPal
   endpoints and error classes, and the environment variables. This kept 19 separate sessions
   consistent.
3. **Global rules in every prompt.** The same rules applied to every task (next section).
4. **Tests are part of the task.** Every prompt ends with "output the complete files, list the
   tests, give the exact command to run them", and a "done when" check.
5. **People verified the result.** Tests were run on the team's own machines, failures were
   pasted back and fixed, and pull requests were reviewed before merging.

## The rules every prompt carried

| Rule | Why it exists |
| --- | --- |
| Sandbox and synthetic data only. Never store or log secrets, full card or account data. | No real money or real data is involved. |
| Default-deny: unknown or invalid input is rejected, and silence never captures money. | A mistake should fail safe. |
| Every status change goes through the state machine and a compare-and-set update, and writes an audit event. | One place decides what can happen to a payment. |
| Use only files and functions named in the task. If something is missing, say so instead of inventing it. | Stops the assistant making up code. |
| Output complete files and the exact test commands. | Everything can be copied, run and checked. |
| Speed rules: a short hot path (one lock, one database transaction, the PayPal call), shared connection, timed steps. A speed shortcut must never skip save-before-send, the duplicate checks or the state machine. | Safety must not be traded for speed. |

## What the AI was allowed to do inside the product

Gemma (an optional language model, used at run time) can explain a payment or an error in plain
words and read an invoice into a draft. It only sees masked facts. Its answer must match a fixed
schema, has a time limit, and falls back to fixed text. It can only name actions the state
machine already allows. **It never moves money, changes an amount or picks an action outside the
permitted list.** The app works without it.

## The 19 prompts

| # | Task | Tier | What the prompt asked for |
| --- | --- | --- | --- |
| 1 | Foundation | Must | Settings, Docker, Compose, license: one command starts PostgreSQL, Redis and Temporal. |
| 2 | Database and payment records | Must | Durable tables for payments, attempts, provider events and an audit trail, with migrations. |
| 3 | State machine and policy | Must | Pure Python rules: which status can follow which, what the buyer sees, which action is safe. |
| 4 | PayPal gateway | Must | The only door to PayPal: tokens, orders, authorize, capture, void, status reads, error classes. |
| 5 | Duplicate protection | Must | Two layers: an idempotency key plus lock, and a stable PayPal request id saved before sending. |
| 6 | Orchestration | Must | The safe order of steps for SmartPay and ConditionalPay. |
| 7 | Webhooks | Must | Verify PayPal's signature, store each event once, apply events in the right order. |
| 8 | Rescue and reconciliation | Must | When an outcome is unknown, read the truth from PayPal, then compute the permitted actions. |
| 9 | Temporal workflows | Should | Durable waiting for a condition, expiry and recovery retries. |
| 10 | API routes and live updates | Must | The HTTP surface and a live status stream (Server-Sent Events). |
| 11 | AI intelligence | Should | Structured analysis, plain-language explanations and invoice drafting, with fallbacks. |
| 12 | Sentry and controlled failures | Must | Monitoring by layer, and sandbox-only "break it on purpose" switches. |
| 13 | Dashboard | Must | A Next.js dashboard: pay, watch the status move, see the safety numbers. |
| 14 | Trust Passport | Must | One page per payment that proves what happened, with confirmed versus recorded events. |
| 15 | Integration and end-to-end tests | Must | Prove the promises with real components: no duplicates, lost answers recovered. |
| 16 | Security and privacy hardening | Must | Signed sessions, rate limits, startup guards, a secrets scan. |
| 17 | Deploy, docs, submission pack | Must | Render and Docker files, README, architecture, demo script, submission text. |
| 18 | Optional extensions | Optional | A signed external condition-verification webhook with replay protection. |
| 19 | Speed and swift transactions | Must | Measure and cut SlothPay's own time, split it from PayPal's and the buyer's time. |

## Where AI help ran out

Being honest about this is part of the method:

- The pull-request review of Task 18 caught missing documentation, a hidden character in two
  files, a wrong comment and a missing rate limit. The tests alone did not.
- Several test failures only showed up on the team's own machines (different operating system,
  leftover local settings). They were fixed after the output was pasted back.
- Real PayPal sandbox behaviour and the live Render deployment were checked by people, not by the
  assistant.
- Known gaps are listed in the README ("Security and Known Limitations") and were not hidden.
