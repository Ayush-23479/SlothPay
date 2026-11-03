"use client";

import { useId, useRef, useState } from "react";
import { api, ApiRequestError } from "@/lib/api";
import { formatMoney } from "@/lib/format";
import { getCheckout, rememberCheckoutPayment } from "@/lib/session";
import type { PathName, Payment, PaymentCreate } from "@/lib/types";

export interface PayFormInitial {
  amount?: string;
  currency?: string;
  description?: string;
}

interface Props {
  initial?: PayFormInitial;
  /** Shown above the form, e.g. warnings from an invoice that filled it in. */
  notices?: string[];
  onCreated: (payment: Payment) => void;
}

const CURRENCIES = ["USD", "EUR", "GBP", "INR"];
const MAX_MINUTES = 48 * 60;
const AMOUNT = /^\d{1,12}(\.\d{1,2})?$/;

const PATHS: { value: PathName; title: string; help: string }[] = [
  { value: "SMARTPAY", title: "SmartPay", help: "Pay now. Safe against double charges." },
  {
    value: "CONDITIONALPAY",
    title: "ConditionalPay",
    help: "Funds are held and only taken once your condition is met.",
  },
];

function safeRedirect(url: string | null): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url, window.location.origin);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.toString() : null;
  } catch {
    return null;
  }
}

