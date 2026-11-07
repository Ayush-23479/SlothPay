import type { PassportProtections } from "@/lib/types";

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-line py-2 last:border-0">
      <dt>{label}</dt>
      <dd className="text-xl font-extrabold">{value}</dd>
    </div>
  );
}

export function ProtectionsCard({ protections }: { protections: PassportProtections }) {
  return (
    <section aria-labelledby="passport-protections" className="card break-inside-avoid">
      <h2 id="passport-protections" className="text-2xl font-extrabold">
        How SlothPay protected this payment
      </h2>
      <dl className="mt-3 text-sm">
        <Row label="Duplicate requests blocked" value={String(protections.duplicate_replays)} />
        <Row label="Blind retries after a timeout" value={String(protections.blind_retries)} />
        <Row
          label="Safe retries (only after PayPal confirmed nothing was sent)"
          value={String(protections.safe_retries)}
        />
        <Row
          label="Checked with PayPal after a lost answer"
          value={protections.reconciled ? "Yes" : "Not needed"}
        />
      </dl>
      {protections.paypal_request_ids_used.length > 0 ? (
        <div className="mt-4 text-sm">
          <p className="font-bold">Request ids sent to PayPal (masked)</p>
          <p className="text-muted">
            One fixed id per step. Sending it again can never create a second charge.
          </p>
          <ul className="mt-2 flex flex-wrap gap-2">
            {protections.paypal_request_ids_used.map((id) => (
              <li key={id} className="rounded-full bg-soft px-3 py-1 font-mono text-xs">
                {id}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}