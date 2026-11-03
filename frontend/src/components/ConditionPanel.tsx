"use client";

import { useState } from "react";
import { api, ApiRequestError } from "@/lib/api";
import { formatCountdown } from "@/lib/format";
import { useNow } from "@/lib/useNow";
import type { Payment } from "@/lib/types";

interface Props {
  payment: Payment;
  onChanged: (payment: Payment) => void;
}

export function ConditionPanel({ payment, onChanged }: Props) {
  const now = useNow();
  const [busy, setBusy] = useState<"verify" | "fail" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const condition = payment.condition;
  if (payment.path !== "CONDITIONALPAY" || !condition) return null;

  const waiting = payment.status === "AWAITING_CONDITION";
  const deadline = condition.deadline_at ? new Date(condition.deadline_at).getTime() : null;
  const left = deadline !== null && now > 0 ? deadline - now : null;

  async function decide(verified: boolean) {
    setBusy(verified ? "verify" : "fail");
    setError(null);
    try {
      onChanged(await api.verifyCondition(payment.id, verified));
    } catch (e) {
      setError(e instanceof ApiRequestError ? e.message : "That did not work. Please try again.");
    }
    setBusy(null);
  }

  return (
    <section aria-labelledby={`cond-${payment.id}`} className="rounded-2xl bg-soft p-4">
      <h3 id={`cond-${payment.id}`} className="text-lg font-extrabold">
        Release condition
      </h3>
      <p className="mt-1 font-semibold">{condition.description}</p>
      <p className="text-sm text-muted">
        Type: {condition.kind} · Deadline: {condition.deadline_minutes} minutes after approval
      </p>
      {waiting ? (
        <>
          <p className="mt-3 text-sm font-bold" role="timer" aria-live="off">
            {left === null ? "Countdown starts once the funds are held." : formatCountdown(left)}
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              type="button"
              className="btn btn-blue btn-sm"
              disabled={busy !== null}
              onClick={() => decide(true)}
            >
              {busy === "verify" ? "Working…" : "Verify: condition met"}
            </button>
            <button
              type="button"
              className="btn btn-danger btn-sm"
              disabled={busy !== null}
              onClick={() => decide(false)}
            >
              {busy === "fail" ? "Working…" : "Fail: condition not met"}
            </button>
          </div>
        </>
      ) : (
        <p className="mt-3 text-sm text-muted">
          {payment.status === "CAPTURED"
            ? "The condition was met and the funds were taken."
            : payment.status === "VOIDED"
              ? "The hold was released."
              : "The funds are not held yet. The buttons appear once PayPal confirms the hold."}
        </p>
      )}
      {error ? (
        <p className="alert alert-error mt-3" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  );
}