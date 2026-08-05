import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL || "http://dgx-portal:5000";

// Paths with their own Route Handler (app/**/route.ts) — proxy must NOT
// rewrite these itself, or it would bypass their streaming-safe logic (see
// those files: a plain rewrite here was confirmed to buffer SSE responses).
const OWN_ROUTE_HANDLERS = new Set([
  "/playground/chat",
  "/support/chat",
  "/admin/runner/stream",
  "/api/ocr/extract",
]);

export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // Every page in this app is a client-rendered React page — none of them
  // handle their own POST. All real form/API actions live in Flask.
  // next.config.ts's `fallback` rewrite only fires when a request matches NO
  // Next.js route, so a POST to a path that IS a page (e.g. /login, /keys)
  // was being served Next's own cached GET markup instead of ever reaching
  // Flask. Proxy runs before Next's own routing, so it can catch these by
  // method instead of maintaining a list of colliding paths.
  if (request.method !== "GET" && request.method !== "HEAD" && !OWN_ROUTE_HANDLERS.has(pathname)) {
    return NextResponse.rewrite(new URL(pathname + request.nextUrl.search, BACKEND_URL));
  }

  // Nonce-based CSP on script-src, generated fresh per request. style-src
  // keeps 'unsafe-inline': this app has no StyleX/Tailwind compiler, so
  // style={{}} props (used throughout) compile to real inline style="..."
  // attributes — nonces can't cover those without a large refactor, and CSS
  // alone can't execute script, so the residual risk is low. Requires every
  // page to render dynamically (see app/layout.tsx's `dynamic` export) —
  // Next.js injects nonces during SSR from the CSP header on the request,
  // which a statically-prerendered page never sees.
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const csp = [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'`,
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self'",
    "img-src 'self' data: blob:",
    // blob: — relecture d'un enregistrement micro avant envoi (page Voix),
    // créé via URL.createObjectURL ; 'self' seul suffirait pour l'audio généré,
    // servi par Flask sur /voice/audio/<id>.
    "media-src 'self' blob:",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ].join("; ");

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", csp);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  matcher: [
    {
      source: "/((?!_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
