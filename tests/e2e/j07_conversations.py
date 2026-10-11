#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Journey « conversations » — create, find, rename, reopen.

A conversation is born from a message; it appears in the history; it carries a
name that can be changed; and reopening it brings the exchange back.
"""
import sys
import time

import common as c

NAME = "conversations"
TIME_LIMIT_S = 360


def main() -> int:
    p = c.Journey(NAME)
    c.time_limit(TIME_LIMIT_S)
    marker = f"e2e-conversations-{time.strftime('%H%M%S')}"
    question = f"{marker} — just answer with the word « received »."

    with c.start_playwright() as pw:
        b, ctx = c.new_context(pw)
        page = ctx.new_page()
        c.login(page, p)
        c.open_playground(page)

        # ── 1. A conversation is born from a message ────────────────────────
        c.button(page, "Nouvelle conversation", "New conversation").first.click()
        page.wait_for_timeout(500)
        c.send_message(page, question)
        c.wait_for_stream(page, p, timeout_s=240)
        p.ok("conversation created", f"question « {marker} » sent and answered")

        # ── 2. It is listed in the history ──────────────────────────────────
        c.button(page, "Historique", "History").first.click()
        dialog = page.get_by_role("dialog")
        dialog.last.wait_for(state="visible", timeout=15000)
        # Filter on the marker: the demo account's list is long, and the
        # filter lets the next steps aim at the right row.
        search = dialog.last.get_by_placeholder(
            c.either_label("Rechercher une conversation", "Search conversations")).first
        search.fill(marker)
        page.wait_for_timeout(800)
        dialog_text = dialog.last.inner_text()
        if marker not in dialog_text:
            p.fail("conversation listed in the history",
                   f"a row containing « {marker} »",
                   f"absent from the history dialog — content: {dialog_text[:200]!r}")
        p.ok("listed in the history", "found by the history search")

        # ── 3. It can be renamed ────────────────────────────────────────────
        new_name = f"e2e validation {time.strftime('%H%M%S')}"
        c.button(dialog.last, "Renommer", "Rename").first.click()
        page.wait_for_timeout(500)
        name_field = dialog.last.get_by_label(c.either_label("Renommer", "Rename")).first
        try:
            name_field.wait_for(state="visible", timeout=10000)
        except Exception:
            p.fail("rename field", "a « Rename » field in the row",
                   "no input appeared after clicking « Rename »")
        name_field.fill(new_name)
        c.button(dialog.last, "Valider", "Confirm").first.click()
        page.wait_for_timeout(1200)
        # The search filter still holds the old marker: search the NEW name now
        # (otherwise the list stays empty and the check would think the rename
        # failed).
        search.fill(new_name)
        page.wait_for_timeout(800)
        dialog_text = dialog.last.inner_text()
        if new_name not in dialog_text:
            p.fail("conversation renamed",
                   f"the new name « {new_name} » in the history",
                   f"still « {dialog_text[:200]!r} »")
        p.ok("conversation renamed", f"« {new_name} »")

        # ── 4. It reopens with its content ──────────────────────────────────
        dialog.last.get_by_text(new_name).first.click()
        page.wait_for_timeout(1500)
        page_text = page.locator("body").inner_text()
        if marker not in page_text:
            p.fail("conversation reopened",
                   f"the thread reopened with the question « {marker} »",
                   f"question absent from the page — excerpt: {page_text[-200:]!r}")
        p.ok("conversation reopened", "the thread brings the question back")

        b.close()

    p.done()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001 — a failure must speak, not propagate
        code = c.journey_failed(NAME, e)
    sys.exit(code)
