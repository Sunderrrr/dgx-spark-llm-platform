// Server-side login gate for the authenticated pages (used by proxy.ts).
//
// Every page is client-rendered: without this gate a logged-out visitor got
// the home page's empty shell first, and was only sent to /login once a data
// fetch came back 401 — about a second of an empty dashboard. The proxy now
// answers that request with the redirect itself.
//
// Kept apart from proxy.ts so the decision can be unit-tested without Next.

/** The pages of app/(app)/. A page missing here still works: it only falls
 *  back to the client-side redirect (tests/session-gate.test.ts fails when
 *  the list drifts from the folder). */
export const PROTECTED_PAGES: ReadonlySet<string> = new Set([
  "/",
  "/admin",
  "/image",
  "/memory",
  "/music",
  "/ocr",
  "/playground",
  "/ranking",
  "/request",
  "/search",
  "/support",
  "/users",
  "/video",
  "/voice",
]);

/** Flask's session cookie (its default name, never overridden in app.py). */
export const SESSION_COOKIE = "session";

export type GateInput = {
  method: string;
  pathname: string;
  hasSessionCookie: boolean;
  /** A client-side navigation (RSC payload request): the document load
   *  already went through the gate, and an expired session is caught by the
   *  page's own 401 handling — no extra round-trip per click. */
  isRscRequest: boolean;
};

/** "redirect": send to /login now. "check": ask Flask. "pass": serve. */
export function gateDecision(i: GateInput): "redirect" | "check" | "pass" {
  if (i.method !== "GET" && i.method !== "HEAD") return "pass";
  if (i.isRscRequest) return "pass";
  const path = i.pathname.length > 1 ? i.pathname.replace(/\/+$/, "") : i.pathname;
  if (!PROTECTED_PAGES.has(path)) return "pass";
  return i.hasSessionCookie ? "check" : "redirect";
}

/** Outcome of the Flask check: only a definite 401 redirects. A timeout or an
 *  unreachable backend serves the page (fail open: the client-side redirect
 *  still applies, and an outage must not look like a logout). */
export function checkRedirects(status: number | null): boolean {
  return status === 401;
}
