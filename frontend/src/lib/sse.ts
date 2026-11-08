// Live updates for one payment (server-sent events).
//
// The browser's own EventSource cannot send the X-Owner-Id header, so this reads the stream
// with fetch. It reconnects by itself with a growing delay and sends Last-Event-ID so the
// server replays what was missed. Events are de-duplicated by id.

import { authHeaders, ensureSession } from "./api";
import { API_BASE_URL, USE_MOCKS } from "./config";
import { mockSubscribe } from "./mocks";
import type { Payment, PaymentEvent, StreamState } from "./types";

export interface StreamHandlers {
  afterId?: string | null;
  onEvent: (event: PaymentEvent) => void;
  onSnapshot: (payment: Payment) => void;
  onState: (state: StreamState) => void;
}

const MIN_DELAY_MS = 1000;
const MAX_DELAY_MS = 15000;

interface Frame {
  id?: string;
  event: string;
  data: string;
}

/** Split a buffer into complete SSE frames. Returns the frames and the unfinished rest. */
export function parseFrames(buffer: string): { frames: Frame[]; rest: string } {
  const normalized = buffer.replaceAll("\r\n", "\n");
  const parts = normalized.split("\n\n");
  const rest = parts.pop() ?? "";
  const frames: Frame[] = [];
  for (const part of parts) {
    const frame: Frame = { event: "message", data: "" };
    for (const line of part.split("\n")) {
      if (!line || line.startsWith(":")) continue; // comment or ping
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
      if (field === "id") frame.id = value;
      else if (field === "event") frame.event = value;
      else if (field === "data") frame.data += (frame.data ? "\n" : "") + value;
    }
    if (frame.data) frames.push(frame);
  }
  return { frames, rest };
}

/** Start listening. Returns a function that stops it. */
export function openPaymentStream(paymentId: string, handlers: StreamHandlers): () => void {
  if (USE_MOCKS) return mockSubscribe(paymentId, handlers);

  const controller = new AbortController();
  const seen = new Set<string>();
  let lastId = handlers.afterId ?? null;
  let delay = MIN_DELAY_MS;
  let stopped = false;

  const wait = (ms: number) =>
    new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, ms);
      controller.signal.addEventListener("abort", () => {
        clearTimeout(timer);
        resolve();
      });
    });

  async function run() {
    let first = true;
    let renewed = false;
    while (!stopped) {
      handlers.onState(first ? "connecting" : "reconnecting");
      first = false;
      try {
        const headers: Record<string, string> = {
          Accept: "text/event-stream",
          ...authHeaders(),
        };
        if (lastId) headers["Last-Event-ID"] = lastId;
        const response = await fetch(
          `${API_BASE_URL}/api/payments/${encodeURIComponent(paymentId)}/stream`,
          { headers, signal: controller.signal, cache: "no-store", credentials: "include" },
        );
        if (response.status === 401 && !renewed) {
          renewed = true; // one try with a fresh session before giving up
          if (await ensureSession()) continue;
        }
        if (!response.ok || !response.body) {
          if (response.status === 401 || response.status === 404) {
            handlers.onState("closed"); // nothing to retry: not ours, or it does not exist
            return;
          }
          throw new Error(`stream status ${response.status}`);
        }
        handlers.onState("live");
        delay = MIN_DELAY_MS;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!stopped) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const { frames, rest } = parseFrames(buffer);
          buffer = rest;
          for (const frame of frames) {
            try {
              if (frame.event === "payment") {
                const event = JSON.parse(frame.data) as PaymentEvent;
                if (frame.id) lastId = frame.id;
                if (!seen.has(event.id)) {
                  seen.add(event.id);
                  handlers.onEvent(event);
                }
              } else if (frame.event === "snapshot") {
                handlers.onSnapshot(JSON.parse(frame.data) as Payment);
              }
            } catch {
              // A damaged frame is skipped; the next snapshot corrects the screen.
            }
          }
        }
      } catch {
        if (stopped) return;
      }
      if (stopped) return;
      handlers.onState("reconnecting");
      await wait(delay);
      delay = Math.min(delay * 2, MAX_DELAY_MS);
    }
  }

  void run();
  return () => {
    stopped = true;
    controller.abort();
    handlers.onState("closed");
  };
}