export function PayForm({ initial, notices = [], onCreated }: Props) {
  const uid = useId();
  const [path, setPath] = useState<PathName>("SMARTPAY");
  const [amount, setAmount] = useState(initial?.amount ?? "");
  const [currency, setCurrency] = useState(
    initial?.currency && CURRENCIES.includes(initial.currency) ? initial.currency : "USD",
  );
  const [description, setDescription] = useState(initial?.description ?? "");
  const [kind, setKind] = useState("delivery");
  const [condition, setCondition] = useState("");
  const [minutes, setMinutes] = useState("60");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "error" | "info" | "ok"; text: string } | null>(
    null,
  );
  // A ref locks synchronously, so two fast clicks in the same tick cannot both start.
  const inFlight = useRef(false);

  function validate(): PaymentCreate | null {
    const next: Record<string, string> = {};
    const trimmed = amount.trim();
    if (!AMOUNT.test(trimmed) || Number(trimmed) <= 0) {
      next.amount = "Enter an amount above 0, with up to 2 decimals (like 24.00).";
    }
    if (description.length > 200) next.description = "Keep the note under 200 characters.";
    let conditionBody: PaymentCreate["condition"] = null;
    if (path === "CONDITIONALPAY") {
      const mins = Number(minutes);
      if (!condition.trim()) next.condition = "Say what must happen before the funds are taken.";
      else if (condition.trim().length > 200) next.condition = "Keep it under 200 characters.";
      if (!Number.isInteger(mins) || mins < 1 || mins > MAX_MINUTES) {
        next.minutes = `Enter a whole number of minutes from 1 to ${MAX_MINUTES}.`;
      }
      conditionBody = { kind, description: condition.trim(), deadline_minutes: mins };
    }
    setErrors(next);
    if (Object.keys(next).length > 0) return null;
    return {
      path,
      amount: Number(trimmed).toFixed(2),
      currency,
      description: description.trim() || null,
      condition: conditionBody,
    };
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (inFlight.current) return;
    const body = validate();
    if (!body) return;
    inFlight.current = true;
    setBusy(true);
    setMessage(null);
    try {
      // One Idempotency-Key per checkout: a second click or a retry reuses it.
      const checkout = getCheckout(JSON.stringify(body));
      const { payment, replay } = await api.createPayment(
        { ...body, merchant_order_id: checkout.orderId },
        checkout.key,
      );
      rememberCheckoutPayment(payment.id);
      onCreated(payment);
      const target = safeRedirect(payment.approval_url);
      if (target) {
        setMessage({
          tone: "info",
          text: replay
            ? "You already started this payment, so we did not create a second one. Taking you to PayPal…"
            : "Payment started. Taking you to PayPal to approve it…",
        });
        window.location.assign(target); // stay locked while the browser leaves
        return;
      }
      setMessage({
        tone: payment.display_status === "ACTION_NEEDED" ? "error" : "info",
        text: replay
          ? "You already started this payment, so we did not create a second one. See its status below."
          : "Payment recorded. See its status below.",
      });
    } catch (error) {
      const text =
        error instanceof ApiRequestError
          ? error.message
          : "Something went wrong. Please try again; it is safe to press the button again.";
      setMessage({ tone: "error", text });
    }
    inFlight.current = false;
    setBusy(false);
  }

  const preview =
    AMOUNT.test(amount.trim()) && Number(amount) > 0
      ? formatMoney(Number(amount).toFixed(2), currency)
      : null;

  return (
    <form
      onSubmit={submit}
      noValidate
      aria-busy={busy}
      className="rounded-[2rem] bg-white p-6 shadow-[0_20px_60px_rgba(0,20,53,0.18)] sm:p-10"
    >
      <h2 className="text-2xl font-extrabold sm:text-3xl">Start a payment</h2>

      {notices.length > 0 ? (
        <div className="alert alert-warn mt-4" role="note">
          <p className="font-semibold">Filled in from your invoice. Please check every field.</p>
          <ul className="mt-1 list-disc pl-5">
            {notices.map((text) => (
              <li key={text}>{text}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <fieldset className="mt-6" disabled={busy}>
        <legend className="label">Payment type</legend>
        <div className="grid grid-cols-2 gap-1 rounded-full bg-soft p-1">
          {PATHS.map((item) => (
            <label
              key={item.value}
              className="relative flex min-h-11 cursor-pointer items-center justify-center rounded-full px-3 text-center text-sm font-bold has-[:checked]:bg-ink has-[:checked]:text-white has-[:focus-visible]:outline has-[:focus-visible]:outline-[3px] has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-[var(--brand-bright)]"
            >
              <input
                type="radio"
                name={`${uid}-path`}
                value={item.value}
                checked={path === item.value}
                onChange={() => setPath(item.value)}
                className="sr-only"
              />
              {item.title}
            </label>
          ))}
        </div>
        <p className="hint" id={`${uid}-path-help`}>
          {PATHS.find((item) => item.value === path)?.help}
        </p>
      </fieldset>

      <div className="mt-5 grid gap-4 sm:grid-cols-[1fr_9rem]">
        <div>
          <label className="label" htmlFor={`${uid}-amount`}>
            Amount
          </label>
          <input
            id={`${uid}-amount`}
            className="field text-xl font-bold"
            inputMode="decimal"
            autoComplete="off"
            placeholder="0.00"
            value={amount}
            disabled={busy}
            aria-invalid={Boolean(errors.amount)}
            aria-describedby={errors.amount ? `${uid}-amount-err` : undefined}
            onChange={(e) => setAmount(e.target.value)}
          />
          {errors.amount ? (
            <p className="error-text" id={`${uid}-amount-err`}>
              {errors.amount}
            </p>
          ) : null}
        </div>
        <div>
          <label className="label" htmlFor={`${uid}-currency`}>
            Currency
          </label>
          <select
            id={`${uid}-currency`}
            className="field"
            value={currency}
            disabled={busy}
            onChange={(e) => setCurrency(e.target.value)}
          >
            {CURRENCIES.map((code) => (
              <option key={code} value={code}>
                {code}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="mt-4">
        <label className="label" htmlFor={`${uid}-desc`}>
          Note (optional)
        </label>
        <input
          id={`${uid}-desc`}
          className="field"
          maxLength={200}
          value={description}
          disabled={busy}
          aria-invalid={Boolean(errors.description)}
          onChange={(e) => setDescription(e.target.value)}
        />
        {errors.description ? <p className="error-text">{errors.description}</p> : null}
      </div>

      {path === "CONDITIONALPAY" ? (
        <div className="mt-5 rounded-2xl bg-soft p-4">
          <p className="text-sm font-bold">Release condition</p>
          <div className="mt-3 grid gap-4 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor={`${uid}-kind`}>
                Type
              </label>
              <select
                id={`${uid}-kind`}
                className="field"
                value={kind}
                disabled={busy}
                onChange={(e) => setKind(e.target.value)}
              >
                <option value="delivery">Delivery</option>
                <option value="approval">Approval</option>
                <option value="milestone">Milestone</option>
              </select>
            </div>
            <div>
              <label className="label" htmlFor={`${uid}-minutes`}>
                Deadline (minutes)
              </label>
              <input
                id={`${uid}-minutes`}
                className="field"
                inputMode="numeric"
                value={minutes}
                disabled={busy}
                aria-invalid={Boolean(errors.minutes)}
                onChange={(e) => setMinutes(e.target.value)}
              />
              {errors.minutes ? <p className="error-text">{errors.minutes}</p> : null}
            </div>
          </div>
          <div className="mt-4">
            <label className="label" htmlFor={`${uid}-cond`}>
              What must happen?
            </label>
            <input
              id={`${uid}-cond`}
              className="field"
              maxLength={200}
              placeholder="Parcel is delivered to the buyer"
              value={condition}
              disabled={busy}
              aria-invalid={Boolean(errors.condition)}
              onChange={(e) => setCondition(e.target.value)}
            />
            {errors.condition ? <p className="error-text">{errors.condition}</p> : null}
            <p className="hint">If the deadline passes, the held funds are released back.</p>
          </div>
        </div>
      ) : null}

      <div className="mt-6 flex flex-col gap-3 sm:flex-row sm:items-center">
        <button type="submit" className="btn btn-primary w-full sm:w-auto" disabled={busy}>
          {busy ? "Working…" : preview ? `Create payment · ${preview}` : "Create payment"}
        </button>
        <p className="text-sm text-muted">
          Pressing twice never charges twice.
        </p>
      </div>

      <div aria-live="polite" className="mt-4 empty:hidden">
        {message ? (
          <p
            className={`alert ${
              message.tone === "error" ? "alert-error" : message.tone === "ok" ? "alert-ok" : "alert-info"
            }`}
            role={message.tone === "error" ? "alert" : "status"}
          >
            {message.text}
          </p>
        ) : null}
      </div>
    </form>
  );
}