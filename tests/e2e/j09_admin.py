#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Journey « admin » — the administration console, read for real.

Needs `is_admin`: the flag is SET then CLEARED on the demo account
(`local_users.is_admin`), always in try/finally — a journey never leaves a right
behind. Reason: the Admin page cannot be tested otherwise.

Covers: the six tabs; the ?tab=models deep link; the catalog filter really
narrowing the list; the /users page with the account list. READ ONLY: no
launch, no deletion, no restart — the journey never drives the served model.
"""
import sys

import common as c

NAME = "admin"
TIME_LIMIT_S = 300

# (tab id, FR label, EN label, FR marker, EN marker): the marker is something
# only that tab renders.
TABS = [
    ("overview", "Vue d'ensemble", "Overview", "Mode maintenance", "Maintenance mode"),
    ("models", "Modèles", "Models", "Filtrer le catalogue", "Filter the catalog"),
    ("users", "Utilisateurs", "Users", "Comptes, groupes et droits", "Accounts, groups and rights"),
    ("requests", "Demandes", "Requests", "Budget actuel", "Current budget"),
    ("config", "Configuration", "Configuration", "Annuaire LDAP", "LDAP directory"),
    ("system", "Système", "System", "Publier une annonce", "Publish an announcement"),
]


def set_demo_admin(value: int) -> str:
    c.portal_sql(f"UPDATE local_users SET is_admin={value} WHERE username='demo'")
    return c.portal_sql("SELECT is_admin FROM local_users WHERE username='demo'")


def main() -> int:
    p = c.Journey(NAME)
    c.time_limit(TIME_LIMIT_S)

    # ── 1. Temporary right (ALWAYS cleared below) ───────────────────────────
    state = set_demo_admin(1)
    if "1" not in state:
        p.fail("temporary is_admin", "is_admin=1 for the demo account", f"read back: {state}")
    p.ok("temporary admin right", "local_users.is_admin=1 for the demo account")

    try:
        with c.start_playwright() as pw:
            b, ctx = c.new_context(pw)
            page = ctx.new_page()
            c.login(page, p)

            # ── 2. /admin loads for an admin ────────────────────────────────
            page.goto(f"{c.BASE}/admin", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            text = page.locator("body").inner_text()
            if c.either_label("Accès réservé aux administrateurs",
                              "Administrators only").search(text):
                p.fail("access to /admin", "the console for an admin account",
                       "« Administrators only » — the is_admin flag was not read "
                       "(session or role guard)")
            if c.visible_text(page, "Vue d'ensemble", "Overview") is None:
                p.fail("admin console rendered", "the console's tabs",
                       f"no tab — excerpt: {text[:200]!r}")
            p.ok("/admin rendered", "administration console shown")

            # ── 3. Every tab renders ────────────────────────────────────────
            # Tabs carry data-tab-value: without that filter, the « Users »
            # label ALSO matches the sidebar entry, which leads to the /users
            # page (measured trap — the click left the console).
            for value, fr, en, marker_fr, marker_en in TABS:
                tab = page.locator(f'button[data-tab-value="{value}"]')
                if not tab.count():
                    p.fail(f"« {en} » tab", "a clickable tab", f"no data-tab-value={value} button")
                tab.first.click()
                page.wait_for_timeout(1200)
                if c.visible_text(page, marker_fr, marker_en) is None:
                    p.fail(f"« {en} » tab content",
                           f"something only this tab renders (« {marker_fr} » / « {marker_en} »)",
                           f"content absent — text: {page.locator('body').inner_text()[-200:]!r}")
                p.ok(f"« {en} » tab", f"« {marker_en} » rendered")

            # ── 4. The ?tab=models deep link lands on Models ────────────────
            page.goto(f"{c.BASE}/admin?tab=models", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            if "tab=models" not in page.url:
                p.fail("?tab=models deep link", "the URL keeps tab=models", f"URL {page.url}")
            active = page.locator('button[data-tab-value="models"]').first
            # Astryx 0.7 marks the selected tab with aria-selected AND
            # aria-current="true"; 0.1 used aria-current="page". Accept both:
            # the point is that the tab is the SELECTED one, not the spelling.
            marked = active.count() and (
                active.get_attribute("aria-selected") == "true"
                or active.get_attribute("aria-current") in ("page", "true")
            )
            if not marked:
                p.fail("?tab=models deep link",
                       "the Models tab marked active (aria-selected/aria-current)",
                       "the Models tab is not the active tab")
            if c.visible_text(page, "Filtrer le catalogue", "Filter the catalog") is None:
                p.fail("?tab=models deep link",
                       "the Models tab active (« Filter the catalog » visible)",
                       "the Models tab did not open from the link")
            p.ok("?tab=models deep link", "Models tab open and active")

            # ── 5. The catalog filter narrows the list ──────────────────────
            # Browser session: the admin API is protected, go through the
            # authenticated context.
            r = ctx.request.get(f"{c.BASE}/api/admin")
            data = r.json() if r.status == 200 else None
            names = [m.get("name", "") for m in (data or {}).get("model_cfgs", []) if m.get("name")]
            if len(names) < 2:
                p.fail("model catalog", "at least 2 catalog entries",
                       f"{len(names)} entry(ies) — the filter cannot be checked")
            before = page.get_by_role("button", name=c.either_label("Lancer", "Launch")).count()
            catalog_filter = page.get_by_label(c.either_label("Filtrer le catalogue",
                                                              "Filter the catalog")).first
            catalog_filter.fill(names[0])
            page.wait_for_timeout(900)
            after = page.get_by_role("button", name=c.either_label("Lancer", "Launch")).count()
            if not (0 < after < before):
                p.fail("catalog filter",
                       f"the list narrowed (1 entry for « {names[0]} », out of {before})",
                       f"{after} entry(ies) visible for « {names[0]} » (before the filter: {before})")
            p.ok("catalog filter", f"« {names[0]} »: {before} → {after} entry(ies) shown")

            # ── 6. The /users page lists the accounts ───────────────────────
            page.goto(f"{c.BASE}/users", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            text = page.locator("body").inner_text()
            if "demo" not in text:
                p.fail("account list (/users)",
                       "the « demo » account in the account list",
                       f"list absent or incomplete — excerpt: {text[-300:]!r}")
            if c.visible_text(page, "Utilisateurs", "Users") is None:
                p.fail("/users page", "the « Users » title", "title absent")
            p.ok("account list", "the demo account is in the list")

            b.close()
    finally:
        # ── 7. The right is ALWAYS cleared ──────────────────────────────────
        state = set_demo_admin(0)
        if "0" not in state:
            print(c._c("31", f"✗ cleanup: is_admin was not cleared ({state}) — "
                             f"fix by hand: UPDATE local_users SET is_admin=0 "
                             f"WHERE username='demo'"))
            raise SystemExit(1)
        p.ok("admin right cleared", "local_users.is_admin=0 (cleanup)")

    p.done()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001 — a failure must speak, not propagate
        code = c.journey_failed(NAME, e)
    sys.exit(code)
