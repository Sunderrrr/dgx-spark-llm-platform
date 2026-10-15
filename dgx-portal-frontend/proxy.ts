import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { SESSION_COOKIE, checkRedirects, gateDecision } from "./lib/session-gate";
import { backendFor, onRust } from "./lib/backend";

// Paths with their own Route Handler (app/**/route.ts) — proxy must NOT
// rewrite these itself, or it would bypass their streaming-safe logic (see
// those files: a plain rewrite here was confirmed to buffer SSE responses).
const OWN_ROUTE_HANDLERS = new Set([
  "/playground/chat",
  "/support/chat",
  "/admin/runner/stream",
  "/api/ocr/extract",
]);

// The Flask check is a few ms on the compose network; past this, serve the
// page anyway (lib/session-gate.ts explains why failing open is right).
const SESSION_CHECK_TIMEOUT_MS = 1500;

function toLogin(request: NextRequest, setCookie?: string | null): NextResponse {
  // Next rejects a relative Location (500 "Invalid URL"). nextUrl takes its
  // host from the Host header Traefik forwards (the public one); the scheme
  // comes from X-Forwarded-Proto, the container itself only speaks http.
  const url = request.nextUrl.clone();
  url.pathname = "/login";
  url.search = "";
  const proto = request.headers.get("x-forwarded-proto");
  if (proto === "https" || proto === "http") url.protocol = `${proto}:`;
  const res = NextResponse.redirect(url, 307);
  // Flask clears an expired/revoked session on its 401: pass that on, so the
  // next visit skips the check entirely.
  if (setCookie) res.headers.set("Set-Cookie", setCookie);
  return res;
}

export async function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // /internal/* (notably /internal/authcheck) is only ever meant to be called
  // BY Traefik's forwardAuth on the internal network — never via this public
  // frontend. The catch-all route handler (app/internal/[...path]/route.ts)
  // blocks GET/HEAD, but a non-GET request would be rewritten to Flask by the
  // method branch below BEFORE Next.js route matching runs, defeating that
  // guard. Deny the whole prefix here, ahead of the rewrite.
  if (pathname === "/internal" || pathname.startsWith("/internal/")) {
    return new NextResponse(null, { status: 404 });
  }

  // Every page in this app is a client-rendered React page — none of them
  // handle their own POST. All real form/API actions live in Flask.
  // next.config.ts's `fallback` rewrite only fires when a request matches NO
  // Next.js route, so a POST to a path that IS a page (e.g. /login, /keys)
  // was being served Next's own cached GET markup instead of ever reaching
  // Flask. Proxy runs before Next's own routing, so it can catch these by
  // method instead of maintaining a list of colliding paths.
  if (request.method !== "GET" && request.method !== "HEAD" && !OWN_ROUTE_HANDLERS.has(pathname)) {
    return NextResponse.rewrite(new URL(pathname + request.nextUrl.search, backendFor(pathname, request.method)));
  }
  // Rust migration: a prefix moved to the Rust portal is served there for
  // every method (lib/backend.ts) — the rewrites of next.config only know Flask.
  if (onRust(pathname, request.method) && !OWN_ROUTE_HANDLERS.has(pathname)) {
    return NextResponse.rewrite(new URL(pathname + request.nextUrl.search, backendFor(pathname, request.method)));
  }

  // A logged-out visitor is sent to /login before any page markup goes out,
  // instead of seeing the empty home page until a data fetch answers 401.
  const gate = gateDecision({
    method: request.method,
    pathname,
    hasSessionCookie: request.cookies.has(SESSION_COOKIE),
    isRscRequest: request.headers.has("rsc"),
  });
  if (gate === "redirect") return toLogin(request);
  if (gate === "check") {
    let status: number | null = null;
    let setCookie: string | null = null;
    try {
      const r = await fetch(new URL("/api/session", backendFor("/api/session")), {
        headers: { cookie: request.headers.get("cookie") ?? "" },
        redirect: "manual",
        cache: "no-store",
        signal: AbortSignal.timeout(SESSION_CHECK_TIMEOUT_MS),
      });
      status = r.status;
      setCookie = r.headers.get("set-cookie");
    } catch {
      status = null;
    }
    if (checkRedirects(status)) return toLogin(request, setCookie);
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
    // blob: — replaying a mic recording before sending (Voix page),
    // created via URL.createObjectURL; 'self' alone would be enough for the
    // generated audio, served by Flask on /voice/audio/<id>.
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
  // Security headers on Next-served pages, matching what Flask sets on its own
  // responses (app.py). Without these, the React pages (served by Next, not
  // Flask) shipped no HSTS — a downgrade window on first contact. NB: the
  // authoritative public HSTS still depends on Cloudflare passing it through
  // (its edge currently forces max-age=0 — fix in the CF dashboard); this
  // covers the origin and any direct-to-Traefik hit.
  response.headers.set("Strict-Transport-Security", "max-age=63072000; includeSubDomains");
  response.headers.set("X-Content-Type-Options", "nosniff");
  response.headers.set("X-Frame-Options", "DENY");
  response.headers.set("Referrer-Policy", "same-origin");
  // Powerful features: the dictation needs the microphone, and file inputs offer
  // the camera on mobile — both stay for 'self'; the rest is denied.
  response.headers.set(
    "Permissions-Policy",
    "geolocation=(), payment=(), usb=(), camera=(self), microphone=(self)",
  );
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
