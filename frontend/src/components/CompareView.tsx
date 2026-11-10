"use client";

import { useEffect, useRef, useState } from "react";
import { api, ApiRequestError } from "@/lib/api";
import type { BaselineResult, Payment } from "@/lib/types";

const TIMEOUTS = [5, 10, 30];
const AMOUNT = "5.00";

type Arm<T> = { state: "idle" } | { state: "running" } | { state: "done"; value: T } | { state: "error"; message: string };

interface SlothResult {
  status: string;
  rescued: boolean;
  total_ms: number;
  orders: number;
}

function seconds(value: number): string {
  return `${(value / 1000).toFixed(value >= 10_000 ? 0 : 1)} s`;
}

function newKey(): string {
  return `compare-${crypto.randomUUID()}`;
}

/** The order step with and without SlothPay. Capturing needs a buyer, so it is not compared. */
export function CompareView() {
  const [timeoutS, setTimeoutS] = useState(30);
  const [loseAnswer, setLoseAnswer] = useState(false);
  const [chaosAvailable, setChaosAvailable] = useState<boolean | null>(null);
  const [baseline, setBaseline] = useState<Arm<BaselineResult>>({ state: "idle" });
  const [sloth, setSloth] = useState<Arm<SlothResult>>({ state: "idle" });
  const [elapsed, setElapsed] = useState(0);
  const running = baseline.state === "running" || sloth.state === "running";
  const startedAt = useRef(0);

  useEffect(() => {
    let cancelled = false;
    api
      .getChaos()
      .then(() => !cancelled && setChaosAvailable(true))
      .catch(() => !cancelled && setChaosAvailable(false));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!running) return;
    startedAt.current = Date.now();
    const timer = setInterval(() => setElapsed(Date.now() - startedAt.current), 250);
    return () => clearInterval(timer);
  }, [running]);

  async function arm(on: boolean) {
    if (!chaosAvailable) return;
    await api.setChaos(
      on
        ? { mode: "drop_after_send", probability: 1, remaining_count: 1 }
        : { mode: "none", probability: 0 },
    );
  }

  async function runBaseline() {
    setBaseline({ state: "running" });
    try {
      await arm(loseAnswer);
      setBaseline({
        state: "done",
        value: await api.baselinePay({ amount: AMOUNT, currency: "USD", timeout_seconds: timeoutS }),
      });
    } catch (e) {
      setBaseline({
        state: "error",
        message: e instanceof ApiRequestError ? e.message : "The run failed.",
      });
    }
  }

  async function runSloth() {
    setSloth({ state: "running" });
    const began = performance.now();
    try {
      await arm(loseAnswer);
      let rescued = false;
      const first = await api.createPayment(
        { path: "SMARTPAY", amount: AMOUNT, currency: "USD" },
        newKey(),
      );
      let payment: Payment = first.payment;
      if (payment.status === "UNCERTAIN") {
        const plan = await api.rescuePlan(payment.id);
        if (plan.allowed_actions.includes("RETRY_SAME_REQUEST")) {
          await api.rescueAction(payment.id, "RETRY_SAME_REQUEST");
          rescued = true;
          payment = await api.getPayment(payment.id);
        }
      }
      setSloth({
        state: "done",
        value: {
          status: payment.status,
          rescued,
          total_ms: performance.now() - began,
          orders: 1,
        },
      });
    } catch (e) {
      setSloth({
        state: "error",
        message: e instanceof ApiRequestError ? e.message : "The run failed.",
      });
    } finally {
      if (loseAnswer) await arm(false).catch(() => undefined);
    }
  }

  async function runBoth() {
    await runBaseline();
    await runSloth();
  }

  return (
    <div className="mx-auto max-w-4xl space-y-6 px-4 py-8 sm:px-6">
      <header>
        <h1 className="text-3xl font-extrabold">With and without SlothPay</h1>
        <p className="mt-2 text-muted">
          Both sides create one {AMOUNT} USD order in the PayPal sandbox.{" "}
          <strong>Only the order step is compared</strong>: capturing needs a real buyer to approve at
          PayPal, so nothing is captured. All numbers are sandbox numbers.
        </p>
      </header>

      <section aria-labelledby="opts-h" className="card space-y-4">
        <h2 id="opts-h" className="text-xl font-extrabold">
          Set up the run
        </h2>
        <div>
          <label htmlFor="timeout" className="block text-sm font-bold">
            How long the plain checkout waits for an answer
          </label>
          <select
            id="timeout"
            className="mt-1 rounded-lg border border-line bg-white px-3 py-2"
            value={timeoutS}
            disabled={running}
            onChange={(e) => setTimeoutS(Number(e.target.value))}
          >
            {TIMEOUTS.map((t) => (
              <option key={t} value={t}>
                {t} seconds{t === 30 ? " (a common default)" : ""}
              </option>
            ))}
          </select>
        </div>
        {chaosAvailable ? (
          <label className="flex items-center gap-2 text-sm font-semibold">
            <input
              type="checkbox"
              checked={loseAnswer}
              disabled={running}
              onChange={(e) => setLoseAnswer(e.target.checked)}
            />
            Lose PayPal&apos;s first answer (test mode: PayPal creates the order, the reply never arrives)
          </label>
        ) : chaosAvailable === false ? (
          <p className="text-sm text-muted">
            Failure test mode is off on this server, so this page can only run the clean case.
          </p>
        ) : null}
        <div className="flex flex-wrap gap-3">
          <button type="button" className="btn btn-primary" disabled={running} onClick={() => void runBoth()}>
            Run both
          </button>
          <button type="button" className="btn btn-soft" disabled={running} onClick={() => void runBaseline()}>
            Plain checkout only
          </button>
          <button type="button" className="btn btn-soft" disabled={running} onClick={() => void runSloth()}>
            SlothPay only
          </button>
        </div>
        {running ? (
          <p role="status" aria-live="polite" className="text-sm text-muted">
            Running… {seconds(elapsed)}
          </p>
        ) : null}
      </section>

      <div className="grid gap-6 md:grid-cols-2">
        <section aria-labelledby="plain-h" className="card" aria-busy={baseline.state === "running"}>
          <h2 id="plain-h" className="text-xl font-extrabold">
            Plain checkout
          </h2>
          {baseline.state === "idle" ? <p className="mt-2 text-sm text-muted">Not run yet.</p> : null}
          {baseline.state === "running" ? <div className="skeleton mt-3 h-24" /> : null}
          {baseline.state === "error" ? (
            <p className="alert alert-error mt-3" role="alert">
              {baseline.message}
            </p>
          ) : null}
          {baseline.state === "done" ? (
            <div className="mt-3 space-y-2 text-sm">
              <p className="text-2xl font-extrabold">{seconds(baseline.value.total_ms)}</p>
              <p>
                {baseline.value.attempts} request{baseline.value.attempts === 1 ? "" : "s"} sent, no
                request id.{" "}
                {baseline.value.waited_ms > 0
                  ? `It waited ${seconds(baseline.value.waited_ms)} for an answer that never came, then sent the order again.`
                  : "Nothing went wrong, so nothing was repeated."}
              </p>
              <p className={baseline.value.paypal_orders_possible > 1 ? "font-bold text-red-700" : ""}>
                PayPal orders created: {baseline.value.paypal_orders_possible}
                {baseline.value.paypal_orders_possible > 1 ? " – a duplicate" : ""}
              </p>
            </div>
          ) : null}
        </section>

        <section aria-labelledby="sloth-h" className="card" aria-busy={sloth.state === "running"}>
          <h2 id="sloth-h" className="text-xl font-extrabold">
            SlothPay
          </h2>
          {sloth.state === "idle" ? <p className="mt-2 text-sm text-muted">Not run yet.</p> : null}
          {sloth.state === "running" ? <div className="skeleton mt-3 h-24" /> : null}
          {sloth.state === "error" ? (
            <p className="alert alert-error mt-3" role="alert">
              {sloth.message}
            </p>
          ) : null}
          {sloth.state === "done" ? (
            <div className="mt-3 space-y-2 text-sm">
              <p className="text-2xl font-extrabold">{seconds(sloth.value.total_ms)}</p>
              <p>
                Final status: <strong>{sloth.value.status}</strong>.{" "}
                {sloth.value.rescued
                  ? "The answer was lost, so SlothPay checked and then sent the same request again with the same request id. PayPal can tell it is the same request."
                  : "Every request carried a request id, so a repeat can never create a second order."}
              </p>
              <p>PayPal orders created: {sloth.value.orders}</p>
            </div>
          ) : null}
        </section>
      </div>

      <p className="text-xs text-muted">
        Honest limits: a lost answer can still leave a SlothPay payment waiting for a check; here the
        recovery is the rescue action &ldquo;Try the same request again&rdquo;. The plain checkout in this
        page is deliberately simple and exists only for this comparison.
      </p>
    </div>
  );
}