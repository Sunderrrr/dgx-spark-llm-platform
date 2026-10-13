#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Journey « security » — closed holes, checked from the outside.

From the outside (the same path as any visitor: Cloudflare → Traefik):
  - /metrics is NOT served publicly (an explicit refusal, not a 200);
  - /internal/authcheck (Traefik's forwardAuth) is NOT reachable from outside
    the proxy's network — neither on the web domain nor on the API domain;
  - security.txt (RFC 9116) is published, current and carries a contact;
  - EVERY login-protected GET route of the portal refuses an anonymous
    visitor (the list is read from the source, so a new route is covered the
    day it is written), and a state-changing POST without a CSRF token is
    refused;
  - the session cookies carry Secure, HttpOnly and SameSite.
"""
import ast
import datetime
import os
import re
import sys

import common as c

NAME = "security"
TIME_LIMIT_S = 120

EXPECTED_REFUSALS = (401, 403, 404)
PORTAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "dgx-portal")
GUARDS = {"login_required", "admin_required", "owner_required"}


def protected_routes():
    """(method, rule) of every route behind an auth decorator, read from the
    portal's source — path parameters filled with a harmless dummy."""
    out = []
    for name in sorted(os.listdir(PORTAL_DIR)):
        if not name.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(PORTAL_DIR, name), encoding="utf-8").read())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            names = {getattr(d, "id", getattr(d, "attr", "")) for d in fn.decorator_list}
            if not names & GUARDS:
                continue
            for d in fn.decorator_list:
                if not (isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "route"):
                    continue
                rule = d.args[0].value
                methods = ["GET"]
                for kw in d.keywords:
                    if kw.arg == "methods":
                        methods = [m.value for m in kw.value.elts]
                path = re.sub(r"<int:[^>]+>", "1", rule)
                path = re.sub(r"<[^>]+>", "x", path)
                out.extend((m, path) for m in methods)
    return sorted(set(out))


def main() -> int:
    p = c.Journey(NAME)
    c.time_limit(TIME_LIMIT_S)

    # ── 1. /metrics: no public metrics ──────────────────────────────────────
    for domain in (c.BASE, c.API):
        status, _, body = c.http("GET", f"{domain}/metrics", timeout_s=20)
        if status is not None and 200 <= status < 300:
            p.fail(f"{domain}/metrics not served publicly",
                   f"a refusal {EXPECTED_REFUSALS} (403/401 expected)",
                   f"HTTP {status} — metrics are exposed publicly")
        if "prometheus" in body.lower() or "python_info" in body:
            p.fail(f"{domain}/metrics not served publicly",
                   "no metric in the response body",
                   f"Prometheus body received: {body[:120]!r}")
        if status not in EXPECTED_REFUSALS:
            p.fail(f"{domain}/metrics not served publicly",
                   f"an explicit refusal {EXPECTED_REFUSALS}",
                   f"HTTP {status} — ambiguous answer, the closure must be clear")
        p.ok(f"{domain}/metrics", f"refused (HTTP {status})")

    # ── 2. /internal/authcheck: out of reach from the outside ───────────────
    for domain in (c.BASE, c.API):
        status, _, body = c.http("GET", f"{domain}/internal/authcheck", timeout_s=20)
        if status == 200:
            p.fail(f"{domain}/internal/authcheck out of reach",
                   "nothing served on this path from the outside (404)",
                   f"HTTP 200 — the internal auth check answers from the outside: {body[:120]!r}")
        if body and body.strip() and body.strip() not in ("{}", "null"):
            # Even when refused, the answer must reveal nothing.
            if any(word in body for word in ("username", "is_admin", "session")):
                p.fail(f"{domain}/internal/authcheck out of reach",
                       "an empty answer, without identity data",
                       f"revealing body: {body[:120]!r}")
        if status not in EXPECTED_REFUSALS and status is not None:
            p.fail(f"{domain}/internal/authcheck out of reach",
                   f"a refusal {EXPECTED_REFUSALS}", f"HTTP {status}")
        p.ok(f"{domain}/internal/authcheck", f"HTTP {status} — not reachable from the outside")

    # ── 3. RFC 9116: a security contact, published, current and canonical ───
    # The static file it replaced had silently expired (2026-08-29).
    status, _, body = c.http("GET", f"{c.BASE}/.well-known/security.txt", timeout_s=20)
    text = str(body or "")
    if status != 200 or "Contact:" not in text or "Expires:" not in text:
        p.fail("security.txt published", "HTTP 200 with Contact and Expires",
               f"HTTP {status} — {text[:120]!r}")
    expires = text.split("Expires:", 1)[1].split()[0]
    if expires <= datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"):
        p.fail("security.txt current", "an Expires date in the future", expires)
    p.ok("security.txt", f"contact published, expires {expires}")

    # ── 4. Every protected route refuses an anonymous visitor ──────────────
    # A 200 here is a route whose decorator stack lost its guard. Redirects
    # (302 to /login) and refusals are both fine; what must never come back
    # is the data. POSTs go out WITHOUT a CSRF token: the CSRF check answers
    # first (400), and nothing may succeed.
    routes = protected_routes()
    leaks = []
    for method, path in routes:
        status, _, body = c.http(method, f"{c.BASE}{path}", body={} if method != "GET" else None,
                                 timeout_s=20, follow_redirects=False)
        if status is not None and 200 <= status < 300:
            leaks.append(f"{method} {path} → HTTP {status} {str(body)[:60]!r}")
    if leaks:
        p.fail("protected routes refuse anonymous visitors",
               "no 2xx without a session", "; ".join(leaks[:8]))
    p.ok("protected routes", f"{len(routes)} method/route pairs refused without a session")

    # ── 5. Session cookies: Secure, HttpOnly, SameSite ──────────────────────
    with c.start_playwright() as pw:
        browser, ctx = c.new_context(pw)
        try:
            page = ctx.new_page()
            c.login(page, p)
            cookies = [k for k in ctx.cookies() if k.get("name") in ("session", "dgx_session")
                       or "session" in k.get("name", "").lower()]
            if not cookies:
                p.fail("session cookie flags", "a session cookie after sign-in", "none found")
            weak = [f"{k['name']} (secure={k.get('secure')}, httpOnly={k.get('httpOnly')}, "
                    f"sameSite={k.get('sameSite')})" for k in cookies
                    if not k.get("secure") or not k.get("httpOnly")
                    or k.get("sameSite") not in ("Lax", "Strict")]
            if weak:
                p.fail("session cookie flags", "Secure + HttpOnly + SameSite=Lax|Strict",
                       ", ".join(weak))
            p.ok("session cookie flags", ", ".join(k["name"] for k in cookies)
                 + " — Secure, HttpOnly, SameSite")
        finally:
            browser.close()

    p.done()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001 — a failure must speak, not propagate
        code = c.journey_failed(NAME, e)
    sys.exit(code)
