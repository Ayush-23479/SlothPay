"use client";

import { useEffect, useState } from "react";
import { api, ApiRequestError } from "@/lib/api";
import type { ChaosSetIn, ChaosSettings, DoubleClickResult } from "@/lib/types";

const PRESETS: { label: string; help: string; body: ChaosSetIn }[] = [
  {
    label: "Kill network",
    help: "The answer from PayPal is lost once.",
    body: { mode: "drop_after_send", probability: 1, remaining_count: 1 },
  },
  {
    label: "Slow bank",
    help: "The next call waits 3 seconds.",
    body: { mode: "delay", probability: 1, delay_ms: 3000, remaining_count: 1 },
  },
  {
    label: "Decline",
    help: "PayPal declines the next payment.",
    body: { mode: "mock_error", probability: 1, code: "INSTRUMENT_DECLINED", remaining_count: 1 },
  },
];

/** Shown only when the server answers GET /api/chaos (sandbox with chaos switched on). */
export function ChaosPanel() {
  const [state, setState] = useState<ChaosSettings | null>(null);
  const [available, setAvailable] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [clicks, setClicks] = useState<DoubleClickResult | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .getChaos()
      .then((value) => {
        if (cancelled) return;
        setState(value);
        setAvailable(true);
      })
      .catch(() => {
        if (!cancelled) setAvailable(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!available) return null;

  async function apply(body: ChaosSetIn, text: string) {
    setBusy(true);
    setError(null);
    try {
      setState(await api.setChaos(body));
      setMessage(text);
    } catch (e) {
      setError(e instanceof ApiRequestError ? e.message : "Could not change the test mode.");
    }
    setBusy(false);
  }

  async function doubleClick() {
    setBusy(true);
    setError(null);
    setClicks(null);
    try {
      setClicks(
        await api.doubleClick({
          n: 5,
          payment: {
            path: "SMARTPAY",
            amount: "1.00",
            currency: "USD",
            description: "Double-click test",
            merchant_order_id: `dbl-${crypto.randomUUID()}`,
          },
        }),
      );
    } catch (e) {
      setError(e instanceof ApiRequestError ? e.message : "The double-click test failed.");
    }
    setBusy(false);
  }

  const active = state && state.mode !== "none";
  return (
    <section aria-labelledby="chaos-h" className="card border-dashed">
      <div className="flex items-center justify-between gap-3">
        <h2 id="chaos-h" className="text-xl font-extrabold">
          Break it on purpose
        </h2>
        <span className="rounded-full bg-[var(--warn-bg)] px-3 py-1 text-xs font-bold text-[var(--warn)]">
          Sandbox only
        </span>
      </div>
      <p className="mt-1 text-sm text-muted">
        Cause a failure, start a payment, and watch SlothPay recover without charging twice.
      </p>
      <div className="mt-4 grid gap-2 sm:grid-cols-3">
        {PRESETS.map((preset) => (
          <button
            key={preset.label}
            type="button"
            className="btn btn-soft flex-col !items-start !rounded-2xl text-left"
            disabled={busy}
            onClick={() => apply(preset.body, `${preset.label} is on for the next payment.`)}
          >
            <span>{preset.label}</span>
            <span className="text-xs font-normal text-muted">{preset.help}</span>
          </button>
        ))}
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <button type="button" className="btn btn-outline btn-sm" disabled={busy} onClick={doubleClick}>
          Double-click test (5 clicks)
        </button>
        <button
          type="button"
          className="btn btn-outline btn-sm"
          disabled={busy || !active}
          onClick={() => apply({ mode: "none" }, "Test mode is off.")}
        >
          Turn off
        </button>
      </div>
      <p className="mt-3 text-sm" role="status">
        Now: <strong>{active ? state?.mode.replaceAll("_", " ") : "off"}</strong>
      </p>
      <div aria-live="polite" className="empty:hidden">
        {message ? <p className="alert alert-info mt-2">{message}</p> : null}
        {clicks ? (
          <p className={`alert mt-2 ${clicks.safe ? "alert-ok" : "alert-error"}`}>
            {clicks.requested} clicks made {clicks.intents} payment and {clicks.paypal_orders} PayPal order.{" "}
            {clicks.safe ? "No duplicates." : "Duplicates found!"}
          </p>
        ) : null}
        {error ? (
          <p className="alert alert-error mt-2" role="alert">
            {error}
          </p>
        ) : null}
      </div>
    </section>
  );
}