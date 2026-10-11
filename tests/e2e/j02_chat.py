#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Journey « chat » — the core function: talking to the served model.

Sends a message in the playground and requires a streamed answer, the model's
name above the answer, and the message footer with the tokens and the TTFT
(measured by the portal).
"""
import re
import sys
import time

import common as c

NAME = "chat"
TIME_LIMIT_S = 360

QUESTION = ("Explain in two sentences only what a context cache is "
            "for a language model.")


def main() -> int:
    p = c.Journey(NAME)
    c.time_limit(TIME_LIMIT_S)

    with c.start_playwright() as pw:
        b, ctx = c.new_context(pw)
        page = ctx.new_page()
        c.login(page, p)
        c.open_playground(page)
        p.ok("playground open", "composer visible")

        # The model name announced by the selector: it is what we want to find
        # again ABOVE the answer (« who speaks »).
        selector = page.get_by_role("combobox").first
        model = ""
        # The selector renders its placeholder before the model list arrives:
        # reading it at once captured « No active model » (run 20261011-133040)
        # and failed the name check further down. Wait for a real name.
        placeholders = ("No active model", "Aucun modèle actif")
        try:
            selector.wait_for(state="visible", timeout=15000)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                model = selector.inner_text().strip().split("\n")[0]
                if model and not model.startswith(placeholders):
                    break
                page.wait_for_timeout(250)
        except Exception:
            pass
        if model.startswith(placeholders):
            p.fail("model announced in the selector", "the served model's name",
                   f"still « {model} » after 30 s — the portal sees no running model")
        if not model:
            model = "model"
        count_before = page.locator("body").inner_text().count(model) if model else 0
        p.ok("model announced in the selector", f"« {model} »")

        c.send_message(page, QUESTION)
        c.wait_for_stream(page, p, timeout_s=300)
        p.ok("answer streamed", "« Stop » button appeared then disappeared")

        page_text = page.locator("body").inner_text()
        # The answer's text: the whole thread minus the question.
        answer = page_text.replace(QUESTION, "", 1)
        if len(answer.strip()) < 40:
            p.fail("answer content", "at least 40 characters of answer",
                   f"{len(answer.strip())} character(s) — excerpt {answer.strip()[:120]!r}")
        p.ok("answer content", f"{len(answer.strip())} characters")

        # Model name: it must appear once more than before sending (the
        # selector + the name above the answer).
        count_after = page_text.count(model)
        if model and count_after <= count_before:
            p.fail("model name above the answer",
                   f"« {model} » repeated above the answer ({count_before} → more)",
                   f"{count_before} occurrence(s) before, {count_after} after")
        p.ok("model name shown", f"« {model} » above the answer")

        # Message footer: tokens + TTFT (both, or the footer is missing).
        footers = page.get_by_text(re.compile(r"TTFT", re.I))
        if footers.count() == 0:
            p.fail("message footer (tokens / TTFT)",
                   "a « N tokens · … · TTFT Xs » footer under the answer",
                   f"no TTFT text in the page — thread excerpt: {page_text[-300:]!r}")
        footer = footers.first.inner_text()
        if not re.search(r"token", footer, re.I):
            p.fail("message footer (tokens / TTFT)",
                   "the token count in the same footer", f"footer found: {footer!r}")
        p.ok("message footer", f"« {footer.strip()} »")

        b.close()

    p.done()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001 — a failure must speak, not propagate
        code = c.journey_failed(NAME, e)
    sys.exit(code)
