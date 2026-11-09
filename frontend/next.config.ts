import type { NextConfig } from "next";

// BACKEND_URL is read at BUILD time (it is baked into the routes of the built app).
//
// Unset (local development): the browser calls the API directly at NEXT_PUBLIC_API_BASE_URL
// (http://localhost:8000), exactly as before.
//
// Set (hosted demo, for example on Render): the browser calls its OWN origin at /api/... and
// this server forwards the call to the API. The login cookie then belongs to the frontend's
// domain, so it is a first-party cookie. Without this, a frontend and an API on two different
// *.onrender.com hostnames are different sites and the browser would drop the SameSite=Lax
// session cookie (see docs/TASK16_SECURITY.md, caveat 1).
const backendUrl = (process.env.BACKEND_URL ?? "").trim().replace(/\/+$/, "");

if (backendUrl && !/^https?:\/\/[^/\s]+$/.test(backendUrl)) {
  throw new Error("BACKEND_URL must look like https://host (no path, no trailing text).");
}

const nextConfig: NextConfig = {
  /* config options here */
  cacheComponents: true,
  partialPrefetching: true,
  // A small self-contained server for the Docker image (see frontend/Dockerfile).
  output: "standalone",
  // Compression buffers small chunks, which delays the live status stream (Server-Sent Events)
  // that is forwarded through the proxy below. Render's edge compresses for browsers anyway.
  compress: false,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  async rewrites() {
    if (!backendUrl) return [];
    return [{ source: "/api/:path*", destination: `${backendUrl}/api/:path*` }];
  },
};

export default nextConfig;
