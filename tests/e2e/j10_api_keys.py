#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Journey « API keys » — the whole loop, from creation to refusal.

From the interface (Settings → API keys): creation with a label; the key is
shown ONCE (contract: never shown again); it is in the list; it produces a
completion through the API with the `auto-model` model (the CALL goes through
the public domain, never an internal port); then it is REVOKED and the API
refuses it from then on (401).

GUARANTEED cleanup (try/finally): the key created by the test is always
revoked, even on failure — the demo account is not polluted.
"""
import sys
import time

import common as c

NAME = "API keys"
TIME_LIMIT_S = 300


def open_account_settings(page):
    """The SHELL's gear opens the account dialog (Settings → API keys) — not the
    playground's."""
    buttons = page.get_by_role("button", name=c.either_label("Réglages", "Settings"))
    for i in range(buttons.count()):
        bt = buttons.nth(i)
        if not bt.is_visible():
            continue
        bt.click()
        page.wait_for_timeout(800)
        if page.locator(".playground-settings-panel").count():
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
            continue
        if c.visible_text(page, "Clés API", "API keys") is not None:
            return
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
    raise c.JourneyFailure(NAME, "Settings → API keys",
                           "the account dialog with the « API keys » section",
                           "no gear opens that dialog")


def read_visible_key(page):
    """The full key shown in the DOM (it is only there at creation time)."""
    return page.evaluate("""() => {
        const out = [];
        for (const e of document.querySelectorAll("*")) {
            if (e.children.length) continue;
            const t = (e.textContent || "").trim();
            if (/^sk-[A-Za-z0-9_.-]{16,}$/.test(t)) out.push(t);
        }
        return out;
    }""")


def call_api(key: str, timeout_s: int = 90):
    """A real HTTP call to the API's DOMAIN (not an internal port)."""
    return c.http("POST", f"{c.API}/v1/chat/completions",
                  body={"model": "auto-model",
                        "messages": [{"role": "user", "content": "say ok"}],
                        "max_tokens": 16},
                  headers={"Authorization": f"Bearer {key}"},
                  timeout_s=timeout_s)


def main() -> int:
    p = c.Journey(NAME)
    c.time_limit(TIME_LIMIT_S)
    alias = f"e2e-validate-{time.strftime('%H%M%S')}"
    key = None
    revoked = False

    with c.start_playwright() as pw:
        b, ctx = c.new_context(pw)
        page = ctx.new_page()
        # The interface's native confirm()/prompt() (revocation): accept.
        page.on("dialog", lambda d: d.accept())
        c.login(page, p)
        try:
            # ── 1. Settings → API keys ──────────────────────────────────────
            open_account_settings(page)
            c.visible_text(page, "Clés API", "API keys").click()
            page.wait_for_timeout(800)
            if c.visible_text(page, "Mes clés API", "My API keys") is None:
                p.fail("« API keys » section", "the « My API keys » panel", "the panel did not open")
            p.ok("Settings → API keys", "« My API keys » panel open")

            # ── 2. Creation with a label ────────────────────────────────────
            field = page.get_by_placeholder(
                c.either_label("Nom (ex: mon-laptop)", "Name (e.g. my-laptop)")).first
            field.fill(alias)
            c.button(page, "Nouvelle clé", "New key").first.click()
            page.wait_for_timeout(2500)

            # ── 3. The key is shown ONCE ────────────────────────────────────
            keys = read_visible_key(page)
            if not keys:
                p.fail("key created and shown",
                       "the key's value visible in its row (shown once, at creation)",
                       f"no « sk-… » key in the DOM — label {alias}")
            key = keys[0]
            p.ok("key created and shown", f"{key[:10]}…{key[-4:]} (full value read from the DOM)")

            # ── 4. It is in the list ────────────────────────────────────────
            if c.visible_text(page, alias, alias) is None:
                p.fail("key listed", f"the label « {alias} » in the list",
                       "label absent from the key list")
            p.ok("key listed", f"label « {alias} » present")

            # ── 5. « Shown only once » contract ─────────────────────────────
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            open_account_settings(page)
            c.visible_text(page, "Clés API", "API keys").click()
            page.wait_for_timeout(1500)
            if key in read_visible_key(page):
                p.fail("key never shown again",
                       "after a reload, the masked value (sk-…xxxx) without the "
                       "full key in the DOM",
                       "the full key is still shown after reload")
            masked = page.evaluate("""() => {
                for (const e of document.querySelectorAll("*")) {
                    if (e.children.length) continue;
                    const t = (e.textContent || "").trim();
                    if (/^sk-.+….+$/.test(t)) return t;
                }
                return null;
            }""")
            if not masked:
                p.fail("masked form shown", "the truncated « sk-… » value in the list",
                       "no masked value found")
            p.ok("shown once", f"after reload: « {masked} » only")

            # ── 6. The key produces a completion through the API DOMAIN ─────
            status, _, body = call_api(key)
            data = c.json_body(status, body)
            content = (((data or {}).get("choices") or [{}])[0]
                       .get("message", {}).get("content") if data else None)
            if status != 200 or not (content or "").strip():
                p.fail("completion through the API",
                       f"200 with an answer (auto-model) from {c.API}",
                       f"HTTP {status} — {body[:200]!r}")
            p.ok("completion through the API domain",
                 f"{c.API}/v1/chat/completions → 200, answer « {content.strip()[:40]}… »")

            # ── 7. Revocation from the interface ────────────────────────────
            row = page.get_by_text(alias, exact=True).first
            clicked = False
            for i in range(1, 9):
                ancestor = row.locator(f"xpath=ancestor::*[{i}]")
                revoke_button = ancestor.get_by_role("button", name=c.either_label("Révoquer", "Revoke"))
                if revoke_button.count():
                    revoke_button.first.click()
                    clicked = True
                    break
            if not clicked:
                p.fail("« Revoke » button", "a Revoke button on the key's row",
                       "no revoke button found for this key")
            page.wait_for_timeout(2500)
            if c.visible_text(page, alias, alias) is not None:
                p.fail("key revoked and removed from the list",
                       f"the label « {alias} » disappears from the list",
                       "still present after revocation")
            p.ok("key revoked", "removed from the list by the interface")
            revoked = True

            # ── 8. The API refuses it from now on ───────────────────────────
            refusal = None
            deadline = time.time() + 60
            while time.time() < deadline:
                status, _, body = call_api(key, timeout_s=30)
                refusal = status
                if status in (401, 403):
                    break
                time.sleep(3)
            if refusal not in (401, 403):
                p.fail("revoked key refused by the API",
                       f"401/403 from {c.API} for a revoked key",
                       f"HTTP {refusal} — the revoked key still works")
            p.ok("revoked key refused", f"HTTP {refusal} for the revoked key")
        finally:
            # ── Cleanup: the test's key NEVER outlives the journey ──────────
            if key and not revoked:
                r = ctx.request.get(f"{c.BASE}/api/csrf")
                token = (r.json() or {}).get("token", "") if r.status == 200 else ""
                rep = ctx.request.post(f"{c.BASE}/keys",
                                       form={"action": "revoke", "key": key},
                                       headers={"X-CSRFToken": token})
                p.warn("forced key cleanup", f"revoked through the /keys route (HTTP {rep.status})")
            elif key and revoked:
                p.ok("cleanup", "no test key left on the account")

        b.close()

    p.done()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001 — a failure must speak, not propagate
        code = c.journey_failed(NAME, e)
    sys.exit(code)
