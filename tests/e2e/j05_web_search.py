#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Journey « web search » — search, read, cite.

A search request must show its progress (« searching for « … » », « reading N
page(s) ») DURING the stream, and the answer must cite sources (web addresses
appear in the answer).
"""
import re
import sys
import time

import common as c

NAME = "web search"
TIME_LIMIT_S = 480

QUESTION = ("Search the web for recent progress in Mars exploration, then "
            "summarize in two sentences and cite your sources.")


def main() -> int:
    p = c.Journey(NAME)
    c.time_limit(TIME_LIMIT_S)

    with c.start_playwright() as pw:
        b, ctx = c.new_context(pw)
        page = ctx.new_page()
        c.login(page, p)
        c.open_playground(page)

        # External links BEFORE sending: comparing after the answer shows the
        # CITED sources, not a menu link.
        links_before = page.locator('a[href^="http"]').count()

        # ── 1. The search request goes out ──────────────────────────────────
        c.send_message(page, QUESTION)
        deadline = time.time() + 60
        while time.time() < deadline and not c.streaming(page):
            page.wait_for_timeout(500)
        if not c.streaming(page):
            p.fail("search request sent", "the answer stream opens", "nothing received within 60 s")
        p.ok("search request sent", "« Search the web for… »")

        # ── 2. Progress is shown during the stream ──────────────────────────
        steps = page.get_by_text(
            re.compile(r"(recherche «|searching for|lecture de \d|reading \d|"
                       r"résultat\(s\)|result\(s\))"))
        step_seen = None
        deadline = time.time() + 180
        while time.time() < deadline:
            for i in range(steps.count()):
                if steps.nth(i).is_visible():
                    step_seen = steps.nth(i).inner_text().strip()
                    break
            if step_seen:
                break
            page.wait_for_timeout(500)
        if not step_seen:
            p.fail("search progress shown",
                   "a « searching for « … » » / « reading N page(s) » step visible "
                   "during the stream",
                   f"no step visible within 180 s — thread: "
                   f"{page.locator('body').inner_text()[-300:]!r}")
        p.ok("progress shown", f"« {step_seen[:80]} »")

        # ── 3. The answer cites sources ─────────────────────────────────────
        c.wait_for_stream(page, p, timeout_s=240, require_start=False)
        p.ok("answer received", "stream ended")

        links_after = page.locator('a[href^="http"]').count()
        text = page.locator("body").inner_text()
        answer = text[text.find(QUESTION) + len(QUESTION):] if QUESTION in text else text
        if links_after <= links_before and "http" not in answer:
            p.fail("answer citing sources",
                   "web addresses (links) in the answer",
                   f"{links_after} external link(s) against {links_before} before "
                   f"sending, and no address in the text — answer: "
                   f"{answer.strip()[-300:]!r}")
        p.ok("sources cited",
             f"{links_after - links_before} new external link(s) in the answer")

        b.close()

    p.done()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001 — a failure must speak, not propagate
        code = c.journey_failed(NAME, e)
    sys.exit(code)
