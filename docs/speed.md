# Speed and swift transactions (Task 19)

Every number here is a **sandbox** or **simulator** number. PayPal's sandbox is slower and less
steady than the live service, so these figures show how much SlothPay adds, not how fast real
PayPal is.

## What is measured

Each payment is split into three kinds of time, stored in `payment_timings`:

| Kind | Meaning | Counted as SlothPay's overhead |
|---|---|---|
| `ours` | SlothPay's own work (checks, database writes, building the answer) | yes |
| `provider` | Waiting for PayPal (create, authorize, capture, reads, token) | no |
| `human` | The buyer approving at PayPal | no |
| `wait` | Deliberate pauses (0.3, 1, 2, 4, 8 s between reconcile reads) | no |

`GET /api/speed` returns p50 and p95 per kind and per step for the signed-in owner. The dashboard
shows it as the **Speed** panel. `GET /api/metrics` adds `safety_overhead_ms_p95` and
`condition_to_capture_ms_p95`.

## What changed to make it fast (safety order untouched)

- **One shared PayPal client** per process: the connection stays open and the token is fetched at
  startup and renewed before it expires. This removes a TLS handshake and a login per request.
- **The extra GET after `create_order` is gone.** The payment moves to `AWAITING_APPROVAL` from
  our own database; PayPal approval is still verified when the buyer returns.
- **Live updates, Sentry reports and the first reconcile check run after the response.**
  The first check waits about 0.3 s, then 1, 2, 4, 8 s.
- Unchanged: one Redis lock, one database transaction and the PayPal call before the answer;
  save-before-send; the duplicate checks; the state machine.

## Targets

SlothPay overhead p95 under 100 ms; verified condition to capture request under 1 s; first
reconcile check within 500 ms.

```bash
python scripts/latency_bench.py              # 200 payments, one at a time, simulator
```

Run it one payment at a time: the simulator, SlothPay and the database share one CPU, so many at
once measures that contention rather than SlothPay's added time. In one development run (simulator,
one at a time) the overhead was p50 39 ms and p95 47 ms. Run the script yourself and update
these two numbers if yours differ.

## Compare page (`/compare`)

Runs a plain checkout (no request id, long timeout, a blind retry) next to SlothPay, for the
**order step only**: capturing needs a real buyer. With "lose the first answer" on, the plain
checkout waits out its timeout and then creates a second order; SlothPay does not resend blindly,
checks, and recovers with the same request id. The plain-checkout route answers 404 unless chaos
is allowed, so it does not exist on the hosted demo.

## Honest limits

- Simulator and sandbox timings only. No live PayPal numbers.
- `condition_to_capture` includes the mandatory authorization read before capture.
- "ours" excludes work done before our code runs (request body parsing).
- About 10% of lost create-order answers still leave the payment `UNCERTAIN` until the rescue
  action runs. A declined capture is still the known expected failure from earlier tasks.
- `PRECREATE_ORDERS` and gathering independent reads were not done.
- The hosted demo has no Temporal; the in-API workers do the reconcile work.
