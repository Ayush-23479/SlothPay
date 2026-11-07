import { formatDateTime } from "@/lib/format";
import type { PassportCondition } from "@/lib/types";

const LABEL: Record<string, { text: string; cls: string }> = {
  VERIFIED: { text: "Condition met", cls: "bg-[var(--ok-bg)] text-[var(--ok)]" },
  PENDING: { text: "Waiting", cls: "bg-[var(--info-bg)] text-[var(--info)]" },
  FAILED: { text: "Not met", cls: "bg-[var(--warn-bg)] text-[var(--warn)]" },
};

/** Only ConditionalPay payments have one. SmartPay passes null and nothing is drawn. */
export function ConditionCard({ condition }: { condition: PassportCondition | null }) {
  if (!condition) return null;
  const label = LABEL[condition.status] ?? LABEL.FAILED;
  return (
    <section aria-labelledby="passport-condition" className="card break-inside-avoid">
      <div className="flex flex-wrap items-center gap-3">
        <h2 id="passport-condition" className="text-2xl font-extrabold">
          The condition
        </h2>
        <span className={`rounded-full px-3 py-1 text-xs font-bold ${label.cls}`}>
          {label.text}
        </span>
      </div>
      <dl className="mt-4 space-y-3 text-sm">
        <div>
          <dt className="font-bold">Required</dt>
          <dd className="text-muted">{condition.required ?? "Not recorded"}</dd>
        </div>
        <div>
          <dt className="font-bold">Evidence</dt>
          <dd className="text-muted">{condition.evidence_summary}</dd>
        </div>
        {condition.verified_at ? (
          <div>
            <dt className="font-bold">Verified at</dt>
            <dd className="text-muted">{formatDateTime(condition.verified_at)}</dd>
          </div>
        ) : null}
      </dl>
    </section>
  );
}