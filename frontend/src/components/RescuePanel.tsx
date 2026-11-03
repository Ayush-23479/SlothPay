"use client";

import { useEffect, useState } from "react";
import { api, ApiRequestError } from "@/lib/api";
import { ACTION_LABEL } from "@/lib/format";
import type { Payment, Rescue } from "@/lib/types";

interface Props {
  payment: Payment;
  onChanged: (payment: Payment) => void;
}

/** Use key={payment.id}. It reloads the plan whenever the payment's status changes. */
export function RescuePanel({ payment, onChanged }: Props) {
  const needsHelp = payment.display_status === "PENDING" || payment.display_status === "ACTION_NEEDED";
  const [plan, setPlan] = useState<Rescue | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const status = payment.status;

  useEffect(() => {
    if (!needsHelp) return;
    let cancelled = false;
    api
      .rescuePlan(payment.id)
      .then((value) => {
        if (cancelled) return;
        setPlan(value);
        setError(null);
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e instanceof ApiRequestError ? e.message : "We could not load the rescue plan.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [payment.id, status, needsHelp]);

  async function run(action: string) {
    setBusy(action);
    setNote(null);
    try {
      await api.rescueAction(payment.id, action);
      setNote(`Done: ${ACTION_LABEL[action] ?? action}.`);
      onChanged(await api.getPayment(payment.id));
      setPlan(await api.rescuePlan(payment.id));
    } catch (e) {
      setError(e instanceof ApiRequestError ? e.message : "That did not work. Please try again.");
    }
    setBusy(null);
  }

  if (!needsHelp) {
    return (
      <section aria-label="Rescue">
        <h3 className="text-lg font-extrabold">Rescue</h3>
        <p className="mt-2 text-sm text-muted">Nothing to fix. This payment needs no action.</p>
      </section>
    );
  }

  return (
    <section aria-labelledby={`rescue-${payment.id}`}>
      <h3 id={`rescue-${payment.id}`} className="text-lg font-extrabold">
        Rescue
      </h3>
      {error ? (
        <p className="alert alert-error mt-3" role="alert">
          {error}
        </p>
      ) : null}
      {!plan && !error ? <div className="skeleton mt-3 h-20" aria-busy="true" /> : null}
      {plan ? (
        <div className="mt-3 space-y-4">
          {plan.explanation ? (
            <div className="alert alert-info">
              <p className="text-xs font-bold uppercase tracking-wide">
                {plan.explanation.source === "model" ? "AI explanation" : "Standard explanation"}
              </p>
              <p className="mt-1 font-semibold">{plan.explanation.message}</p>
              <p className="mt-1">{plan.explanation.next_step}</p>
            </div>
          ) : null}
          {plan.facts.length > 0 ? (
            <div>
              <p className="text-sm font-bold">What we know</p>
              <ul className="mt-1 list-disc pl-5 text-sm text-muted">
                {plan.facts.map((fact) => (
                  <li key={fact}>{fact}</li>
                ))}
              </ul>
            </div>
          ) : null}
          {plan.needs_human ? (
            <p className="alert alert-warn">A person should look at this one.</p>
          ) : null}
          {plan.allowed_actions.length > 0 ? (
            <div>
              <p className="text-sm font-bold">What you can do</p>
              <div className="mt-2 flex flex-wrap gap-2">
                {plan.allowed_actions.map((action) => (
                  <button
                    key={action}
                    type="button"
                    className={`btn btn-sm ${
                      action === plan.recommended_action ? "btn-primary" : "btn-soft"
                    }`}
                    disabled={busy !== null}
                    onClick={() => run(action)}
                  >
                    {busy === action ? "Working…" : (ACTION_LABEL[action] ?? action)}
                    {action === plan.recommended_action ? " (suggested)" : ""}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <p className="text-sm text-muted">No actions are available right now.</p>
          )}
          <div aria-live="polite" className="empty:hidden">
            {note ? <p className="alert alert-ok">{note}</p> : null}
          </div>
        </div>
      ) : null}
    </section>
  );
}