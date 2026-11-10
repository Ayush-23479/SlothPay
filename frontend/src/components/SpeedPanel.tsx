"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Speed } from "@/lib/types";

function ms(value: number | null): string {
  if (value === null) return "–";
  return value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${value.toFixed(0)} ms`;
}

function Row({
  label,
  who,
  p50,
  p95,
}: {
  label: string;
  who: string;
  p50: number | null;
  p95: number | null;
}) {
  return (
    <tr className="border-t border-line">
      <th scope="row" className="py-2 pr-3 text-left font-semibold">
        {label}
        <span className="block text-xs font-normal text-muted">{who}</span>
      </th>
      <td className="py-2 pr-3 text-right tabular-nums">{ms(p50)}</td>
      <td className="py-2 text-right tabular-nums">{ms(p95)}</td>
    </tr>
  );
}

/** Where the time of your payments went. All sandbox figures, split by who spent the time. */
export function SpeedPanel({ refreshKey }: { refreshKey: number }) {
  const [speed, setSpeed] = useState<Speed | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api
        .speed()
        .then((value) => {
          if (cancelled) return;
          setSpeed(value);
          setError(null);
        })
        .catch(() => {
          if (!cancelled) setError("We could not load the speed numbers.");
        });
    void load();
    const timer = setInterval(load, 15_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [refreshKey]);

  const target = speed?.targets.overhead_p95_ms ?? 100;
  const p95 = speed?.overhead.p95_ms ?? null;

  return (
    <section aria-labelledby="speed-h" className="card">
      <div className="flex flex-wrap items-center gap-2">
        <h2 id="speed-h" className="text-xl font-extrabold">
          Speed
        </h2>
        <span className="rounded-full bg-soft px-3 py-1 text-xs font-bold uppercase tracking-wide">
          Sandbox
        </span>
      </div>
      {error && !speed ? (
        <p className="alert alert-error mt-3" role="alert">
          {error}
        </p>
      ) : !speed ? (
        <div className="skeleton mt-3 h-40" aria-busy="true" />
      ) : speed.payments === 0 ? (
        <p className="mt-3 text-sm text-muted">
          No timings yet. Make a payment and the time it took will show up here.
        </p>
      ) : (
        <>
          <table className="mt-3 w-full text-sm">
            <caption className="sr-only">Time per payment, split by who spent it</caption>
            <thead>
              <tr className="text-xs uppercase tracking-wide text-muted">
                <th scope="col" className="text-left font-bold">
                  Whose time
                </th>
                <th scope="col" className="text-right font-bold">
                  Typical
                </th>
                <th scope="col" className="text-right font-bold">
                  Slowest 5%
                </th>
              </tr>
            </thead>
            <tbody>
              <Row label="SlothPay" who="our own work" p50={speed.overhead.p50_ms} p95={p95} />
              <Row label="PayPal" who="waiting for PayPal" p50={speed.provider.p50_ms} p95={speed.provider.p95_ms} />
              <Row label="Buyer" who="approving at PayPal" p50={speed.human.p50_ms} p95={speed.human.p95_ms} />
            </tbody>
          </table>
          <p className="mt-3 text-sm" role="status">
            SlothPay adds <strong>{ms(p95)}</strong> at the slowest 5% (aim: under {target} ms){" "}
            {p95 !== null && p95 < target ? "– on target." : "."}
          </p>
          <p className="mt-1 text-xs text-muted">
            {speed.payments} payments measured. Sandbox figures: PayPal&apos;s sandbox is slower than
            the live service, so read these as SlothPay&apos;s added time, not PayPal&apos;s speed.
          </p>
          <p className="mt-2 text-sm">
            <a href="/compare" className="font-semibold text-brand underline">
              Compare with a payment that has no safety layer
            </a>
          </p>
        </>
      )}
    </section>
  );
}