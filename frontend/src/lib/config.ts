// NEXT_PUBLIC_* values are read at build time and are visible in the browser.
// Never put a secret here. NEXT_PUBLIC_ADMIN_API_KEY is for the local sandbox demo only.
export const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000"
).replace(/\/+$/, "");

/** USE_MOCKS=true runs the whole dashboard on fixture data, with no backend. */
export const USE_MOCKS = process.env.NEXT_PUBLIC_USE_MOCKS === "true";