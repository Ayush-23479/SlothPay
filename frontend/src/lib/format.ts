const MONEY: Record<string, string> = { USD: "$", EUR: "€", GBP: "£", INR: "₹" };

export function formatMoney(amount: string, currency: string): string {
  const symbol = MONEY[currency];
  return symbol ? `${symbol}${amount}` : `${amount} ${currency}`;
}

export function formatTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatCountdown(ms: number): string {
  if (ms <= 0) return "Deadline passed";
  const total = Math.floor(ms / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return hours > 0 ? `${hours}h ${pad(minutes)}m ${pad(seconds)}s` : `${minutes}m ${pad(seconds)}s`;
}

/** "5.20" or null from the API numbers; keeps two decimals. */
export function formatNumber(value: number | null, digits = 1): string {
  return value === null ? "–" : value.toFixed(digits);
}

export const STATUS_LABEL: Record<string, string> = {
  INTENT_CREATED: "Payment started",
  ORDER_CREATED: "PayPal order created",
  AWAITING_APPROVAL: "Waiting for you to approve",
  APPROVED: "Approved",
  CAPTURE_REQUESTED: "Taking the payment",
  CAPTURE_PENDING: "PayPal is finishing the payment",
  CAPTURED: "Paid",
  AUTHORIZE_REQUESTED: "Holding the funds",
  AUTHORIZED: "Funds held",
  AWAITING_CONDITION: "Waiting for the condition",
  VOID_REQUESTED: "Releasing the hold",
  VOIDED: "Hold released",
  UNCERTAIN: "Checking the result with PayPal",
  FAILED: "Did not go through",
  CANCELLED: "Cancelled",
};

export function statusLabel(status: string): string {
  return STATUS_LABEL[status] ?? status.replaceAll("_", " ").toLowerCase();
}

export const ACTION_LABEL: Record<string, string> = {
  WAIT: "Wait and watch",
  RECONCILE: "Check with PayPal now",
  RETRY_SAME_REQUEST: "Try the same request again",
  CAPTURE: "Take the payment",
  VOID: "Release the hold",
  ASK_BUYER_TO_APPROVE: "Ask the buyer to approve",
  ASK_BUYER_NEW_METHOD: "Ask for another payment method",
  CONTACT_SUPPORT: "Contact support",
};