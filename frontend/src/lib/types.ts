// Types that mirror backend/api/schemas.py (Task 10, plus the Task 12 and 13 additions).
// Money is always a string with two decimals ("10.00"). Event ids are opaque cursors.

export type DisplayStatus = "COMPLETED" | "PENDING" | "ACTION_NEEDED" | "CLOSED";
export type PathName = "SMARTPAY" | "CONDITIONALPAY";
export type RescueActionName =
  | "WAIT"
  | "RECONCILE"
  | "RETRY_SAME_REQUEST"
  | "CAPTURE"
  | "VOID"
  | "ASK_BUYER_TO_APPROVE"
  | "ASK_BUYER_NEW_METHOD"
  | "CONTACT_SUPPORT";

// ------------------------------------------------------------------ requests
export interface ConditionIn {
  kind: string;
  description: string;
  deadline_minutes: number;
}

export interface PaymentCreate {
  path: PathName;
  amount: string;
  currency: string;
  description?: string | null;
  condition?: ConditionIn | null;
  merchant_order_id?: string | null;
}

// ----------------------------------------------------------------- responses
export interface PayPalIds {
  order_id: string | null;
  authorization_id: string | null;
  capture_id: string | null;
}

export interface ConditionOut {
  kind: string;
  description: string;
  deadline_minutes: number;
  deadline_at: string | null;
}

export interface Payment {
  id: string;
  path: PathName;
  amount: string;
  currency: string;
  status: string;
  display_status: DisplayStatus;
  allowed_actions: string[];
  approval_url: string | null;
  paypal_ids: PayPalIds;
  condition: ConditionOut | null;
  created_at: string;
  updated_at: string;
}

export interface PaymentList {
  items: Payment[];
  next_cursor: string | null;
}

export interface PaymentEvent {
  id: string;
  type: string;
  title: string;
  summary: string;
  from_status: string | null;
  to_status: string | null;
  source: string;
  confirmed: boolean;
  created_at: string;
}

export interface Explanation {
  message: string;
  next_step: string;
  source: "model" | "template";
}

export interface Rescue {
  payment_id: string;
  display_status: DisplayStatus;
  facts: string[];
  allowed_actions: string[];
  recommended_action: string;
  needs_human: boolean;
  explanation: Explanation | null;
}

export interface RescueActionResult {
  action: string;
  result: Record<string, unknown>;
}

export interface Metrics {
  total: number;
  completed: number;
  uncertain_now: number;
  recovered: number;
  duplicates_suppressed: number;
  retries: number;
  capture_seconds_avg: number | null;
  capture_seconds_p95: number | null;
  capture_latency_label: string;
  webhook_verify_ms_avg: number | null;
  webhook_verify_ms_p95: number | null;
  safety_overhead_ms_p95: number | null;
  condition_to_capture_ms_p95: number | null;
  generated_at: string;
}

// ------------------------------------------------------------------ speed (Task 19)
export interface KindStats {
  p50_ms: number | null;
  p95_ms: number | null;
}

export interface StepStats extends KindStats {
  kind: "ours" | "provider" | "human" | "wait";
  count: number;
}

export interface SpeedRecent {
  payment_id: string;
  path: string;
  amount: string;
  currency: string;
  created_at: string;
  ours_ms: number;
  provider_ms: number;
  human_ms: number;
}

export interface Speed {
  label: string;
  payments: number;
  ours: KindStats;
  provider: KindStats;
  human: KindStats;
  overhead: KindStats;
  steps: Record<string, StepStats>;
  recent: SpeedRecent[];
  targets: { overhead_p95_ms: number; condition_to_capture_ms: number; first_check_ms: number };
  note: string;
  generated_at: string;
}

export interface BaselineIn {
  amount?: string;
  currency?: string;
  timeout_seconds?: number;
}

export interface BaselineResult {
  arm: string;
  label: string;
  outcome: string;
  attempts: number;
  paypal_orders_possible: number;
  order_ids: (string | null)[];
  waited_ms: number;
  provider_ms: number;
  ours_ms: number;
  total_ms: number;
  timeout_seconds: number;
  request_id_used: boolean;
  note: string;
}

export type ChaosMode =
  | "none"
  | "delay"
  | "drop_before_send"
  | "drop_after_send"
  | "mock_error";

export interface ChaosSettings {
  mode: ChaosMode;
  probability: number;
  delay_ms: number;
  code: string | null;
  remaining_count: number | null;
}

export interface ChaosSetIn {
  mode: ChaosMode;
  probability?: number;
  delay_ms?: number;
  code?: string | null;
  remaining_count?: number | null;
}

export interface DoubleClickIn {
  n: number;
  payment: PaymentCreate;
}

export interface DoubleClickResult {
  requested: number;
  accepted: number;
  replays: number;
  busy: number;
  failed: number;
  intents: number;
  paypal_orders: number;
  safe: boolean;
}

export interface InvoiceLine {
  description: string;
  amount: string;
}

export interface InvoicePrefill {
  payee_name: string | null;
  invoice_number: string | null;
  amount: string | null;
  currency: string | null;
  due_date: string | null;
  line_items: InvoiceLine[];
  confidence: number;
  warnings: string[];
  prefill_only: true;
}

export interface InvoiceResult {
  result: InvoicePrefill;
  source: "model" | "template";
}

// ------------------------------------------------------------ Trust Passport (Task 14)
export interface PassportEntry {
  time: string | null;
  title: string;
  status_change: { from: string | null; to: string } | null;
  source: string;
  confirmed: boolean;
  explanation: string;
  entry_hash: string;
}

export interface PassportError {
  time: string | null;
  operation: string;
  error_class: string;
  plain_explanation: string;
  next_step: string | null;
  explanation_source: "model" | "template";
  what_slothpay_did: string;
}

export interface PassportCondition {
  required: string | null;
  status: string;
  verified_at: string | null;
  evidence_summary: string;
}

export interface PassportProtections {
  duplicate_replays: number;
  paypal_request_ids_used: string[];
  blind_retries: number;
  safe_retries: number;
  reconciled: boolean;
}

export interface Passport {
  view: "owner" | "shared";
  summary: {
    payment_id: string | null;
    path: PathName;
    amount: string;
    currency: string;
    display_status: DisplayStatus;
    final_outcome: string;
  };
  timeline: PassportEntry[];
  condition: PassportCondition | null;
  errors: PassportError[];
  protections: PassportProtections;
  integrity: { chain_valid: boolean; head: string | null };
}

export interface ShareLink {
  token: string;
  url: string;
  expires_at: string;
}

export interface ApiErrorBody {
  error: string;
  message: string;
  retry_after?: number | null;
}

export type StreamState = "connecting" | "live" | "reconnecting" | "closed";

export const FINAL_STATUSES: DisplayStatus[] = ["COMPLETED", "CLOSED"];