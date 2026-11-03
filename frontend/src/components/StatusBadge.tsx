import type { DisplayStatus } from "@/lib/types";

// Colour is never the only signal: every badge has an icon shape and text.
const STYLE: Record<DisplayStatus, { label: string; cls: string; icon: string }> = {
  COMPLETED: { label: "Completed", cls: "bg-[var(--ok-bg)] text-[var(--ok)]", icon: "✓" },
  PENDING: { label: "Pending", cls: "bg-[var(--info-bg)] text-[var(--info)]", icon: "◔" },
  ACTION_NEEDED: { label: "Action needed", cls: "bg-[var(--warn-bg)] text-[var(--warn)]", icon: "!" },
  CLOSED: { label: "Closed", cls: "bg-[var(--closed-bg)] text-[var(--closed)]", icon: "–" },
};

export function StatusBadge({ status }: { status: DisplayStatus }) {
  const style = STYLE[status] ?? STYLE.PENDING;
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-bold ${style.cls}`}
    >
      <span aria-hidden="true">{style.icon}</span>
      {style.label}
    </span>
  );
}