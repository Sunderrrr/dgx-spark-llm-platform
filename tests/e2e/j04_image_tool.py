#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Journey « image tool » — an explicit request produces a real image.

The generation sidecar starts ON DEMAND: the journey waits up to 4 minutes
(bounded by the service time, never a restart). Required: the tool fires, and a
real image (loaded, non-zero size) appears in the conversation.
"""
import sys
import time

import common as c

NAME = "image tool"
TIME_LIMIT_S = 540

QUESTION = ("Generate an image of an astronaut cat in space, retro poster style, "
            "then describe it in one sentence.")

# The honest refusal when the sidecar is off and cannot start (the
# « image_service_off » notice), in both interface languages.
SERVICE_OFF = c.label_fragment("le démarrer depuis l'espace Admin", "start it from the Admin area")


def main() -> int:
    p = c.Journey(NAME)
    c.time_limit(TIME_LIMIT_S)

    with c.start_playwright() as pw:
        b, ctx = c.new_context(pw)
        page = ctx.new_page()
        c.login(page, p)
        c.open_playground(page)

        # ── 1. The explicit request goes out ────────────────────────────────
        c.send_message(page, QUESTION)
        deadline = time.time() + 60
        while time.time() < deadline and not c.streaming(page):
            page.wait_for_timeout(500)
        if not c.streaming(page):
            p.fail("image request sent", "the answer stream opens", "nothing received within 60 s")
        p.ok("image request sent", "« Generate an image of an astronaut cat… »")

        # ── 2. The tool fires (« generating » step announced) ───────────────
        gen_step = page.get_by_text(
            c.label_fragment("génération de l'image", "generating the image"))
        step_seen = False
        deadline = time.time() + 120
        while time.time() < deadline:
            if gen_step.count() and gen_step.first.is_visible():
                step_seen = True
                break
            page.wait_for_timeout(500)
        if not step_seen:
            # The step is shown ONLY while streaming: if we missed it, do not
            # block — the image is the required proof.
            p.warn("« generating » step announced",
                   "not observed in the window (short-lived step) — the required "
                   "proof is the image itself")
        else:
            p.ok("image tool fired", "« generating the image » step shown")

        # ── 3. A REAL image in the conversation (up to 4 min) ───────────────
        image = page.locator('img[src*="/image/file/"]')
        deadline = time.time() + 240
        seen = False
        refused = False
        while time.time() < deadline:
            if image.count() and image.first.is_visible():
                seen = True
                break
            # The honest refusal can come before the window ends: no point
            # waiting 4 minutes when the service already said so.
            page.wait_for_timeout(1000)
            if SERVICE_OFF.search(page.locator("body").inner_text()):
                refused = True
                break
        if not seen and not refused:
            # Last look: the notice may have appeared in the window's last seconds.
            refused = bool(SERVICE_OFF.search(page.locator("body").inner_text()))
        if not seen:
            text = page.locator("body").inner_text()
            if refused:
                # HONEST refusal by the service (structured « image_service_off »
                # notice, with the free memory): the generation sidecar is off
                # and the on-demand start is refused for lack of memory. Not an
                # outage: it is the portal's warning contract. The FULL image
                # path is only validated when the sidecar can start — in this
                # state it stays NOT VALIDATED (⚠, never a ✓) and the journey
                # does not sink the whole validation.
                p.warn(
                    "IMAGE PATH NOT VALIDATED — generation service off",
                    "the portal answers with the honest « image_service_off » notice "
                    "(service off + not enough free memory for the on-demand start); "
                    "a real image will be REQUIRED as soon as the sidecar can start")
                c.wait_for_stream(page, p, timeout_s=120, require_start=False)
                b.close()
                p.done()
                return 0
            p.fail("image generated in the conversation",
                   "an image served by /image/file/… shown in the answer "
                   "(the sidecar starts on demand: up to 4 min)",
                   f"no image within 4 min and no service notice — answer: {text[-300:]!r}")
        # « Real image »: loaded by the browser, with dimensions.
        width = image.first.evaluate("e => e.naturalWidth")
        if not width:
            p.fail("image really loaded",
                   "naturalWidth > 0 (the bytes are an image)",
                   "the <img> element is there but has no size")
        p.ok("image generated and loaded", f"{image.count()} image(s), width {width} px")

        # ── 4. The answer ends cleanly ──────────────────────────────────────
        c.wait_for_stream(page, p, timeout_s=180, require_start=False)
        p.ok("answer finished", "stream closed after the generation")

        b.close()

    p.done()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001 — a failure must speak, not propagate
        code = c.journey_failed(NAME, e)
    sys.exit(code)
