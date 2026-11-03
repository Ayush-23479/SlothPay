"use client";

import { useId, useRef, useState } from "react";
import { api, ApiRequestError } from "@/lib/api";
import type { InvoiceResult } from "@/lib/types";
import type { PayFormInitial } from "./PayForm";

interface Props {
  /** Fills the payment form. Nothing is paid until the person presses Create payment. */
  onPrefill: (values: PayFormInitial, warnings: string[]) => void;
}

const MAX_BYTES = 200_000;

export function InvoiceUpload({ onPrefill }: Props) {
  const uid = useId();
  const fileRef = useRef<HTMLInputElement>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<InvoiceResult | null>(null);

  async function onFile(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    if (file.size > MAX_BYTES) {
      setError("That file is too large. Paste just the invoice text instead.");
      return;
    }
    try {
      setText(await file.text());
      setError(null);
    } catch {
      setError("We could not read that file. Paste the invoice text instead.");
    }
  }

  async function read() {
    if (!text.trim()) {
      setError("Paste the invoice text or choose a text file first.");
      return;
    }
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await api.extractInvoice(text.trim()));
    } catch (e) {
      setError(
        e instanceof ApiRequestError ? e.message : "We could not read this invoice. Try again.",
      );
    }
    setBusy(false);
  }

  function use() {
    if (!result) return;
    const data = result.result;
    onPrefill(
      {
        amount: data.amount ?? undefined,
        currency: data.currency ?? undefined,
        description:
          [data.payee_name, data.invoice_number ? `Invoice ${data.invoice_number}` : null]
            .filter(Boolean)
            .join(" · ") || undefined,
      },
      [
        ...data.warnings,
        ...(data.confidence < 0.7 ? ["We are not very sure about these details."] : []),
      ],
    );
  }

  const data = result?.result;
  return (
    <section aria-labelledby={`${uid}-h`} className="card">
      <h2 id={`${uid}-h`} className="text-xl font-extrabold">
        Have an invoice?
      </h2>
      <p className="mt-1 text-sm text-muted">
        We read it and fill in the form for you. You check the details, then press Create payment.
        Nothing is paid automatically.
      </p>
      <label className="label mt-4" htmlFor={`${uid}-text`}>
        Invoice text
      </label>
      <textarea
        id={`${uid}-text`}
        className="field min-h-28"
        value={text}
        disabled={busy}
        onChange={(e) => setText(e.target.value)}
        placeholder={"From: Studio Nine Ltd\nInvoice no: INV-1042\nTotal due: 128.50 USD"}
      />
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <button type="button" className="btn btn-soft btn-sm" onClick={() => fileRef.current?.click()}>
          Choose a text file
        </button>
        <input
          ref={fileRef}
          type="file"
          accept=".txt,.csv,.md,text/plain"
          className="sr-only"
          tabIndex={-1}
          aria-label="Invoice text file"
          onChange={onFile}
        />
        <button type="button" className="btn btn-primary btn-sm" onClick={read} disabled={busy}>
          {busy ? "Reading…" : "Read invoice"}
        </button>
      </div>
      <div aria-live="polite" className="mt-3 empty:hidden">
        {error ? (
          <p className="alert alert-error" role="alert">
            {error}
          </p>
        ) : null}
        {data ? (
          <div className="alert alert-info">
            <p className="font-semibold">
              {result?.source === "model" ? "Read with AI" : "Read with simple rules"}
              {" · "}
              {Math.round(data.confidence * 100)}% sure
            </p>
            <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
              <dt className="font-semibold">Payee</dt>
              <dd>{data.payee_name ?? "–"}</dd>
              <dt className="font-semibold">Invoice</dt>
              <dd>{data.invoice_number ?? "–"}</dd>
              <dt className="font-semibold">Amount</dt>
              <dd>{data.amount ? `${data.amount} ${data.currency ?? ""}` : "–"}</dd>
            </dl>
            {data.warnings.length > 0 ? (
              <ul className="mt-2 list-disc pl-5 text-[#5c3700]">
                {data.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            ) : null}
            <button type="button" className="btn btn-blue btn-sm mt-3" onClick={use}>
              Fill the payment form
            </button>
          </div>
        ) : null}
      </div>
    </section>
  );
}