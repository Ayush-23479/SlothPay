// Small browser-only helpers: who is using the demo, and the checkout idempotency key.
// Everything here runs from event handlers or effects, never while a page is prerendered.

const OWNER_KEY = "slothpay_owner";
const CHECKOUT_KEY = "slothpay_checkout";
const ADMIN_KEY = "slothpay_admin_key";

function read(store: Storage | undefined, key: string): string | null {
  try {
    return store?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function write(store: Storage | undefined, key: string, value: string | null) {
  try {
    if (value === null) store?.removeItem(key);
    else store?.setItem(key, value);
  } catch {
    // Storage can be blocked (private windows). The app still works without it.
  }
}

const local = () => (typeof window === "undefined" ? undefined : window.localStorage);
const session = () => (typeof window === "undefined" ? undefined : window.sessionStorage);

/** The demo identity sent as X-Owner-Id. Stable per browser. */
export function getOwnerId(): string {
  const existing = read(local(), OWNER_KEY);
  if (existing && /^[A-Za-z0-9_.@:-]{1,128}$/.test(existing)) return existing;
  const fresh = `demo-${crypto.randomUUID()}`;
  write(local(), OWNER_KEY, fresh);
  return fresh;
}

export function getAdminKey(): string | null {
  return (
    read(session(), ADMIN_KEY) ?? process.env.NEXT_PUBLIC_ADMIN_API_KEY ?? null
  );
}

export function setAdminKey(value: string | null) {
  write(session(), ADMIN_KEY, value && value.trim() ? value.trim() : null);
}

interface Checkout {
  key: string;
  orderId: string;
  fingerprint: string;
  paymentId?: string;
}

function readCheckout(): Checkout | null {
  const raw = read(session(), CHECKOUT_KEY);
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Checkout;
    return typeof value.key === "string" && typeof value.fingerprint === "string"
      ? value
      : null;
  } catch {
    return null;
  }
}

/**
 * ONE Idempotency-Key per checkout. It is kept in sessionStorage until the payment reaches a
 * final status, so a second click or a refresh after a freeze reuses it. If the person
 * changes the details (a different fingerprint) it is a new checkout with a new key,
 * because the same key with a different body would be refused.
 */
export function getCheckout(fingerprint: string): { key: string; orderId: string } {
  const current = readCheckout();
  if (current && current.fingerprint === fingerprint) {
    return { key: current.key, orderId: current.orderId };
  }
  const fresh: Checkout = {
    key: crypto.randomUUID(),
    orderId: `web-${crypto.randomUUID()}`,
    fingerprint,
  };
  write(session(), CHECKOUT_KEY, JSON.stringify(fresh));
  return { key: fresh.key, orderId: fresh.orderId };
}

export function rememberCheckoutPayment(paymentId: string) {
  const current = readCheckout();
  if (current) write(session(), CHECKOUT_KEY, JSON.stringify({ ...current, paymentId }));
}

/** Forget the key once the payment it started has reached a final status. */
export function clearCheckoutFor(paymentId: string) {
  const current = readCheckout();
  if (current?.paymentId === paymentId) write(session(), CHECKOUT_KEY, null);
}

export function readMockStore(): string | null {
  return read(session(), "slothpay_mock_store");
}

export function writeMockStore(value: string) {
  write(session(), "slothpay_mock_store", value);
}