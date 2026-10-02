import type { NextConfig } from "next";
import path from "node:path";

const BACKEND_URL = process.env.BACKEND_URL || "http://dgx-portal:5000";

// Content-Security-Policy is set per-request in proxy.ts instead of here: it
// needs a fresh nonce on script-src for every render, which a static header
// value can't provide.

const nextConfig: NextConfig = {
  output: "standalone",
  // Removes the X-Powered-By: Next.js header — no functional value,
  // just free fingerprinting handed to an attacker.
  poweredByHeader: false,
  // /root/package-lock.json (Astryx sandbox, outside this project) otherwise
  // makes Next drift to the wrong workspace root.
  turbopack: {
    root: path.join(__dirname),
  },
  experimental: {
    // The proxy.ts middleware (routes ALL non-GET/HEAD to Flask) silently
    // truncates request bodies beyond 10 MB by default — it broke the image
    // uploads of /api/video/generate and /api/ocr/extract (advertised
    // limit: 15 MB). Raised to match + margin. Seen in prod on 04/08:
    // an ~11 MB screenshot crashed the proxy with ECONNRESET.
    proxyClientMaxBodySize: "20mb",
    // Next cuts EVERY proxied request at 30 s by default
    // (server/lib/router-utils/proxy-request.ts: `proxyTimeout || 30_000`) and
    // then answers « 500 Internal Server Error » — on the client side it is
    // indistinguishable from an outage. Voice cloning of a long text goes well
    // beyond 30 s (~45 s for 1 500 characters): the generation succeeded
    // and was properly saved, but the user saw an error and relaunched,
    // stacking generations. Seen in prod on 05/08.
    // Intended order of the timeouts: proxy 300 s > gunicorn 200 s > /tts call
    // 120 s, so that the most INTERNAL timeout wins and returns a real message.
    proxyTimeout: 300_000,
  },
  async rewrites() {
    // The browser only talks to this Next.js server (same origin, no CORS to
    // handle for cookies/CSRF). Anything that is not a known Next page
    // (JSON API, /login POST, /playground/chat SSE, /admin/*, etc.) is passed
    // as-is to Flask, reachable internally over the docker-compose network.
    return {
      fallback: [{ source: "/:path*", destination: `${BACKEND_URL}/:path*` }],
    };
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "same-origin" },
        ],
      },
    ];
  },
};

export default nextConfig;
