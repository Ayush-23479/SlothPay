// The typed client for the SlothPay API. The dashboard talks to the backend only through
// this file. With NEXT_PUBLIC_USE_MOCKS=true every call is answered from lib/mocks.ts.

import { API_BASE_URL, USE_MOCKS } from "./config";
import { mockApi } from "./mocks";
import { getAdminKey, getOwnerId } from "./session";
import type {
  ApiErrorBody,
  BaselineIn,
  BaselineResult,
  ChaosSetIn,
  ChaosSettings,
  DoubleClickIn,
  DoubleClickResult,
  InvoiceResult,
  Metrics,
  Passport,
  Payment,
  PaymentCreate,
  PaymentEvent,
  PaymentList,
  Rescue,
  RescueActionResult,
  ShareLink,
  Speed,
} from "./types";

export class ApiRequestError extends Error {
  status: number;
  code: string;
  retryAfter: number | null;

  constructor(status: number, code: string, message: string, retryAfter: number | null = null) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
  }
}

export interface Api {
  createPayment(
    body: PaymentCreate,
    idempotencyKey: string,
  ): Promise<{ payment: Payment; replay: boolean }>;
  listPayments(limit?: number, cursor?: string | null): Promise<PaymentList>;
  getPayment(id: string): Promise<Payment>;
  listEvents(id: string, afterId?: string | null, limit?: number): Promise<PaymentEvent[]>;
  returnPayment(id: string): Promise<Payment>;
  verifyCondition(id: string, verified: boolean): Promise<Payment>;
  cancelPayment(id: string): Promise<Payment>;
  rescuePlan(id: string): Promise<Rescue>;
  rescueAction(id: string, action: string): Promise<RescueActionResult>;
  metrics(): Promise<Metrics>;
  speed(): Promise<Speed>;
  baselinePay(body: BaselineIn): Promise<BaselineResult>;
  getChaos(): Promise<ChaosSettings>;
  setChaos(body: ChaosSetIn): Promise<ChaosSettings>;
  doubleClick(body: DoubleClickIn): Promise<DoubleClickResult>;
  extractInvoice(text: string): Promise<InvoiceResult>;
  getPassport(id: string): Promise<Passport>;
  sharePassport(id: string): Promise<ShareLink>;
  getSharedPassport(token: string): Promise<Passport>;
}

interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  idempotencyKey?: string;
  admin?: boolean;
  signal?: AbortSignal;
}

/** Headers every call carries. The stream (lib/sse.ts) reuses them. */
export function authHeaders(admin = false): Record<string, string> {
  const headers: Record<string, string> = { "X-Owner-Id": getOwnerId() };
  if (admin) {
    const key = getAdminKey();
    if (key) headers["X-Admin-Api-Key"] = key;
  }
  return headers;
}

function readableError(status: number, body: unknown): ApiRequestError {
  const known = body as Partial<ApiErrorBody> | null;
  if (known && typeof known.error === "string" && typeof known.message === "string") {
    return new ApiRequestError(status, known.error, known.message, known.retry_after ?? null);
  }
  if (status === 422) {
    return new ApiRequestError(status, "invalid", "Please check the details and try again.");
  }
  return new ApiRequestError(status, "error", "Something went wrong. Please try again.");
}

let loginInFlight: Promise<boolean> | null = null;

/**
 * Start (or renew) the signed demo session cookie. The server ignores the X-Owner-Id header
 * everywhere except local development, so a hosted demo needs this cookie. The browser keeps
 * it (HttpOnly); this code never sees it.
 */
export function ensureSession(): Promise<boolean> {
  if (!loginInFlight) {
    loginInFlight = fetch(`${API_BASE_URL}/api/demo-login`, {
      method: "POST",
      credentials: "include",
      cache: "no-store",
    })
      .then((response) => response.ok)
      .catch(() => false)
      .finally(() => {
        loginInFlight = null;
      });
  }
  return loginInFlight;
}

async function request<T>(
  path: string,
  options: RequestOptions = {},
  retried = false,
): Promise<{ data: T; headers: Headers }> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...authHeaders(options.admin),
  };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
      cache: "no-store",
      credentials: "include",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiRequestError(0, "network", "Can't reach SlothPay right now. Check your connection.");
  }
  let data: unknown = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (response.status === 401 && !retried && !path.startsWith("/api/demo-login")) {
    // No valid session yet (first visit, or it expired): start one and try once more.
    if (await ensureSession()) return request<T>(path, options, true);
  }
  if (!response.ok) throw readableError(response.status, data);
  return { data: data as T, headers: response.headers };
}

const get = async <T,>(path: string, admin = false) =>
  (await request<T>(path, { admin })).data;
const post = async <T,>(path: string, body: unknown, extra: Partial<RequestOptions> = {}) =>
  (await request<T>(path, { method: "POST", body, ...extra })).data;

const base = "/api/payments";

const realApi: Api = {
  async createPayment(body, idempotencyKey) {
    const { data, headers } = await request<Payment>(base, {
      method: "POST",
      body,
      idempotencyKey,
    });
    return { payment: data, replay: headers.get("Idempotent-Replay") === "true" };
  },
  listPayments(limit = 20, cursor = null) {
    const query = new URLSearchParams({ limit: String(limit) });
    if (cursor) query.set("cursor", cursor);
    return get<PaymentList>(`${base}?${query}`);
  },
  getPayment: (id) => get<Payment>(`${base}/${encodeURIComponent(id)}`),
  listEvents(id, afterId = null, limit = 100) {
    const query = new URLSearchParams({ limit: String(limit) });
    if (afterId) query.set("after_id", afterId);
    return get<PaymentEvent[]>(`${base}/${encodeURIComponent(id)}/events?${query}`);
  },
  returnPayment: (id) => post<Payment>(`${base}/${encodeURIComponent(id)}/return`, undefined),
  verifyCondition: (id, verified) =>
    post<Payment>(`${base}/${encodeURIComponent(id)}/conditions/verify`, {
      verified,
      evidence: { source: "dashboard_demo" },
    }),
  cancelPayment: (id) => post<Payment>(`${base}/${encodeURIComponent(id)}/cancel`, undefined),
  rescuePlan: (id) => get<Rescue>(`${base}/${encodeURIComponent(id)}/rescue`),
  rescueAction: (id, action) =>
    post<RescueActionResult>(`${base}/${encodeURIComponent(id)}/rescue`, { action }),
  metrics: () => get<Metrics>("/api/metrics"),
  speed: () => get<Speed>("/api/speed"),
  baselinePay: (body) => post<BaselineResult>("/api/baseline/pay", body),
  getChaos: () => get<ChaosSettings>("/api/chaos", true),
  setChaos: (body) => post<ChaosSettings>("/api/chaos", body, { admin: true }),
  doubleClick: (body) => post<DoubleClickResult>("/api/chaos/double-click", body, { admin: true }),
  extractInvoice: (text) => post<InvoiceResult>("/api/invoices/extract", { text }),
  getPassport: (id) => get<Passport>(`${base}/${encodeURIComponent(id)}/passport`),
  sharePassport: (id) =>
    post<ShareLink>(`${base}/${encodeURIComponent(id)}/passport/share`, undefined),
  getSharedPassport: (token) => get<Passport>(`/api/passport/${encodeURIComponent(token)}`),
};

export const api: Api = USE_MOCKS ? mockApi : realApi;