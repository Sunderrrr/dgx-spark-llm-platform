#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Journey « skills » — the composer's « / » commands.

Typing « / » opens the skills menu. Selecting a skill inserts its prompt into
the composer (and applies its system prompt).
"""
import sys

import common as c

NAME = "skills"
TIME_LIMIT_S = 120

# The « Summarize » skill's prompt, in both interface languages.
SUMMARIZE_PROMPT = ("Résume ce texte en 3 points clairs et concis : ",
                    "Summarize this text in 3 clear, concise points: ")


def main() -> int:
    p = c.Journey(NAME)
    c.time_limit(TIME_LIMIT_S)

    with c.start_playwright() as pw:
        b, ctx = c.new_context(pw)
        page = ctx.new_page()
        c.login(page, p)
        c.open_playground(page)
        p.ok("playground open", "composer visible")

        # ── 1. « / » opens the skills menu ──────────────────────────────────
        field = c.composer(page)
        field.click()
        page.keyboard.type("/", delay=30)
        menu = page.get_by_text(c.either_label("Tapez / pour appeler une compétence",
                                               "Type / to call a skill"))
        try:
            menu.first.wait_for(state="visible", timeout=10000)
        except Exception:
            p.fail("skills menu opened by « / »",
                   "the « Skills » menu under the composer",
                   "no menu shown after typing « / »")
        # The menu is proven by its contextual help AND by its entries (base
        # skills listed with their « /alias » command).
        if c.visible_text(page, "Résumer", "Summarise") is None and \
                page.get_by_text("/summarize").count() == 0:
            p.fail("menu entries", "the skills listed with their /alias",
                   "menu open but no skill listed")
        p.ok("skills menu", "opened after typing « / », skills listed")

        # ── 2. Selecting a skill puts its prompt in the composer ────────────
        # The skill's name is the card's ACCESSIBLE label: we click its
        # « /summarize » badge (the click bubbles up to the card).
        target = page.get_by_text("/summarize", exact=True).first
        try:
            target.wait_for(state="visible", timeout=10000)
            target.click()
        except Exception:
            p.fail("« Summarize » skill selectable",
                   "a « /summarize » row in the menu",
                   "not found in the skills menu")
        page.wait_for_timeout(600)
        text = c.composer(page).inner_text().strip()
        expected = tuple(a.strip() for a in SUMMARIZE_PROMPT)
        if not any(a in text for a in expected):
            p.fail("skill prompt in the composer",
                   f"the composer prefilled with {expected[1]!r}",
                   f"composer = {text!r}")
        p.ok("prompt inserted", f"« {text[:60]}… »")

        b.close()

    p.done()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001 — a failure must speak, not propagate
        code = c.journey_failed(NAME, e)
    sys.exit(code)
