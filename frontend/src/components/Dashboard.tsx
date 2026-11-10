"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, ApiRequestError } from "@/lib/api";
import { formatMoney } from "@/lib/format";
import { clearCheckoutFor } from "@/lib/session";
import { FINAL_STATUSES, type Payment } from "@/lib/types";
import { ChaosPanel } from "./ChaosPanel";
import { ConditionPanel } from "./ConditionPanel";
import { InvoiceUpload } from "./InvoiceUpload";
import { LiveTimeline } from "./LiveTimeline";
import { MetricsPanel } from "./MetricsPanel";
import { SpeedPanel } from "./SpeedPanel";
import { PayForm, type PayFormInitial } from "./PayForm";
import { PaymentTable } from "./PaymentTable";
import { RescuePanel } from "./RescuePanel";
import { StatusBadge } from "./StatusBadge";

interface Prefill {
  nonce: number;
  values: PayFormInitial;
  notices: string[];
}

export function Dashboard() {
  const [payments, setPayments] = useState<Payment[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [prefill, setPrefill] = useState<Prefill>({ nonce: 0, values: {}, notices: [] });
  const [metricsKey, setMetricsKey] = useState(0);

  const load = useCallback(async () => {
    try {
      const page = await api.listPayments(20);
      setPayments(page.items);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiRequestError ? e.message : "We could not load your payments.");
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch; state is set after the await
    void load();
  }, [load]);

  const upsert = useCallback((payment: Payment) => {
    setPayments((current) => {
      const rest = (current ?? []).filter((p) => p.id !== payment.id);
      const existing = (current ?? []).findIndex((p) => p.id === payment.id);
      if (existing === -1) return [payment, ...rest];
      const copy = [...(current ?? [])];
      copy[existing] = payment;
      return copy;
    });
    if (FINAL_STATUSES.includes(payment.display_status)) clearCheckoutFor(payment.id);
  }, []);

  const selected = payments?.find((p) => p.id === selectedId) ?? null;

  function created(payment: Payment) {
    upsert(payment);
    setSelectedId(payment.id);
    setMetricsKey((n) => n + 1);
  }

  function changed(payment: Payment) {
    upsert(payment);
    setMetricsKey((n) => n + 1);
  }

  return (
    <>
      {/* Hero: sky-blue band, huge headline, big card overlapping the edge. */}
      <section id="pay" className="bg-sky pb-0 pt-12 sm:pt-16" aria-labelledby="hero-h">
        <div className="mx-auto max-w-6xl px-4 sm:px-6">
          <h1
            id="hero-h"
            className="max-w-3xl text-[2.5rem] font-extrabold leading-[1.05] tracking-tight text-ink sm:text-6xl"
          >
            Pay once. Never twice.
          </h1>
          <p className="mt-4 max-w-xl text-lg text-ink/80">
            SlothPay sits on top of PayPal. If the network drops or you click twice, your money is
            safe and the status is always honest.
          </p>
          <div className="mt-10 translate-y-12 sm:mt-12">
            <div className="grid gap-6 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
              <PayForm
                key={prefill.nonce}
                initial={prefill.values}
                notices={prefill.notices}
                onCreated={created}
              />
              <InvoiceUpload
                onPrefill={(values, notices) =>
                  setPrefill((p) => ({ nonce: p.nonce + 1, values, notices }))
                }
              />
            </div>
          </div>
        </div>
      </section>

      <div className="mx-auto max-w-6xl space-y-8 px-4 pb-16 pt-24 sm:px-6 sm:pt-28">
        <div className="grid gap-8 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
          <section id="payments" aria-labelledby="payments-h" className="card">
            <div className="flex items-center justify-between gap-3">
              <h2 id="payments-h" className="text-2xl font-extrabold">
                Your payments
              </h2>
              <button type="button" className="btn btn-soft btn-sm" onClick={() => void load()}>
                Refresh
              </button>
            </div>
            <div className="mt-4">
              <PaymentTable
                payments={payments}
                error={error}
                selectedId={selectedId}
                onSelect={setSelectedId}
                onRetry={() => void load()}
              />
            </div>
          </section>

          <div id="safety" className="space-y-8">
            <MetricsPanel refreshKey={metricsKey} />
            <SpeedPanel refreshKey={metricsKey} />
            <ChaosPanel />
          </div>
        </div>

        {selected ? (
          <section aria-labelledby="detail-h" className="card">
            <div className="flex flex-wrap items-center gap-3">
              <h2 id="detail-h" className="text-2xl font-extrabold">
                {formatMoney(selected.amount, selected.currency)}
              </h2>
              <StatusBadge status={selected.display_status} />
              <div className="ml-auto flex flex-wrap gap-2">
                <Link href={`/payments/${selected.id}/passport`} className="btn btn-outline btn-sm">
                  Open Trust Passport
                </Link>
                <button
                  type="button"
                  className="btn btn-soft btn-sm"
                  onClick={() => setSelectedId(null)}
                >
                  Close
                </button>
              </div>
            </div>
            {selected.status === "AWAITING_APPROVAL" && selected.approval_url ? (
              <p className="alert alert-info mt-4">
                Waiting for approval in PayPal.{" "}
                <a className="font-bold underline" href={selected.approval_url}>
                  Continue to PayPal
                </a>
              </p>
            ) : null}
            <div className="mt-6 grid gap-8 lg:grid-cols-2">
              <LiveTimeline key={selected.id} paymentId={selected.id} onPayment={upsert} />
              <div className="space-y-6">
                <RescuePanel key={selected.id} payment={selected} onChanged={changed} />
                <ConditionPanel payment={selected} onChanged={changed} />
              </div>
            </div>
          </section>
        ) : null}
      </div>
    </>
  );
}