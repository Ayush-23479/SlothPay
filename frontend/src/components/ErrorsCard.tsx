import { formatDateTime } from "@/lib/format";
import type { PassportError } from "@/lib/types";

export function ErrorsCard({ errors }: { errors: PassportError[] }) {
  return (
    <section aria-labelledby="passport-errors" className="card break-inside-avoid">
      <h2 id="passport-errors" className="text-2xl font-extrabold">
        Problems along the way
      </h2>
      {errors.length === 0 ? (
        <p className="alert alert-ok mt-4">No errors were recorded for this payment.</p>
      ) : (
        <ul className="mt-4 space-y-4">
          {errors.map((error, index) => (
            <li
              key={`${error.time}-${index}`}
              className="break-inside-avoid rounded-2xl bg-soft p-4 text-sm"
            >
              <p className="font-bold">
                {error.plain_explanation}
                <span className="ml-2 rounded-full bg-white px-2 py-0.5 text-xs font-bold text-muted">
                  {error.explanation_source === "model" ? "AI explanation" : "Standard explanation"}
                </span>
              </p>
              {error.next_step ? <p className="mt-1">{error.next_step}</p> : null}
              <p className="mt-2 text-muted">
                <strong>What SlothPay did:</strong> {error.what_slothpay_did}
              </p>
              <p className="mt-1 text-xs text-muted">
                {error.time ? formatDateTime(error.time) : ""} · {error.operation} ·{" "}
                {error.error_class}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}