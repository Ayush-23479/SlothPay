// Fixture data and a tiny in-browser "backend", so the dashboard works with no server:
//   NEXT_PUBLIC_USE_MOCKS=true npm run dev
// State lives in sessionStorage so the PayPal return page (a fresh page load) sees the
// payment that was just created. Nothing here talks to PayPal or moves money.

import type { Api } from "./api";
import { readMockStore, writeMockStore } from "./session";
import type { StreamHandlers } from "./sse";
import type {
  ChaosSettings,
  DisplayStatus,
  InvoiceResult,
  Metrics,
  Payment,
  PaymentCreate,
  PaymentEvent,
  Rescue,
} from "./types";

interface Store {
  payments: Payment[];
  events: Record<string, PaymentEvent[]>;
  keys: Record<string, string>;
  chaos: ChaosSettings;
  seq: number;
}

const OFF: ChaosSettings = {
  mode: "none",
  probability: 1,
  delay_ms: 0,
  code: null,
  remaining_count: null,
};

const DISPLAY: Record<string, DisplayStatus> = {
  CAPTURED: "COMPLETED",
  CAPTURE_PENDING: "PENDING",
  APPROVED: "PENDING",
  AWAITING_CONDITION: "PENDING",
  AUTHORIZED: "PENDING",
  UNCERTAIN: "PENDING",
  INTENT_CREATED: "PENDING",
  ORDER_CREATED: "PENDING",
  AWAITING_APPROVAL: "ACTION_NEEDED",
  FAILED: "ACTION_NEEDED",
  VOIDED: "CLOSED",
  CANCELLED: "CLOSED",
};

const ALLOWED: Record<string, string[]> = {
  AWAITING_APPROVAL: ["ASK_BUYER_TO_APPROVE", "WAIT"],
  UNCERTAIN: ["WAIT", "RECONCILE"],
  FAILED: ["ASK_BUYER_NEW_METHOD", "CONTACT_SUPPORT"],
  AWAITING_CONDITION: ["WAIT"],
};

const mask = (id: string) => `${id.slice(0, 4).toUpperCase()}…${id.slice(-3).toUpperCase()}`;
const iso = (offsetMs = 0) => new Date(Date.now() + offsetMs).toISOString();
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

function makePayment(
  id: string,
  path: Payment["path"],
  amount: string,
  status: string,
  ageMinutes: number,
  extra: Partial<Payment> = {},
): Payment {
  const created = iso(-ageMinutes * 60_000);
  return {
    id,
    path,
    amount,
    currency: "USD",
    status,
    display_status: DISPLAY[status] ?? "PENDING",
    allowed_actions: ALLOWED[status] ?? [],
    approval_url: null,
    paypal_ids: {
      order_id: mask(`ord${id.replaceAll("-", "")}`),
      authorization_id: null,
      capture_id: status === "CAPTURED" ? mask(`cap${id.replaceAll("-", "")}`) : null,
    },
    condition: null,
    created_at: created,
    updated_at: created,
    ...extra,
  };
}

