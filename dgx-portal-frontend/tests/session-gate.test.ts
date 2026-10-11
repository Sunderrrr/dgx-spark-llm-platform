/* The server-side login gate (lib/session-gate.ts, used by proxy.ts): a
 * logged-out visitor must be redirected before any page markup is sent, and
 * nothing else (API calls, client navigations, /login itself) may be caught.
 *
 * Runs with the package's `npm test`: `node --test tests/*.test.ts`. */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { PROTECTED_PAGES, checkRedirects, gateDecision } from "../lib/session-gate.ts";

const base = { method: "GET", hasSessionCookie: false, isRscRequest: false };

test("logged-out document request to a page is redirected at once", () => {
  for (const p of ["/", "/playground", "/admin", "/admin/"]) {
    assert.equal(gateDecision({ ...base, pathname: p }), "redirect", p);
  }
});

test("a session cookie is checked against Flask, not trusted", () => {
  assert.equal(gateDecision({ ...base, pathname: "/", hasSessionCookie: true }), "check");
});

test("login, API, assets, POSTs and client navigations are never gated", () => {
  for (const p of ["/login", "/login/sso", "/api/whoami", "/playground/chat",
                   "/admin/runner/stream", "/.well-known/security.txt", "/healthz"]) {
    assert.equal(gateDecision({ ...base, pathname: p }), "pass", p);
  }
  assert.equal(gateDecision({ ...base, pathname: "/", method: "POST" }), "pass");
  assert.equal(gateDecision({ ...base, pathname: "/", isRscRequest: true }), "pass");
});

test("only a definite 401 redirects; an unreachable backend serves the page", () => {
  assert.equal(checkRedirects(401), true);
  for (const s of [204, 200, 403, 500, 503, null]) assert.equal(checkRedirects(s), false, String(s));
});

test("the protected list matches the pages of app/(app)", () => {
  const root = join(import.meta.dirname, "..", "app", "(app)");
  const found = new Set<string>();
  const walk = (dir: string, prefix: string) => {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name);
      if (!statSync(full).isDirectory()) {
        if (name === "page.tsx") found.add(prefix || "/");
        continue;
      }
      if (name.startsWith("_")) continue;
      walk(full, `${prefix}/${name}`);
    }
  };
  walk(root, "");
  assert.deepEqual([...found].sort(), [...PROTECTED_PAGES].sort());
});
