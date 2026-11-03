import { formatDateTime, formatMoney, statusLabel } from "@/lib/format";
import type { Payment } from "@/lib/types";
import { StatusBadge } from "./StatusBadge";

interface Props {
  payments: Payment[] | null;
  error: string | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onRetry: () => void;
}

export function PaymentTable({ payments, error, selectedId, onSelect, onRetry }: Props) {
  if (error) {
    return (
      <div className="alert alert-error" role="alert">
        <p>{error}</p>
        <button type="button" className="btn btn-outline btn-sm mt-3" onClick={onRetry}>
          Try again
        </button>
      </div>
    );
  }
  if (payments === null) {
    return (
      <div className="space-y-2" aria-busy="true" aria-label="Loading payments">
        {[0, 1, 2].map((i) => (
          <div key={i} className="skeleton h-14" />
        ))}
      </div>
    );
  }
  if (payments.length === 0) {
    return (
      <p className="rounded-2xl bg-soft p-6 text-center text-muted">
        No payments yet. Start one above and it will show up here.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[34rem] border-collapse text-left text-sm">
        <caption className="sr-only">Your payments, newest first. Select one to see its details.</caption>
        <thead>
          <tr className="border-b border-line text-xs uppercase tracking-wide text-muted">
            <th scope="col" className="py-2 pr-3 font-bold">Amount</th>
            <th scope="col" className="py-2 pr-3 font-bold">Type</th>
            <th scope="col" className="py-2 pr-3 font-bold">Status</th>
            <th scope="col" className="py-2 pr-3 font-bold">Started</th>
            <th scope="col" className="py-2 font-bold"><span className="sr-only">Details</span></th>
          </tr>
        </thead>
        <tbody>
          {payments.map((p) => {
            const selected = p.id === selectedId;
            return (
              <tr
                key={p.id}
                className={`border-b border-line ${selected ? "bg-[var(--info-bg)]" : ""}`}
              >
                <td className="py-3 pr-3 text-base font-extrabold">
                  {formatMoney(p.amount, p.currency)}
                </td>
                <td className="py-3 pr-3">
                  {p.path === "SMARTPAY" ? "SmartPay" : "ConditionalPay"}
                </td>
                <td className="py-3 pr-3">
                  <div className="flex flex-col items-start gap-1">
                    <StatusBadge status={p.display_status} />
                    <span className="text-xs text-muted">{statusLabel(p.status)}</span>
                  </div>
                </td>
                <td className="py-3 pr-3 text-muted">{formatDateTime(p.created_at)}</td>
                <td className="py-3 text-right">
                  <button
                    type="button"
                    className="btn btn-soft btn-sm"
                    aria-pressed={selected}
                    aria-label={`${selected ? "Showing" : "Show"} details for ${formatMoney(p.amount, p.currency)} payment`}
                    onClick={() => onSelect(p.id)}
                  >
                    {selected ? "Showing" : "Details"}
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}