"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import type { Metrics } from "@/lib/types";

function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-2xl bg-soft p-4">
      <dt className="text-xs font-bold uppercase tracking-wide text-muted">{label}</dt>
      <dd className="mt-1 text-3xl font-extrabold">{value}</dd>
      {sub ? <p className="text-xs text-muted">{sub}</p> : null}
    </div>
  );
}

export function MetricsPanel({ refreshKey }: { refreshKey: number }) {
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api
        .metrics()
        .then((value) => {
          if (cancelled) return;
          setMetrics(value);
          setError(null);
        })
        .catch(() => {
          if (!cancelled) setError("We could not load the numbers.");
        });
    void load();
    const timer = setInterval(load, 10_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [refreshKey]);

  return (
    <section aria-labelledby="metrics-h" className="card">
      <h2 id="metrics-h" className="text-xl font-extrabold">
        Safety numbers
      </h2>
      {error && !metrics ? (
        <p className="alert alert-error mt-3" role="alert">
          {error}
        </p>
      ) : !metrics ? (
        <div className="mt-3 grid grid-cols-2 gap-3" aria-busy="true">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="skeleton h-24" />
          ))}
        </div>
      ) : (
        <>
          <dl className="mt-3 grid grid-cols-2 gap-3">
            <Tile label="Payments" value={String(metrics.total)} sub={`${metrics.completed} completed`} />
            <Tile label="Checking now" value={String(metrics.uncertain_now)} sub="result unknown" />
            <Tile label="Recovered" value={String(metrics.recovered)} sub="after a lost answer" />
            <Tile label="Duplicates stopped" value={String(metrics.duplicates_suppressed)} />
          </dl>
          <p className="mt-4 text-sm text-muted">
            Time to capture (<strong>{metrics.capture_latency_label}</strong>): average{" "}
            {formatNumber(metrics.capture_seconds_avg)} s, slowest 5% {formatNumber(metrics.capture_seconds_p95)} s.
            Webhook check: average {formatNumber(metrics.webhook_verify_ms_avg, 0)} ms.
          </p>
          <p className="mt-1 text-xs text-muted">
            Sandbox figures. Real PayPal timings will differ. {metrics.retries} safe retries so far.
          </p>
        </>
      )}
    </section>
  );
}