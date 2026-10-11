#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Journey « login » — the service's front door, end to end.

Covers: the server-side redirect of a visitor without a session (no empty page
served); the login page and the demo account's credentials; the instance's
identity in the header; an invalid session sent back to the login page (no
blank screen, no 500); logout really clearing the session; and the PRESENCE of
the SSO path (the button + the OIDC redirect to the provider — going through
Authentik cannot be automated: 2FA/LDAP, this is the testable contract).
"""
import re
import sys
from urllib.parse import parse_qs, unquote, urlparse

import common as c
from e2e_config import DOMAIN, SSO_DOMAIN

NAME = "login"
TIME_LIMIT_S = 180


def main() -> int:
    p = c.Journey(NAME)
    c.time_limit(TIME_LIMIT_S)

    # ── 1. The public configuration names the instance and announces SSO ────
    status, _, body = c.http("GET", f"{c.BASE}/api/config")
    cfg = c.json_body(status, body)
    if status != 200 or not cfg:
        p.fail("public configuration (/api/config)",
               "200 with {branding, oidc_enabled}", f"HTTP {status} — {body[:120]!r}")
    instance_name = (cfg.get("branding") or {}).get("name") or ""
    oidc = bool(cfg.get("oidc_enabled"))
    p.ok("public configuration", f"instance « {instance_name} », oidc_enabled={oidc}")

    # ── 1a. An administered instance offers NO owner setup, and refuses it ──
    if cfg.get("setup_required") is not False:
        p.fail("owner setup closed", "setup_required=false (administered instance)",
               f"setup_required={cfg.get('setup_required')!r}")
    status, _, body = c.http("POST", f"{c.BASE}/api/setup",
                             body={"token": "e2e", "username": "e2e", "password": "x"})
    if status not in (400, 403, 409):
        p.fail("owner setup refused", "4xx (instance already administered)",
               f"HTTP {status} — {body[:120]!r}")
    p.ok("owner setup", f"closed (setup_required=false, POST /api/setup → {status})")

    # ── 1b. Logged out, a page is refused by the SERVER: a 307 to /login
    #        before any markup (it used to serve the empty home page for ~1 s
    #        until a data fetch came back 401) ──────────────────────────────
    status, headers, body = c.http("GET", f"{c.BASE}/", follow_redirects=False)
    location = headers.get("Location") or headers.get("location") or ""
    if status not in (302, 303, 307, 308) or not location.rstrip("/").endswith("/login"):
        p.fail("server redirect without a session",
               "GET / → 30x to /login, no page",
               f"HTTP {status}, Location {location[:120]!r}")
    status, _, body = c.http("GET", f"{c.BASE}/api/session")
    if status != 401:
        p.fail("session check without a cookie (/api/session)", "401",
               f"HTTP {status} — {body[:120]!r}")
    p.ok("server redirect", "GET / without a session → /login, /api/session → 401")

    with c.start_playwright() as pw:
        # ── 2. The login page loads and accepts the demo account ────────────
        b, ctx = c.new_context(pw)
        page = ctx.new_page()
        page.goto(f"{c.BASE}/login", wait_until="domcontentloaded")
        page.locator('input[type="text"]').first.wait_for(state="visible", timeout=20000)
        if not page.locator('input[type="password"]').first.is_visible():
            p.fail("login form", "a visible password field", "no password field")
        p.ok("login page", "username + password shown")

        c.login(page, p)
        if "/login" in page.url:
            p.fail("sign-in", "a redirect to the home page", f"URL {page.url}")
        p.ok("demo account signed in", f"landed on {page.url}")
        session_status = ctx.request.get(f"{c.BASE}/api/session").status
        if session_status != 204:
            p.fail("session check when signed in (/api/session)", "204",
                   f"HTTP {session_status}")
        p.ok("session check", "/api/session → 204 once signed in")

        # ── 3. The header carries the instance's name ───────────────────────
        page.wait_for_timeout(1200)
        page_text = page.locator("body").inner_text()
        if instance_name and instance_name not in page_text:
            p.fail("instance identity in the header",
                   f"the name « {instance_name} » rendered by the shell",
                   f"absent from the page text (excerpt: {page_text[:120]!r})")
        p.ok("instance identity", f"« {instance_name} » visible in the header")

        # ── 4. Logout: the session is really cleared ────────────────────────
        c.button(page, "Déconnexion", "Log out").first.click()
        page.wait_for_url(lambda u: "/login" in u, timeout=20000)
        p.ok("logout", "back on the login page")
        page.goto(f"{c.BASE}/", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        if "/login" not in page.url:
            p.fail("session cleared after logout",
                   "a protected page sends to /login", f"URL {page.url}")
        p.ok("session cleared", "a protected page sends to the login page")
        b.close()

        # ── 5. Invalid session: back to the login page, no blank screen ─────
        b2, ctx2 = c.new_context(pw)
        ctx2.add_cookies([{"name": "session", "value": "invalid.token.e2e",
                           "domain": DOMAIN, "path": "/"}])
        page2 = ctx2.new_page()
        page2.goto(f"{c.BASE}/playground", wait_until="domcontentloaded")
        page2.wait_for_timeout(2500)
        if "/login" not in page2.url:
            p.fail("invalid session sent to the login page",
                   "landing on /login", f"URL {page2.url}")
        if not page2.locator('input[type="password"]').first.is_visible():
            p.fail("invalid session: usable screen",
                   "the login form shown again",
                   f"form absent (text: {page2.locator('body').inner_text()[:120]!r})")
        p.ok("invalid session", "back to the login page, form intact")
        b2.close()

        # ── 6. SSO: the path is offered (the button) ────────────────────────
        if not oidc:
            p.warn("SSO", "oidc_enabled=false: no SSO path offered, check skipped")
        else:
            b3, ctx3 = c.new_context(pw)
            page3 = ctx3.new_page()
            page3.goto(f"{c.BASE}/login", wait_until="domcontentloaded")
            sso_button = page3.get_by_role("button", name=re.compile(r"SSO", re.I)).first
            # The button appears once /api/config answers — on a cold page that
            # is one fetch later than the form. Two attempts, 30 s apart: a
            # single 15 s wait flagged a healthy page as broken twice.
            seen = False
            for attempt in range(2):
                try:
                    sso_button.wait_for(state="visible", timeout=30000)
                    seen = True
                    break
                except Exception:
                    if attempt == 0:
                        page3.goto(f"{c.BASE}/login", wait_until="networkidle")
                        sso_button = page3.locator("text=SSO").first
            if not seen:
                p.fail("SSO button offered", "a visible « Sign in with SSO » button",
                       "no SSO button on the login page")
            else:
                p.ok("SSO button", "offered on the login page")
            b3.close()

    # ── 7. SSO: /login/sso redirects to the OIDC provider ───────────────────
    if oidc:
        status, headers, body = c.http("GET", f"{c.BASE}/login/sso", follow_redirects=False)
        location = headers.get("Location") or headers.get("location") or ""
        if status not in (301, 302, 303, 307, 308):
            p.fail("SSO redirect (/login/sso)", "a 30x status",
                   f"HTTP {status} — {body[:120]!r}")
        # No configured IdP domain needed: the provider is set in Admin → Config.
        # A real OIDC authorize redirect leaves for ANOTHER host over https and
        # carries our client_id and a redirect_uri that comes back here.
        target = urlparse(location)
        params = parse_qs(target.query)
        back = unquote((params.get("redirect_uri") or [""])[0])
        if (target.scheme != "https" or not target.hostname or target.hostname == DOMAIN
                or not params.get("client_id") or DOMAIN not in back
                or (SSO_DOMAIN and target.hostname != SSO_DOMAIN)):
            p.fail("OIDC provider",
                   "https Location to the IdP with client_id and a redirect_uri back to "
                   + DOMAIN + (f" (IdP {SSO_DOMAIN})" if SSO_DOMAIN else ""),
                   f"Location {location[:160]!r}")
        p.ok("SSO redirect", f"30x to {location.split('/')[2]}")

    p.done()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001 — a failure must speak, not propagate
        code = c.journey_failed(NAME, e)
    sys.exit(code)
