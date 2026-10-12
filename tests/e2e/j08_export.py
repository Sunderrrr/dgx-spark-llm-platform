#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Journey « export » — leaving with the conversation.

The « Export » button must produce a REAL Markdown file: not empty, titled,
with the turns of speech (the exported model is named).
"""
import sys
import time

import common as c

NAME = "export"
TIME_LIMIT_S = 300


def main() -> int:
    p = c.Journey(NAME)
    c.time_limit(TIME_LIMIT_S)

    with c.start_playwright() as pw:
        b, ctx = c.new_context(pw)
        page = ctx.new_page()
        c.login(page, p)
        c.open_playground(page)

        # ── 1. A conversation to export ─────────────────────────────────────
        question = f"e2e-export-{time.strftime('%H%M%S')} — answer with the word « ready »."
        c.send_message(page, question)
        c.wait_for_stream(page, p, timeout_s=240)
        p.ok("conversation ready", "question + answer saved")

        # ── 2. « Export » produces a download ───────────────────────────────
        try:
            with page.expect_download(timeout=30000) as info:
                # Export lives in the header's « ⋯ » menu since 2026-10-12.
                c.button(page, "Plus d'actions", "More actions").first.click()
                page.get_by_role("menuitem", name=c.either_label(
                    "Exporter en Markdown", "Export as Markdown")).first.click()
            download = info.value
        except Exception:
            p.fail("export download",
                   "a file downloaded after clicking « Export »",
                   "no download within 30 s")
        path = download.path()
        if path is None:
            p.fail("exported file", "a file on disk", "no download path")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        p.ok("exported file", f"{download.suggested_filename!r}, {len(content)} characters")

        # ── 3. It is non-empty, structured Markdown ─────────────────────────
        # The export follows the interface language: both labels are accepted.
        problems = []
        if len(content.strip()) < 80:
            problems.append(f"{len(content.strip())} character(s)")
        if not content.lstrip().startswith("#"):
            problems.append("no Markdown « # … » title")
        if "Modèle :" not in content and "Model:" not in content:
            problems.append("no « Model: » line")
        if not ("Vous :" in content or "You:" in content):
            problems.append("no « You: » turn")
        if not ("Assistant :" in content or "Assistant:" in content):
            problems.append("no « Assistant: » turn")
        if question[:40] not in content:
            problems.append("the question asked is missing from the export")
        if problems:
            p.fail("export Markdown content",
                   "a titled Markdown with the turns of speech and the question",
                   "; ".join(problems) + f" — excerpt {content[:150]!r}")
        p.ok("Markdown content", f"{len(content.splitlines())} lines, titles and turns of speech")

        b.close()

    p.done()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001 — a failure must speak, not propagate
        code = c.journey_failed(NAME, e)
    sys.exit(code)
