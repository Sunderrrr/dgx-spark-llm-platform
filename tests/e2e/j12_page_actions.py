#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Journey « page actions » — one real gesture on each screen.

Where « the page loads » proves nothing (an empty SPA answers 200), this
journey does WHAT A USER DOES:
  /search   a Hugging Face search returns cards;
  /ranking  a leaderboard shows (the ranked accounts are in it);
  /request  the request form submits — then the test row is DELETED
            (try/finally) so the admin queue is not polluted;
  /memory   a preference is read and written (original state restored);
  /support  a message to the assistant gets an answer;
  /image /video /ocr /voice: the form is usable and the service state
            is announced honestly (available / on demand) — no heavy
            generation here (covered by « image tool »).
"""
import re
import sys
import time

import common as c

NAME = "page actions"
TIME_LIMIT_S = 600

MEDIA = [
    ("/image", "Génération d'image", "Image generation"),
    ("/video", "Génération vidéo", "Video generation"),
    ("/ocr", "OCR", "OCR"),
    ("/voice", "Clonage de voix", "Voice cloning"),
]

# How a media page announces its service, in both interface languages.
SERVICE_STATES = [
    ("Demander un modèle", "Request a model"),
    ("Aucun modèle", "No model"),
    ("Générer", "Generate"),
    ("indisponible", "unavailable"),
    ("à la demande", "on demand"),
]

SUPPORT_QUESTION = "Answer only with the word « hello »."


def main() -> int:
    p = c.Journey(NAME)
    c.time_limit(TIME_LIMIT_S)

    with c.start_playwright() as pw:
        b, ctx = c.new_context(pw)
        page = ctx.new_page()
        c.login(page, p)

        # ── 1. /search: a Hugging Face search returns cards ─────────────────
        page.goto(f"{c.BASE}/search", wait_until="domcontentloaded")
        field = page.get_by_placeholder(
            c.either_label("Nom de modèle, ex: Qwen, Ornith, Mistral...",
                           "Model name, e.g. Qwen, Ornith, Mistral...")).first
        field.wait_for(state="visible", timeout=20000)
        field.fill("Qwen")
        c.button(page, "Recherche", "Search").first.click()
        card_seen = False
        deadline = time.time() + 45
        while time.time() < deadline:
            text = page.locator("body").inner_text()
            if "Qwen/" in text or re.search(r"Qwen[^\s]{0,20}/", text):
                card_seen = True
                break
            if c.label_fragment("Hugging Face ne répond pas",
                                "Hugging Face is not responding").search(text):
                p.fail("Hugging Face search (/search)",
                       "result cards for « Qwen »",
                       "Hugging Face is not responding (upstream outage) — the page says so")
            page.wait_for_timeout(700)
        if not card_seen:
            p.fail("Hugging Face search (/search)",
                   "result cards (repository identifiers shown)",
                   f"no result within 45 s — text: {page.locator('body').inner_text()[:200]!r}")
        p.ok("Hugging Face search", "result cards shown for « Qwen »")

        # ── 2. /ranking: a leaderboard shows ────────────────────────────────
        page.goto(f"{c.BASE}/ranking", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        r = ctx.request.get(f"{c.BASE}/api/ranking?period=day&metric=total")
        rows = (r.json() or {}).get("rows", []) if r.status == 200 else []
        text = page.locator("body").inner_text()
        # An account may appear anonymously (username null): the first NAMED
        # row is the one to find on screen.
        named = [x for x in rows if x.get("username")]
        if named and named[0]["username"] in text:
            p.ok("leaderboard shown", f"{len(rows)} ranked account(s), "
                                      f"first named « {named[0]['username']} » visible")
        elif not rows:
            # No consumption over the period: an empty leaderboard IS a real
            # render, but at least the table's structure is required.
            if c.visible_text(page, "Rang", "Rank") is None:
                p.fail("leaderboard shown", "the leaderboard's structure (« Rank »)",
                       "neither ranked accounts nor a table")
            p.warn("leaderboard shown", "period without consumption: table structure checked")
        else:
            p.fail("leaderboard shown",
                   f"the first account « {rows[0].get('username')} » in the table",
                   f"table not in line with /api/ranking — text: {text[:200]!r}")

        # The heatmap and the records under it (2026-10-14).
        # A cell is a real moment: 7 dates × 24 hours over a week, one cell
        # per day over 30 days.
        week = ctx.request.get(f"{c.BASE}/api/ranking/activity?period=week&scope=all")
        month = ctx.request.get(f"{c.BASE}/api/ranking/activity?period=month&scope=all")
        rows = (week.json() or {}).get("rows", []) if week.status == 200 else []
        days = (month.json() or {}).get("days", []) if month.status == 200 else []
        if len(rows) == 7 and all(len(r["cells"]) == 24 for r in rows) and len(days) == 30:
            p.ok("activity heatmap", f"7 dates × 24 h, 30 days — {month.json().get('total_requests')} "
                                     "request(s) over 30 days")
        else:
            p.fail("activity heatmap", "7 dates × 24 hours and 30 days from /api/ranking/activity",
                   f"HTTP {week.status}/{month.status}, {len(rows)} row(s), {len(days)} day(s)")
        if c.visible_text(page, "Quand la plateforme travaille", "When the platform works") is None:
            p.fail("activity heatmap", "the heatmap card on /ranking", "not rendered")
        rec = ctx.request.get(f"{c.BASE}/api/ranking/records")
        if rec.status == 200 and {"best_days", "longest_streaks", "firsts"} <= set(rec.json() or {}):
            p.ok("records", f"{len(rec.json()['best_days'])} biggest day(s), {len(rec.json()['firsts'])} first(s)")
        else:
            p.fail("records", "best days, streaks and firsts from /api/ranking/records", f"HTTP {rec.status}")

        # ── 3. /request: the form submits (then cleanup) ────────────────────
        model_id = f"e2e-validate/test-{time.strftime('%H%M%S')}"
        page.goto(f"{c.BASE}/request", wait_until="domcontentloaded")
        field = page.get_by_label(
            c.either_label("Identifiant HuggingFace *", "HuggingFace identifier *")).first
        field.wait_for(state="visible", timeout=20000)
        field.fill(model_id)
        try:
            c.button(page, "Envoyer la demande", "Send request").first.click()
            deadline = time.time() + 20
            sent = False
            while time.time() < deadline:
                text = page.locator("body").inner_text()
                if c.label_fragment("Demande envoyée", "Request sent").search(text):
                    sent = True
                    break
                page.wait_for_timeout(500)
            if not sent:
                p.fail("request form (/request)",
                       "« Request sent! » after submitting",
                       f"no success message — text: {text[:200]!r}")
            p.ok("model request submitted", f"« {model_id} » accepted")
        finally:
            # The test row does NOT stay in the admin queue.
            cleanup = c.portal_sql(f"DELETE FROM model_requests WHERE model_id='{model_id}'")
            p.ok("admin queue not polluted", f"test request row deleted ({cleanup})")

        # ── 4. /memory: a preference is read and written ────────────────────
        page.goto(f"{c.BASE}/memory", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        r = ctx.request.get(f"{c.BASE}/api/memory")
        before = (r.json() or {}).get("enabled") if r.status == 200 else None
        if before is None:
            p.fail("memory preference read (/api/memory)",
                   "the preference's {enabled} state", f"HTTP {r.status} — cannot read it")
        p.ok("memory preference read", f"enabled={bool(before)}")
        target = not bool(before)
        # The label is not a clickable <label>: the switch itself (role="switch")
        # toggles the preference.
        # Three switches since 2026-10-12 (search chats / generate memory /
        # sensitive topics): « enabled » is the one named after generation.
        switch = page.get_by_role("switch", name=c.either_label(
            "Générer la mémoire à partir des conversations",
            "Generate memory from chat history")).first
        try:
            switch.wait_for(state="visible", timeout=15000)
            switch.click()
        except Exception:
            p.fail("« Enable memory » switch",
                   "the preference switch visible (Memory tab)",
                   "not found on the /memory page (redirected)")
        page.wait_for_timeout(1500)
        r = ctx.request.get(f"{c.BASE}/api/memory")
        after = (r.json() or {}).get("enabled") if r.status == 200 else None
        if bool(after) != target:
            p.fail("memory preference written",
                   f"enabled={target} after the click (written by the page)",
                   f"enabled={bool(after)} — the click was not taken into account")
        p.ok("memory preference written", f"enabled={bool(before)} → {bool(after)}")
        # Back to the original state: a journey leaves no setting behind.
        switch.click()
        page.wait_for_timeout(1200)
        r = ctx.request.get(f"{c.BASE}/api/memory")
        restored = (r.json() or {}).get("enabled") if r.status == 200 else None
        if bool(restored) != bool(before):
            p.fail("memory preference restored",
                   f"enabled={bool(before)} (original state)", f"enabled={bool(restored)}")
        p.ok("preference restored", f"enabled={bool(restored)}")

        # ── 5. /support: a message to the assistant gets an answer ──────────
        page.goto(f"{c.BASE}/support", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        field = c.composer(page)
        field.wait_for(state="visible", timeout=20000)
        field.click()
        page.keyboard.type(SUPPORT_QUESTION, delay=5)
        page.keyboard.press("Enter")
        # The question already contains « hello »: the ANSWER is the word's
        # second occurrence (a « No response » text instead = failure).
        answered = False
        deadline = time.time() + 240
        while time.time() < deadline:
            text = page.locator("body").inner_text()
            if c.label_fragment("Pas de réponse", "No response").search(text):
                break  # the Support explicitly returned « no response »
            if text.lower().count("hello") >= 2:
                answered = True
                break
            page.wait_for_timeout(1000)
        if not answered:
            p.fail("Support assistant answer",
                   f"an answer to « {SUPPORT_QUESTION} » (the word appears a second "
                   "time, in the answer)",
                   f"no answer within 240 s — text: "
                   f"{page.locator('body').inner_text()[-250:]!r}")
        p.ok("Support assistant answers", "answer received")

        # ── 6. Media pages: usable form + announced state ───────────────────
        for path, _title_fr, _title_en in MEDIA:
            page.goto(f"{c.BASE}{path}", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            text = page.locator("body").inner_text()
            state = next((en for fr, en in SERVICE_STATES
                          if c.label_fragment(fr, en).search(text)), None)
            if state is None:
                p.fail(f"service state announced ({path})",
                       "an honest state: available (« Generate ») or on demand "
                       "(« Request a model » / « No model »)",
                       f"no state announced — text: {text[:200]!r}")
            # The form responds: if there is an input, type into it.
            fields = page.get_by_role("textbox")
            usable = 0
            for i in range(min(fields.count(), 4)):
                f = fields.nth(i)
                if f.is_visible():
                    try:
                        f.click()
                        page.keyboard.type("e2e", delay=5)
                        usable += 1
                        break
                    except Exception:
                        pass
            if fields.count() and not usable:
                p.fail(f"usable form ({path})", "the input accepts text", "no interactive input")
            p.ok(f"page {path}", f"state announced « {state} », form responding")

        b.close()

    p.done()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001 — a failure must speak, not propagate
        code = c.journey_failed(NAME, e)
    sys.exit(code)
