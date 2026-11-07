"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { api, ApiRequestError } from "@/lib/api";
import { formatMoney, statusLabel } from "@/lib/format";
import { clearCheckoutFor } from "@/lib/session";
import { FINAL_STATUSES, type Payment } from "@/lib/types";
import { StatusBadge } from "./StatusBadge";

export function ReturnClient() {
  const params = useSearchParams();
  const paymentId = params.get("payment_id");
  const cancelled = params.get("cancelled") === "1";
  const [payment, setPayment] = useState<Payment | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!paymentId) return;
    let alive = true;
    (async () => {
      try {
        // Tell the server the buyer is back. It checks with PayPal; we only show the answer.
        const result = cancelled ? await api.getPayment(paymentId) : await api.returnPayment(paymentId);
        if (!alive) return;
        setPayment(result);
        if (FINAL_STATUSES.includes(result.display_status)) clearCheckoutFor(result.id);
      } catch (e) {
        if (alive) {
          setError(
            e instanceof ApiRequestError ? e.message : "We could not check this payment. Please try again.",
          );
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [paymentId, cancelled]);

  return (
    <div className="mx-auto max-w-xl px-4 py-16 sm:px-6">
      <div className="card text-center">
        <h1 className="text-3xl font-extrabold">
          {cancelled ? "You left PayPal" : "Checking your payment"}
        </h1>
        {!paymentId ? (
          <p className="alert alert-error mt-6" role="alert">
            This link is missing its payment. Go back to the dashboard and open the payment from there.
          </p>
        ) : error ? (
          <p className="alert alert-error mt-6" role="alert">
            {error}
          </p>
        ) : !payment ? (
          <div className="mt-6 space-y-2" aria-busy="true" role="status">
            <div className="skeleton mx-auto h-8 w-40" />
            <p className="text-sm text-muted">Asking PayPal what happened…</p>
          </div>
        ) : (
          <div className="mt-6 space-y-3" role="status">
            <p className="text-4xl font-extrabold">{formatMoney(payment.amount, payment.currency)}</p>
            <StatusBadge status={payment.display_status} />
            <p className="text-muted">{statusLabel(payment.status)}</p>
            {cancelled && payment.status === "AWAITING_APPROVAL" ? (
              <p className="alert alert-info">You did not approve it. Nothing was taken.</p>
            ) : null}
          </div>
        )}
        <div className="mt-8 flex flex-wrap justify-center gap-3">
          {payment ? (
            <Link href={`/payments/${payment.id}/passport`} className="btn btn-outline">
              View Trust Passport
            </Link>
          ) : null}
          <Link href="/" className="btn btn-primary">
            Back to dashboard
          </Link>
        </div>
      </div>
    </div>
  );
}