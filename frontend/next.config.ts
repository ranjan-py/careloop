import type { NextConfig } from "next";

/**
 * REST requests from the browser go to same-origin `/api/*` and are proxied
 * server-side to the backend (keeps the session cookie same-origin, no CORS).
 * BACKEND_URL is read at build/dev-server start:
 *   - dev on host:        http://localhost:8000 (default below)
 *   - docker compose:     http://backend:8000   (set as build arg / env)
 * The WebSocket connects directly to the backend host (see src/lib/ws.ts) —
 * Next.js rewrites do not reliably proxy WebSockets in standalone output.
 */
const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  output: "standalone",
  // Anchor file tracing to this package so standalone output stays correct
  // even when a stray lockfile exists in a parent directory.
  outputFileTracingRoot: __dirname,
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${BACKEND_URL}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
