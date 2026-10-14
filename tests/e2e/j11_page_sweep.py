#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Journey « page sweep » — every screen of the shell really renders.

For each page: it loads within 15 s, something OF ITS OWN is present (an empty
SPA also answers 200 — a page title or its service state, never a sidebar
label), the DOM does not carry « Something went wrong » / « Application error »,
and no React console error nor unhandled exception is reported.

Two paths have no screen of their own: /memory redirects to the Memory tab of
the settings, /keys has no page any more (removed on purpose — the key list
lives in Settings → API keys, covered by the « API keys » journey). Both are
checked for WHAT they are, with a word on screen.
"""
import re
import sys
import time

import common as c

NAME = "page sweep"
TIME_LIMIT_S = 420

# (path, label, [(FR marker, EN marker, mode)]) — a page changes screen with
# its service's state (form ready, on-demand service, unavailable): ONE of the
# alternatives is enough. Modes:
#   "title" — a page title (never the sidebar);
#   "text"  — visible text of the page's own.
PAGES = [
    ("/", "home",
     [("Disponibilité par capacité", "Availability by capability", "text")]),
    ("/playground", "playground", [("@composer", "@composer", "text")]),
    ("/support", "Support assistant", [("Support", "Support", "title")]),
    ("/image", "image generation",
     [("Génération d'image", "Image generation", "title"),
      ("Aucun modèle image", "No image model", "text")]),
    ("/video", "video generation",
     [("Génération vidéo", "Video generation", "title"),
      ("Aucun modèle vidéo", "No video model", "text")]),
    ("/ocr", "OCR",
     [("Extrait le texte d'une image", "Extracts text from an image", "text"),
      ("Aucun modèle OCR", "No OCR model", "text")]),
    ("/voice", "voice cloning",
     [("Clonage de voix", "Voice cloning", "title"),
      ("Aucun modèle vocal", "No voice model", "text")]),
    ("/ranking", "leaderboard",
     [("Classement", "Leaderboard", "title")]),
    ("/search", "model search",
     [("Chercher un modèle", "Find a model", "title")]),
    ("/request", "model request",
     [("Demander un modèle", "Request a model", "title")]),
    ("/users", "accounts (admin guard)",
     [("Utilisateurs", "Users", "title"),
      ("Accès réservé aux administrateurs", "Administrators only", "text")]),
    ("/admin", "admin console (admin guard)",
     [("Vue d'ensemble", "Overview", "text"),
      ("Accès réservé aux administrateurs", "Administrators only", "text")]),
]

REACT_ERRORS = re.compile(
    r"react|hydrat|uncaught|typeerror|referenceerror|minified error", re.I)

# ── Known defects, reported, NOT fixed ───────────────────────────────────────
# A journey that finds a defect REPORTS it, it does not fix the application
# code. A defect listed here is flagged on every run (⚠) without turning the
# validation red — but ONLY for the exact error measured, on the page measured:
# any other React error stays a failure. Entries: (path regex, error pattern,
# explanation).
# Empty: the /playground #418, long listed here, is FIXED since 2026-10-11 —
# border-beam resolved its « auto » theme during render (dark on the server,
# light in the browser). Any hydration error now fails the sweep: it threw the
# server HTML away and reloaded the page's data.
KNOWN_DEFECTS: list = []


def find_marker(page, alternatives, deadline):
    """Returns (found, detail), until the 15 s deadline passes."""
    while time.time() < deadline:
        for marker_fr, marker_en, mode in alternatives:
            if marker_fr == "@composer":
                try:
                    page.locator('div[role="textbox"]').first.wait_for(
                        state="visible", timeout=800)
                    return True, "composer visible"
                except Exception:
                    continue
            # Substring, FR or EN (a title can be followed by the current
            # model, a state message by a detail).
            pattern = c.label_fragment(marker_fr, marker_en)
            if mode == "title":
                title = page.get_by_role("heading", name=pattern)
                if title.count() and title.first.is_visible():
                    return True, f"title « {marker_en} »"
            else:
                if c.visible_text(page, marker_fr, marker_en, anchored=False) is not None:
                    return True, f"« {marker_en} »"
        page.wait_for_timeout(400)
    return False, " / ".join(f"« {en} »" for _, en, _ in alternatives) + " not found"


def main() -> int:
    p = c.Journey(NAME)
    c.time_limit(TIME_LIMIT_S)

    with c.start_playwright() as pw:
        b, ctx = c.new_context(pw)
        page = ctx.new_page()
        c.login(page, p)
        p.ok("login", "sweep as the demo account")

        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text[:200]) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {str(e)[:200]}"))
        # A page calling an API route that does not exist (404) or that fails
        # (5xx). Refusals (401/403) are answers, not defects. Added 2026-10-14:
        # the home page had called a renamed route (/api/latence) since the
        # English pass, and nothing in the sweep looked at network answers.
        broken: list[str] = []
        page.on("response", lambda r: broken.append(f"{r.status} {r.request.method} {r.url.split('?')[0]}")
                if "/api/" in r.url and (r.status == 404 or r.status >= 500) else None)

        for path, label, alternatives in PAGES:
            errors.clear()
            broken.clear()
            start = time.time()
            try:
                page.goto(f"{c.BASE}{path}", wait_until="domcontentloaded", timeout=15000)
            except Exception as e:
                p.fail(f"page {path} ({label})", "loaded within 15 s",
                       f"navigation failed: {type(e).__name__}: {str(e)[:120]}")

            found, detail = find_marker(page, alternatives, start + 15)
            duration = time.time() - start
            if not found:
                p.fail(f"page {path} ({label})",
                       f"something of the page's own within 15 s ({detail})",
                       f"absent — rendered text: {page.locator('body').inner_text()[:180]!r}")

            # « Real » rendering: neither an error screen nor a near-empty page.
            text = page.locator("body").inner_text()
            if c.DOM_ERROR.search(text):
                p.fail(f"page {path} ({label})",
                       "no error screen (« Something went wrong »…)",
                       f"error text in the DOM: {text[:150]!r}")
            if len(text.strip()) < 150:
                p.fail(f"page {path} ({label})", "a non-empty render",
                       f"{len(text.strip())} character(s) of text")

            # React console errors / unhandled exceptions (known, reported
            # defects are flagged without failing the sweep).
            react = [e for e in errors if REACT_ERRORS.search(e)]
            remaining = []
            for e in react:
                known = False
                for path_re, pattern, explanation in KNOWN_DEFECTS:
                    if re.match(path_re, path) and pattern.search(e):
                        p.warn("known defect (reported, not fixed)", f"{path}: {explanation}")
                        known = True
                        break
                if not known:
                    remaining.append(e)
            if remaining:
                p.fail(f"page {path} ({label})",
                       "no React console error nor unhandled exception",
                       f"{len(remaining)} error(s): {remaining[0]!r}")
            page.wait_for_timeout(500)          # late API calls of the page
            if broken:
                p.fail(f"page {path} ({label})", "every API call of the page answered",
                       f"{len(broken)} broken: {broken[0]}")
            p.ok(f"page {path}", f"{detail}, {duration:.1f} s, "
                                 f"{len(remaining)} blocking React error(s)")

        # ── /memory: no page any more — redirects to the settings tab ───────
        errors.clear()
        page.goto(f"{c.BASE}/memory", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(2500)
        if not page.url.rstrip("/").endswith(c.BASE.rstrip("/")):
            p.fail("page /memory (redirect)",
                   "a redirect to the home page that opens the settings' Memory tab",
                   f"URL {page.url}")
        if c.visible_text(page, "Mémoire", "Memory") is None:
            p.fail("Memory tab opened by /memory",
                   "the settings' « Memory » tab shown",
                   f"tab absent — text: {page.locator('body').inner_text()[:150]!r}")
        p.ok("page /memory", "redirect to the settings' Memory tab")

        # ── /keys: no page any more (documented) — the list lives in Settings
        # The route answers without error; the keys screen is covered by the
        # « API keys » journey (Settings → API keys).
        status, _, _ = c.http("GET", f"{c.BASE}/keys",
                              headers={"Cookie": "; ".join(
                                  f"{ck['name']}={ck['value']}" for ck in ctx.cookies())})
        if status is None or status >= 500:
            p.fail("/keys route (page removed)",
                   "an answer without a server error (204 expected)", f"HTTP {status}")
        p.ok("/keys route", f"HTTP {status} — no page since its removal "
                            f"(key list covered by Settings → API keys)")

        b.close()

    p.done()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001 — a failure must speak, not propagate
        code = c.journey_failed(NAME, e)
    sys.exit(code)
