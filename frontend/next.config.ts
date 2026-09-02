import type { NextConfig } from "next";

/**
 * Deliberately close to empty.
 *
 * The dashboard talks to the backend directly from the browser (see
 * `src/lib/api.ts`), because the live agent trace is an `EventSource` and
 * proxying SSE through a Next rewrite adds a buffering layer that can hold
 * frames back — the one thing this UI cannot tolerate. CORS is already open for
 * `http://localhost:3000` on the FastAPI side, so no rewrite is needed.
 *
 * `NEXT_PUBLIC_API_BASE` is read at build time for the client bundle. In Docker
 * it must point at the host-visible backend (`http://localhost:8000`), not the
 * compose service name — the fetches happen in the judge's browser, not in the
 * container.
 */
const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Emit a self-contained server bundle so the Docker runtime layer carries only
  // what it needs. The Dockerfile copies `.next/standalone` + `.next/static`.
  output: "standalone",
};

export default nextConfig;