function seed(): Store {
  const store: Store = { payments: [], events: {}, keys: {}, chaos: { ...OFF }, seq: 0 };
  const done = makePayment("11111111-aaaa-4aaa-8aaa-111111111111", "SMARTPAY", "24.00", "CAPTURED", 95);
  const held = makePayment(
    "22222222-bbbb-4bbb-8bbb-222222222222",
    "CONDITIONALPAY",
    "180.00",
    "AWAITING_CONDITION",
    40,
    {
      condition: {
        kind: "delivery",
        description: "Parcel is delivered to the buyer",
        deadline_minutes: 120,
        deadline_at: iso(80 * 60_000),
      },
    },
  );
  const failed = makePayment("33333333-cccc-4ccc-8ccc-333333333333", "SMARTPAY", "60.00", "FAILED", 25);
  const closed = makePayment("44444444-dddd-4ddd-8ddd-444444444444", "CONDITIONALPAY", "75.50", "VOIDED", 180, {
    condition: {
      kind: "delivery",
      description: "Signed receipt uploaded",
      deadline_minutes: 60,
      deadline_at: iso(-120 * 60_000),
    },
  });
  store.payments = [failed, held, done, closed];
  const add = (p: Payment, rows: [string, string, string, string | null, string | null, boolean][]) => {
    store.events[p.id] = rows.map(([type, title, summary, from, to, confirmed], i) => ({
      id: `evt-${++store.seq}`,
      type,
      title,
      summary,
      from_status: from,
      to_status: to,
      source: confirmed ? "paypal" : "internal_record",
      confirmed,
      created_at: new Date(new Date(p.created_at).getTime() + i * 20_000).toISOString(),
    }));
  };
  add(done, [
    ["INTENT_CREATED", "Payment started", "Your $24.00 payment was recorded.", null, "INTENT_CREATED", false],
    ["STATE_TRANSITION", "PayPal order created", "PayPal accepted the order.", "INTENT_CREATED", "ORDER_CREATED", true],
    ["STATE_TRANSITION", "Approved", "PayPal confirmed you approved the payment.", "AWAITING_APPROVAL", "APPROVED", true],
    ["STATE_TRANSITION", "Paid", "PayPal confirmed the payment was captured.", "CAPTURE_REQUESTED", "CAPTURED", true],
  ]);
  add(held, [
    ["INTENT_CREATED", "Payment started", "Your $180.00 ConditionalPay was recorded.", null, "INTENT_CREATED", false],
    ["STATE_TRANSITION", "Funds held", "PayPal is holding the funds.", "AUTHORIZE_REQUESTED", "AUTHORIZED", true],
    ["STATE_TRANSITION", "Waiting for the condition", "The funds are released when the condition is met.", "AUTHORIZED", "AWAITING_CONDITION", false],
  ]);
  add(failed, [
    ["INTENT_CREATED", "Payment started", "Your $60.00 payment was recorded.", null, "INTENT_CREATED", false],
    ["STATE_TRANSITION", "Did not go through", "PayPal declined the payment method.", "CAPTURE_REQUESTED", "FAILED", true],
  ]);
  add(closed, [
    ["INTENT_CREATED", "Payment started", "Your $75.50 ConditionalPay was recorded.", null, "INTENT_CREATED", false],
    ["STATE_TRANSITION", "Hold released", "The deadline passed, so the hold was released.", "VOID_REQUESTED", "VOIDED", true],
  ]);
  return store;
}

let memory: Store | null = null;

function load(): Store {
  if (memory) return memory;
  const raw = readMockStore();
  if (raw) {
    try {
      memory = JSON.parse(raw) as Store;
      return memory;
    } catch {
      // fall through to a fresh seed
    }
  }
  memory = seed();
  save();
  return memory;
}

function save() {
  if (memory) writeMockStore(JSON.stringify(memory));
}

// ------------------------------------------------------------------ live updates
const listeners = new Map<string, Set<StreamHandlers>>();

export function mockSubscribe(paymentId: string, handlers: StreamHandlers): () => void {
  const set = listeners.get(paymentId) ?? new Set();
  set.add(handlers);
  listeners.set(paymentId, set);
  handlers.onState("live");
  return () => {
    set.delete(handlers);
    handlers.onState("closed");
  };
}

function addEvent(
  payment: Payment,
  title: string,
  summary: string,
  to: string | null,
  confirmed: boolean,
) {
  const store = load();
  const event: PaymentEvent = {
    id: `evt-${++store.seq}`,
    type: to ? "STATE_TRANSITION" : "NOTE",
    title,
    summary,
    from_status: to ? payment.status : null,
    to_status: to,
    source: confirmed ? "paypal" : "internal_record",
    confirmed,
    created_at: iso(),
  };
  (store.events[payment.id] ??= []).push(event);
  if (to) {
    payment.status = to;
    payment.display_status = DISPLAY[to] ?? "PENDING";
    payment.allowed_actions = ALLOWED[to] ?? [];
    payment.updated_at = event.created_at;
    if (to === "CAPTURED") {
      payment.paypal_ids.capture_id = mask(`cap${payment.id.replaceAll("-", "")}`);
    }
    if (to !== "AWAITING_APPROVAL") payment.approval_url = null;
  }
  save();
  for (const handlers of listeners.get(payment.id) ?? []) {
    handlers.onEvent(event);
    handlers.onSnapshot({ ...payment });
  }
}

function find(id: string): Payment {
  const payment = load().payments.find((item) => item.id === id);
  if (!payment) throw Object.assign(new Error("Payment not found"), { status: 404 });
  return payment;
}

async function failWith(status: number, error: string, message: string): Promise<never> {
  const { ApiRequestError } = await import("./api");
  throw new ApiRequestError(status, error, message);
}

