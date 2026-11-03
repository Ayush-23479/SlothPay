"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { formatTime } from "@/lib/format";
import { openPaymentStream } from "@/lib/sse";
import type { Payment, PaymentEvent, StreamState } from "@/lib/types";

interface Props {
  paymentId: string;
  /** Called with each fresh payment snapshot (and after each event). */
  onPayment: (payment: Payment) => void;
}

const STATE_TEXT: Record<StreamState, string> = {
  connecting: "Connecting…",
  live: "Live",
  reconnecting: "Reconnecting…",
  closed: "Not live",
};

/** Use key={paymentId} so a new payment starts with a clean list. */
export function LiveTimeline({ paymentId, onPayment }: Props) {
  const [events, setEvents] = useState<PaymentEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [state, setState] = useState<StreamState>("connecting");
  const callback = useRef(onPayment);
  useEffect(() => {
    callback.current = onPayment;
  });

  useEffect(() => {
    let stop: (() => void) | null = null;
    let cancelled = false;
    const add = (incoming: PaymentEvent[]) =>
      setEvents((current) => {
        const known = new Set((current ?? []).map((e) => e.id));
        const fresh = incoming.filter((e) => !known.has(e.id));
        return fresh.length === 0 && current ? current : [...(current ?? []), ...fresh];
      });

    api
      .listEvents(paymentId)
      .then((rows) => {
        if (cancelled) return;
        add(rows);
        stop = openPaymentStream(paymentId, {
          afterId: rows.length > 0 ? rows[rows.length - 1].id : null,
          onEvent: (event) => add([event]),
          onSnapshot: (payment) => callback.current(payment),
          onState: (next) => {
            if (!cancelled) setState(next);
          },
        });
      })
      .catch(() => {
        if (!cancelled) setError("We could not load the history. Refresh to try again.");
      });
    return () => {
      cancelled = true;
      stop?.();
    };
  }, [paymentId]);

  return (
    <section aria-labelledby={`tl-${paymentId}`}>
      <div className="flex items-center justify-between gap-3">
        <h3 id={`tl-${paymentId}`} className="text-lg font-extrabold">
          What happened
        </h3>
        <span
          className="inline-flex items-center gap-2 text-xs font-bold text-muted"
          role="status"
        >
          <span
            aria-hidden="true"
            className={`h-2.5 w-2.5 rounded-full ${
              state === "live" ? "bg-[var(--ok)]" : "bg-[#b88a00]"
            }`}
          />
          {STATE_TEXT[state]}
        </span>
      </div>

      {error ? (
        <p className="alert alert-error mt-3" role="alert">
          {error}
        </p>
      ) : events === null ? (
        <div className="mt-3 space-y-2" aria-busy="true">
          <div className="skeleton h-12" />
          <div className="skeleton h-12" />
        </div>
      ) : events.length === 0 ? (
        <p className="mt-3 text-sm text-muted">Nothing has happened yet.</p>
      ) : (
        <ol className="mt-3 space-y-3 border-l-2 border-line pl-4">
          {events.map((event) => (
            <li key={event.id} className="relative">
              <span
                aria-hidden="true"
                className={`absolute -left-[1.4rem] top-1.5 h-3 w-3 rounded-full border-2 border-white ${
                  event.confirmed ? "bg-[var(--ok)]" : "bg-[#9aa5b1]"
                }`}
              />
              <div className="flex flex-wrap items-center gap-2">
                <p className="font-bold">{event.title}</p>
                <span
                  className={`rounded-full px-2 py-0.5 text-[0.7rem] font-bold ${
                    event.confirmed
                      ? "bg-[var(--ok-bg)] text-[var(--ok)]"
                      : "bg-[var(--closed-bg)] text-[var(--closed)]"
                  }`}
                >
                  {event.confirmed ? "Confirmed by PayPal" : "Recorded by SlothPay"}
                </span>
                <time className="text-xs text-muted" dateTime={event.created_at}>
                  {formatTime(event.created_at)}
                </time>
              </div>
              <p className="text-sm text-muted">{event.summary}</p>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}