import { formatDateTime } from "@/lib/format";
import type { PassportEntry } from "@/lib/types";

export function PassportTimeline({ timeline }: { timeline: PassportEntry[] }) {
  return (
    <section aria-labelledby="passport-timeline" className="card">
      <h2 id="passport-timeline" className="text-2xl font-extrabold">
        What happened
      </h2>
      <p className="mt-1 text-sm text-muted">
        <strong>Confirmed</strong> means PayPal told SlothPay (a verified webhook or a successful
        PayPal read). <strong>Recorded</strong> means SlothPay wrote it down itself.
      </p>
      {timeline.length === 0 ? (
        <p className="mt-4 rounded-2xl bg-soft p-4 text-muted">Nothing has been recorded yet.</p>
      ) : (
        <ol className="mt-5 space-y-5 border-l-2 border-line pl-5">
          {timeline.map((entry, index) => (
            <li key={entry.entry_hash} className="relative break-inside-avoid">
              <span
                aria-hidden="true"
                className={`absolute -left-[1.7rem] top-1.5 h-3.5 w-3.5 rounded-full border-2 border-white ${
                  entry.confirmed ? "bg-[var(--ok)]" : "bg-[#9aa5b1]"
                }`}
              />
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="text-base font-bold">{entry.title}</h3>
                <span
                  className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-bold ${
                    entry.confirmed
                      ? "bg-[var(--ok-bg)] text-[var(--ok)]"
                      : "bg-[var(--closed-bg)] text-[var(--closed)]"
                  }`}
                >
                  <span aria-hidden="true">{entry.confirmed ? "✓" : "•"}</span>
                  {entry.confirmed ? "Confirmed" : "Recorded"}
                </span>
              </div>
              <p className="text-sm text-muted">
                {entry.time ? (
                  <time dateTime={entry.time}>{formatDateTime(entry.time)}</time>
                ) : null}
                {" · "}
                {entry.source}
              </p>
              <p className="mt-1 text-sm">{entry.explanation}</p>
              <p className="mt-1 font-mono text-[0.7rem] text-muted">
                <span className="sr-only">Entry {index + 1} fingerprint </span>#
                {entry.entry_hash.slice(0, 12)}
              </p>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}