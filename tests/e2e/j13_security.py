#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Journey « security » — closed holes, checked from the outside.

From the outside (the same path as any visitor: Cloudflare → Traefik):
  - /metrics is NOT served publicly (an explicit refusal, not a 200);
  - /internal/authcheck (Traefik's forwardAuth) is NOT reachable from outside
    the proxy's network — neither on the web domain nor on the API domain;
  - security.txt (RFC 9116) is published, current and carries a contact.
"""
import datetime
import sys

import common as c

NAME = "security"
TIME_LIMIT_S = 120

EXPECTED_REFUSALS = (401, 403, 404)


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
    if expires <= datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"):
        p.fail("security.txt current", "an Expires date in the future", expires)
    p.ok("security.txt", f"contact published, expires {expires}")

    p.done()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001 — a failure must speak, not propagate
        code = c.journey_failed(NAME, e)
    sys.exit(code)
