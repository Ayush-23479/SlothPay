"use client";

import { useState } from "react";
import { api, ApiRequestError } from "@/lib/api";
import { formatDateTime, formatMoney } from "@/lib/format";
import type { Passport, ShareLink } from "@/lib/types";
import { StatusBadge } from "./StatusBadge";

interface Props {
  passport: Passport;
  /** Needed for the owner's Copy share link button. */
  paymentId?: string;
}

/** The outcome comes first, in large type. */
export function PassportHeader({ passport, paymentId }: Props) {
  const { summary, integrity } = passport;
  const owner = passport.view === "owner" && Boolean(paymentId);
  const [busy, setBusy] = useState(false);
  const [link, setLink] = useState<ShareLink | null>(null);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  async function share() {
    if (!paymentId) return;
    setBusy(true);
    setMessage(null);
    try {
      const made = await api.sharePassport(paymentId);
      setLink(made);
      try {
        await navigator.clipboard.writeText(made.url);
        setMessage({ tone: "ok", text: "Link copied. It works for 7 days, with no login." });
      } catch {
        setMessage({ tone: "ok", text: "Link ready. Copy it from the box below." });
      }
    } catch (e) {
      setMessage({
        tone: "error",
        text: e instanceof ApiRequestError ? e.message : "Could not make a share link.",
      });
    }
    setBusy(false);
  }

  return (
    <header className="bg-sky print:bg-white">
      <div className="mx-auto max-w-4xl px-4 pb-10 pt-10 sm:px-6 sm:pt-14">
        <p className="text-sm font-bold uppercase tracking-wide text-ink/70">
          SlothPay Trust Passport
          {passport.view === "shared" ? " · shared view" : ""}
        </p>
        <h1 className="mt-3 text-[2.25rem] font-extrabold leading-[1.08] tracking-tight text-ink sm:text-6xl">
          {summary.final_outcome}
        </h1>
        <div className="mt-5 flex flex-wrap items-center gap-3">
          <StatusBadge status={summary.display_status} />
          <span className="text-2xl font-extrabold">
            {formatMoney(summary.amount, summary.currency)}
          </span>
          <span className="text-ink/80">
            {summary.path === "SMARTPAY" ? "SmartPay" : "ConditionalPay"}
            {summary.payment_id ? ` · ${summary.payment_id}` : ""}
          </span>
        </div>
        <p className="mt-4 text-sm text-ink/80">
          {integrity.chain_valid ? (
            <>
              <span aria-hidden="true">✓ </span>Record check passed: no entry has been changed.
            </>
          ) : (
            <strong className="text-[var(--bad)]">
              <span aria-hidden="true">✕ </span>Record check failed: an entry was changed after it
              was written. Do not rely on this page.
            </strong>
          )}
          {integrity.head ? (
            <span className="ml-2 font-mono text-xs">#{integrity.head.slice(0, 12)}</span>
          ) : null}
        </p>

        <div className="no-print mt-6 flex flex-wrap items-center gap-3">
          {owner ? (
            <button type="button" className="btn btn-primary" onClick={share} disabled={busy}>
              {busy ? "Making link…" : "Copy share link"}
            </button>
          ) : null}
          <button type="button" className="btn btn-outline" onClick={() => window.print()}>
            Print or save as PDF
          </button>
        </div>
        <div aria-live="polite" className="no-print mt-3 empty:hidden">
          {message ? (
            <p
              className={`alert ${message.tone === "ok" ? "alert-ok" : "alert-error"}`}
              role={message.tone === "error" ? "alert" : "status"}
            >
              {message.text}
            </p>
          ) : null}
          {link ? (
            <div className="mt-3">
              <label className="label" htmlFor="share-link">
                Share link (expires {formatDateTime(link.expires_at)})
              </label>
              <input
                id="share-link"
                className="field font-mono text-sm"
                readOnly
                value={link.url}
                onFocus={(e) => e.currentTarget.select()}
              />
            </div>
          ) : null}
        </div>
      </div>
    </header>
  );
}