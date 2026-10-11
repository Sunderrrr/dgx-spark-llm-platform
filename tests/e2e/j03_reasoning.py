#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Journey « reasoning » — the model's visible thinking.

Forces reasoning to « Always » in the playground settings, sends a question
that makes the model think, and requires: the reasoning block appears AND
expands (the thought's text is readable).
"""
import sys
import time

import common as c

NAME = "reasoning"
TIME_LIMIT_S = 360

QUESTION = ("Explain step by step, thinking out loud, why the sky is blue "
            "during the day and red at sunset.")


def main() -> int:
    p = c.Journey(NAME)
    c.time_limit(TIME_LIMIT_S)

    with c.start_playwright() as pw:
        b, ctx = c.new_context(pw)
        page = ctx.new_page()
        c.login(page, p)
        c.open_playground(page)
        p.ok("playground open", "composer visible")

        # ── 1. Settings: reasoning forced to « Always » ─────────────────────
        c.open_playground_settings(page)
        panel = page.locator(".playground-settings-panel")
        panel.wait_for(state="visible", timeout=15000)
        p.ok("settings panel open", "« Playground settings »")

        always = c.visible_text(panel, "Toujours", "Always")
        if always is None:
            p.fail("« Always » option (forced reasoning)",
                   "an « Always » choice in the Reasoning section",
                   "not found in the settings panel")
        always.click()
        page.wait_for_timeout(400)
        c.button(panel, "Fermer", "Close").first.click()
        p.ok("reasoning forced", "« Always » option selected")

        # ── 2. A question that makes the model think ────────────────────────
        c.send_message(page, QUESTION)

        # ── 3. The reasoning block appears (header) ─────────────────────────
        header = page.locator(".raison-entete")
        found = False
        deadline = time.time() + 240
        while time.time() < deadline:
            if header.count() > 0 and header.first.is_visible():
                found = True
                break
            page.wait_for_timeout(500)
        if not found:
            p.fail("reasoning block visible",
                   "the « Reasoning » header above the answer (reasoning forced to « Always »)",
                   f"no .raison-entete within 240 s — thread: "
                   f"{page.locator('body').inner_text()[-300:]!r}")
        p.ok("reasoning block appeared", "« Reasoning » header present")

        # ── 4. The answer ends, then the thought can still be expanded ──────
        c.wait_for_stream(page, p, timeout_s=240)
        p.ok("answer written after the reasoning", "stream ended")

        # The block collapses by itself once the answer is written: if it is
        # not open, a click on the header must expand the thought.
        thought_panel = page.locator(".raison-panneau")
        if not (thought_panel.count() and thought_panel.first.is_visible()):
            header.first.scroll_into_view_if_needed()
            header.first.click()
            page.wait_for_timeout(800)
        if not (thought_panel.count() and thought_panel.first.is_visible()):
            p.fail("reasoning block expanded",
                   "the thought bubble visible after clicking the header",
                   "the .raison-panneau panel does not appear")
        thought = thought_panel.first.inner_text().strip()
        if len(thought) < 20:
            p.fail("reasoning text", "at least 20 characters of thought",
                   f"{len(thought)} character(s) — {thought[:120]!r}")
        p.ok("reasoning expanded", f"{len(thought)} characters of readable thought")

        b.close()

    p.done()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001 — a failure must speak, not propagate
        code = c.journey_failed(NAME, e)
    sys.exit(code)