// ----------------------------------------------------------------------- the API
export const mockApi: Api = {
  async createPayment(body: PaymentCreate, key: string) {
    await sleep(350);
    const store = load();
    const existing = store.keys[key];
    if (existing) return { payment: { ...find(existing) }, replay: true };

    const chaos = store.chaos;
    if (chaos.mode === "delay") await sleep(Math.min(chaos.delay_ms, 4000));

    const id = crypto.randomUUID();
    const payment = makePayment(id, body.path, Number(body.amount).toFixed(2), "INTENT_CREATED", 0, {
      condition: body.condition
        ? { ...body.condition, deadline_at: null }
        : null,
    });
    store.payments.unshift(payment);
    store.keys[key] = id;
    store.events[id] = [];
    addEvent(payment, "Payment started", `Your ${body.currency} ${payment.amount} payment was recorded.`, null, false);

    const consume = () => {
      if (store.chaos.remaining_count !== null) {
        store.chaos.remaining_count -= 1;
        if (store.chaos.remaining_count <= 0) store.chaos = { ...OFF };
      }
    };
    if (chaos.mode === "drop_after_send") {
      consume();
      addEvent(payment, "Result unknown", "PayPal got the request but the answer was lost. We are checking, not resending.", "UNCERTAIN", false);
    } else if (chaos.mode === "drop_before_send") {
      consume();
      addEvent(payment, "Not sent", "The connection dropped before the request left. Nothing happened, so it is safe to try again.", "FAILED", false);
    } else if (chaos.mode === "mock_error") {
      consume();
      addEvent(payment, "Did not go through", "PayPal declined the payment method.", "FAILED", true);
    } else {
      addEvent(payment, "PayPal order created", "PayPal accepted the order.", "AWAITING_APPROVAL", true);
      payment.approval_url = `${window.location.origin}/pay/return?payment_id=${id}&mock=1`;
      save();
    }
    return { payment: { ...payment }, replay: false };
  },

  async listPayments() {
    await sleep(150);
    return { items: load().payments.map((p) => ({ ...p })), next_cursor: null };
  },

  async getPayment(id) {
    await sleep(80);
    try {
      return { ...find(id) };
    } catch {
      return failWith(404, "not_found", "Payment not found");
    }
  },

  async listEvents(id, afterId = null) {
    await sleep(80);
    const rows = load().events[id] ?? [];
    if (!afterId) return rows.map((e) => ({ ...e }));
    const at = rows.findIndex((e) => e.id === afterId);
    return rows.slice(at + 1).map((e) => ({ ...e }));
  },

  async returnPayment(id) {
    await sleep(300);
    const payment = find(id);
    if (payment.status === "AWAITING_APPROVAL") {
      addEvent(payment, "Approved", "PayPal confirmed you approved the payment.", "APPROVED", true);
      if (payment.path === "SMARTPAY") {
        addEvent(payment, "Paid", "PayPal confirmed the payment was captured.", "CAPTURED", true);
      } else {
        addEvent(payment, "Funds held", "PayPal is holding the funds.", "AUTHORIZED", true);
        addEvent(payment, "Waiting for the condition", "The funds are released when the condition is met.", "AWAITING_CONDITION", false);
        if (payment.condition) {
          payment.condition.deadline_at = iso(payment.condition.deadline_minutes * 60_000);
          save();
        }
      }
    }
    return { ...payment };
  },

  async verifyCondition(id, verified) {
    await sleep(250);
    const payment = find(id);
    if (payment.status !== "AWAITING_CONDITION") {
      return failWith(409, "condition_not_ready", "The funds are not held yet; try again shortly");
    }
    if (verified) {
      addEvent(payment, "Condition met", "You confirmed the condition was met.", null, false);
      await sleep(600);
      addEvent(payment, "Paid", "PayPal confirmed the held funds were captured.", "CAPTURED", true);
    } else {
      addEvent(payment, "Condition not met", "You said the condition was not met.", null, false);
      await sleep(600);
      addEvent(payment, "Hold released", "PayPal confirmed the hold was released.", "VOIDED", true);
    }
    return { ...payment };
  },

  async cancelPayment(id) {
    await sleep(200);
    const payment = find(id);
    addEvent(payment, "Cancelled", "The payment was cancelled before anything was taken.", "CANCELLED", false);
    return { ...payment };
  },

  async rescuePlan(id): Promise<Rescue> {
    await sleep(150);
    const payment = find(id);
    const status = payment.status;
    if (status === "UNCERTAIN") {
      return {
        payment_id: id,
        display_status: "PENDING",
        facts: [
          "PayPal received the request but the answer was lost.",
          "We have not sent it a second time.",
          "We are asking PayPal what happened.",
        ],
        allowed_actions: ["WAIT", "RECONCILE"],
        recommended_action: "RECONCILE",
        needs_human: false,
        explanation: {
          message: "The connection dropped after PayPal got the request, so we do not know if it went through.",
          next_step: "Press Check with PayPal now. We will not charge twice.",
          source: "template",
        },
      };
    }
    if (status === "FAILED") {
      return {
        payment_id: id,
        display_status: "ACTION_NEEDED",
        facts: ["PayPal declined the payment method.", "No money was taken."],
        allowed_actions: ["ASK_BUYER_NEW_METHOD", "CONTACT_SUPPORT"],
        recommended_action: "ASK_BUYER_NEW_METHOD",
        needs_human: false,
        explanation: {
          message: "The bank behind this payment method said no, so PayPal could not take the payment.",
          next_step: "Ask the buyer to pick another payment method and start a new payment.",
          source: "model",
        },
      };
    }
    return {
      payment_id: id,
      display_status: payment.display_status,
      facts: ["The buyer has not approved the payment in PayPal yet."],
      allowed_actions: ALLOWED[status] ?? ["WAIT"],
      recommended_action: "ASK_BUYER_TO_APPROVE",
      needs_human: false,
      explanation: null,
    };
  },

  async rescueAction(id, action) {
    await sleep(400);
    const payment = find(id);
    if (action === "RECONCILE" && payment.status === "UNCERTAIN") {
      addEvent(payment, "Checked with PayPal", "PayPal confirmed the order exists. Nothing was sent twice.", "AWAITING_APPROVAL", true);
      payment.approval_url = `${window.location.origin}/pay/return?payment_id=${id}&mock=1`;
      save();
      return { action, result: { outcome: "reconciled" } };
    }
    if (action === "WAIT") return { action, result: { outcome: "waiting" } };
    if (action === "CONTACT_SUPPORT") return { action, result: { outcome: "support_requested" } };
    return { action, result: { outcome: "noted" } };
  },

  async metrics(): Promise<Metrics> {
    await sleep(150);
    const rows = load().payments;
    const completed = rows.filter((p) => p.status === "CAPTURED").length;
    return {
      total: rows.length,
      completed,
      uncertain_now: rows.filter((p) => p.status === "UNCERTAIN").length,
      recovered: 3 + Math.max(0, completed - 1),
      duplicates_suppressed: 7,
      retries: 2,
      capture_seconds_avg: 11.4,
      capture_seconds_p95: 24.8,
      capture_latency_label: "sandbox",
      webhook_verify_ms_avg: 212,
      webhook_verify_ms_p95: 388,
      generated_at: iso(),
    };
  },

  async getChaos() {
    await sleep(80);
    return { ...load().chaos };
  },

  async setChaos(body) {
    await sleep(120);
    const store = load();
    store.chaos =
      body.mode === "none"
        ? { ...OFF }
        : {
            mode: body.mode,
            probability: body.probability ?? 1,
            delay_ms: body.delay_ms ?? 0,
            code: body.code ?? null,
            remaining_count: body.remaining_count ?? null,
          };
    save();
    return { ...store.chaos };
  },

  async doubleClick(body) {
    await sleep(700);
    return {
      requested: body.n,
      accepted: body.n,
      replays: body.n - 1,
      busy: 0,
      failed: 0,
      intents: 1,
      paypal_orders: 1,
      safe: true,
    };
  },

  async extractInvoice(text): Promise<InvoiceResult> {
    await sleep(600);
    const amount = /(?:total|amount due|balance due)[^\d]{0,20}([\d,]+\.\d{2})/i.exec(text)?.[1];
    const currency = /\b(USD|EUR|GBP|INR)\b/.exec(text)?.[1] ?? (text.includes("$") ? "USD" : null);
    const number = /(?:invoice\s*(?:no\.?|number|#)\s*[:#]?\s*)([A-Za-z0-9-]+)/i.exec(text)?.[1] ?? null;
    const payee = /^\s*(?:from|payee|bill from)\s*[:\-]\s*(.+)$/im.exec(text)?.[1]?.trim() ?? null;
    if (!amount || !currency) {
      return {
        source: "template",
        result: {
          payee_name: payee,
          invoice_number: number,
          amount: null,
          currency: null,
          due_date: null,
          line_items: [],
          confidence: 0,
          warnings: ["We could not read this invoice. Please fill in the details yourself."],
          prefill_only: true,
        },
      };
    }
    return {
      source: "model",
      result: {
        payee_name: payee ?? "Studio Nine Ltd",
        invoice_number: number ?? "INV-1042",
        amount: amount.replaceAll(",", ""),
        currency,
        due_date: null,
        line_items: [],
        confidence: 0.82,
        warnings: [],
        prefill_only: true,
      },
    };
  },
};