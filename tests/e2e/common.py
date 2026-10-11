# -*- coding: utf-8 -*-
"""Shared by the e2e journeys (tests/e2e/) — the base, never a journey itself.

Each journey (j*.py) is a standalone script run by `scripts/validate.sh` with
{PYTEST_BIN} (Playwright is installed there). This module provides:

- the configuration (demo account, base URLs, password read from a file outside
  the repository — never printed, never logged);
- readable journey output: « ✓ step » / « ✗ journey « X » failed at step « Y »:
  expected …, got … »;
- a HARD time limit per journey (SIGALRM): no journey can run forever, even
  stuck inside Playwright;
- browser helpers (login, composer, waiting for a stream to end) and HTTP
  helpers (domain API calls, security checks).

Golden rules: the demo account ONLY, no destructive action, no model restart —
a journey sends requests, it never drives the infrastructure.

UI labels are matched in BOTH languages (`fr`, `en` arguments): the French
string is the interface's i18n key, the English one its translation.
"""
from __future__ import annotations

import json as _json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request

from e2e_config import API as _API
from e2e_config import DEMO_PASS, WEB

# ── Configuration ────────────────────────────────────────────────────────────
BASE = os.environ.get("E2E_BASE", WEB)
API = os.environ.get("E2E_API", _API)
USER = os.environ.get("E2E_USER", "demo")
PASSWORD_FILE = os.environ.get("E2E_PW_FILE", DEMO_PASS)
# A real User-Agent: Cloudflare refuses Python's default one (error 1010).
UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

COLORS = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLORS else text


# ── Journey failures ─────────────────────────────────────────────────────────
class JourneyFailure(Exception):
    """An expected outcome did not happen. The message says where and what."""

    def __init__(self, journey: str, step: str, expected: str, got: str):
        self.journey, self.step = journey, step
        self.expected, self.got = expected, got
        super().__init__(str(self))

    def __str__(self) -> str:
        return (f"journey « {self.journey} » failed at step « {self.step} »: "
                f"expected {self.expected}, got {self.got}")


class TimeLimitExceeded(Exception):
    pass


def _alarm(_sig, _frm):  # pragma: no cover — called by SIGALRM
    raise TimeLimitExceeded("time limit exceeded")


class Journey:
    """Clock and step output of one journey."""

    def __init__(self, name: str):
        self.name = name
        self.start = time.time()
        self._last = self.start
        self.steps = 0
        print(f"▶ journey « {name} »")

    def ok(self, step: str, detail: str = "") -> None:
        self.steps += 1
        now = time.time()
        duration = now - self._last
        self._last = now
        suffix = f" — {detail}" if detail else ""
        print(f"  {_c('32', '✓')} {step}{suffix} ({duration:.1f} s)")

    def warn(self, step: str, detail: str = "") -> None:
        suffix = f" — {detail}" if detail else ""
        print(f"  {_c('33', '⚠')} {step}{suffix}")

    def fail(self, step: str, expected: str, got: str) -> None:
        raise JourneyFailure(self.name, step, expected, got)

    def done(self) -> None:
        print(f"{_c('32', '✓')} journey « {self.name} » — OK "
              f"({self.steps} steps, {time.time() - self.start:.1f} s)")


def time_limit(seconds: int):
    """Sets the journey's hard time limit (SIGALRM)."""
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(seconds)


def password() -> str:
    """The demo account's password. Never printed."""
    try:
        with open(PASSWORD_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError as e:
        print(f"✗ cannot read {PASSWORD_FILE}: {e}", file=sys.stderr)
        raise SystemExit(2)


def check_demo(username: str) -> None:
    """Guard: a journey only ever speaks as the demo account."""
    if username != USER:
        raise SystemExit(f"refused: this journey only uses the « {USER} » account")


# ── Bilingual labels ─────────────────────────────────────────────────────────
# The demo account displays ENGLISH (an account preference stored on the
# server): selectors therefore accept both labels.
def either_label(fr: str, en: str) -> "re.Pattern":
    """« Exact label » pattern, in both languages."""
    return re.compile(rf"^(?:{re.escape(fr)}|{re.escape(en)})$", re.I)


def label_fragment(fr: str, en: str) -> "re.Pattern":
    """« Label contained in the text » pattern, in both languages."""
    return re.compile(rf"(?:{re.escape(fr)}|{re.escape(en)})", re.I)


def button(page, fr: str, en: str):
    """The button with this label (accessible name), FR or EN."""
    return page.get_by_role("button", name=either_label(fr, en))


def visible_text(parent, fr: str, en: str, anchored: bool = True):
    """First VISIBLE element carrying this text (a label can exist twice in the
    DOM — Astryx sometimes keeps a hidden copy). `anchored=False`: substring
    search (a title followed by a detail)."""
    pattern = either_label(fr, en) if anchored else label_fragment(fr, en)
    locs = parent.get_by_text(pattern)
    for i in range(locs.count()):
        if locs.nth(i).is_visible():
            return locs.nth(i)
    return None


def open_playground_settings(page):
    """The PLAYGROUND's gear (the one in the top bar opens the account dialog —
    same label, different effect)."""
    buttons = page.get_by_role("button", name=either_label("Réglages", "Settings"))
    for i in range(buttons.count()):
        b = buttons.nth(i)
        if not b.is_visible():
            continue
        b.click()
        page.wait_for_timeout(700)
        if page.locator(".playground-settings-panel").count() > 0:
            return
        page.keyboard.press("Escape")  # not the right gear
        page.wait_for_timeout(400)
    raise JourneyFailure("playground", "settings panel",
                         "the « Playground settings » panel",
                         "none of the gears opens it")


# ── Browser ──────────────────────────────────────────────────────────────────
def start_playwright():
    """Late import: the browserless journeys (security) do not need it."""
    from playwright.sync_api import sync_playwright
    return sync_playwright()


def new_context(p, width=1600, height=1000):
    """Browser + fresh context (no cookie shared between journeys)."""
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": width, "height": height}, locale="fr-FR")
    ctx.set_default_timeout(30000)
    return b, ctx


def dismiss_onboarding(page) -> None:
    """Closes the first-visit onboarding if shown (« Skip »/« Close » button)."""
    for label in ("Skip", "Close", "Fermer", "Passer"):
        try:
            btn = page.get_by_role("button", name=re.compile(f"^{label}$", re.I))
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                page.wait_for_timeout(500)
                return
        except Exception:
            pass


def login(page, p: "Journey | None" = None) -> None:
    """Signs the demo account in on the login page.

    Measured trap (one loss in ~15 runs): filling the fields BEFORE React
    hydration fills them visually without ever feeding the form state — the
    « Sign in » button then stays disabled and the click times out. So we wait
    for the button to be enabled before clicking, and refill once if it is not.
    """
    check_demo(USER)
    page.goto(f"{BASE}/login", wait_until="domcontentloaded")
    user_field = page.locator('input[type="text"]').first
    password_field = page.locator('input[type="password"]').first
    submit = page.get_by_role("button", name=re.compile(r"^(Se connecter|Sign in)$", re.I)).first
    user_field.wait_for(state="visible", timeout=20000)

    submit_enabled = """() => {
        const b = [...document.querySelectorAll('button')].find(
            x => /^(Se connecter|Sign in)$/i.test((x.textContent || '').trim()));
        return !!b && !b.disabled;
    }"""
    for _attempt in range(3):
        user_field.click()
        user_field.fill(USER)
        password_field.click()
        password_field.fill(password())
        try:
            page.wait_for_function(submit_enabled, timeout=8000)
            break
        except Exception:
            continue
    submit.click()
    try:
        page.wait_for_url(lambda u: "/login" not in u, timeout=40000)
    except Exception:
        raise JourneyFailure(
            p.name if p else "login", "login",
            "a redirect to the home page after sign-in",
            f"URL stayed at {page.url} — page excerpt: "
            f"{page.locator('body').inner_text()[:150]!r}")
    dismiss_onboarding(page)


def open_playground(page) -> None:
    page.goto(f"{BASE}/playground", wait_until="domcontentloaded")
    dismiss_onboarding(page)
    composer(page).wait_for(state="visible", timeout=30000)


def composer(page):
    """The playground composer is a contenteditable (ChatComposerInput)."""
    return page.locator('div[role="textbox"]').first


def send_message(page, text: str) -> None:
    """Types into the composer and sends (Enter)."""
    field = composer(page)
    field.wait_for(state="visible", timeout=20000)
    field.click()
    # keyboard.type: the composer is contenteditable, a React fill() loses it.
    page.keyboard.type(text, delay=5)
    page.wait_for_timeout(300)
    page.keyboard.press("Enter")


STOP_BUTTON = re.compile(r"^(Arr[eê]ter|Stop)$", re.I)


def streaming(page) -> bool:
    return page.get_by_role("button", name=STOP_BUTTON).count() > 0


def wait_for_stream(page, p: "Journey", timeout_s: int = 300,
                    require_start: bool = True) -> None:
    """Waits for the stream to start (« Stop » button) then to end.
    `require_start=False`: only waits for the END (the stream may already be over)."""
    deadline = time.time() + timeout_s
    if require_start:
        while time.time() < deadline:
            if streaming(page):
                break
            page.wait_for_timeout(500)
        else:
            raise JourneyFailure(
                p.name, "stream started",
                "the « Stop » button appears (streamed answer)",
                "no stream detected")
    while time.time() < deadline:
        if not streaming(page):
            page.wait_for_timeout(800)  # let the message footer fill in
            return
        page.wait_for_timeout(1000)
    raise JourneyFailure(
        p.name, "stream ended",
        f"the answer ends within {timeout_s} s",
        "the stream is still running (« Stop » button still present)")


def last_answer(page) -> str:
    """Text of the thread (the last assistant bubble is in it)."""
    return page.locator("body").inner_text()


# ── HTTP (urllib, no dependency) ─────────────────────────────────────────────
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http(method: str, url: str, body=None, headers=None, timeout_s: int = 60,
         follow_redirects: bool = True):
    """Raw HTTP call. Returns (status, headers, body_text)."""
    data = None
    h = {"User-Agent": UA, "Accept": "*/*"}
    if body is not None:
        if isinstance(body, (dict, list)):
            data = _json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = body
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    handlers = [] if follow_redirects else [_NoRedirect()]
    op = urllib.request.build_opener(*handlers)
    try:
        with op.open(req, timeout=timeout_s) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read().decode("utf-8", "replace")
    except Exception as e:  # timeout, DNS, TLS…
        return None, {}, f"{type(e).__name__}: {e}"


def json_body(status, body):
    try:
        return _json.loads(body)
    except Exception:
        return None


# ── Test SQL (demo account only) ─────────────────────────────────────────────
def portal_sql(sql: str):
    """Runs one SQL statement in the portal container and returns the rows.
    STRICTLY for the demo account:
    - set/clear `local_users.is_admin` to test the Admin page (ALWAYS in
      try/finally, see j09_admin);
    - clean the model-request row created by j12 (same);
    - revoke the demo sessions a run opened (close_sessions.py).
    Nothing else: a journey never touches real data.
    """
    import subprocess
    if "demo" not in sql and "e2e" not in sql.lower():
        raise SystemExit(f"refused: a test SQL statement must target the demo account — {sql!r}")
    program = ("import sqlite3,sys;"
               "db=sqlite3.connect('/app/data/portal.db');"
               "cur=db.execute(sys.argv[1]);db.commit();"
               "print(cur.fetchall())")
    r = subprocess.run(["docker", "exec", "dgx-portal", "python3", "-c", program, sql],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"SQL failed ({sql[:60]}…): {r.stderr.strip()[:200]}")
    return r.stdout.strip()


# ── Console error capture (page sweep) ───────────────────────────────────────
def watch_console(page):
    """Collects a page's console / React errors. Returns the list."""
    errors: list[str] = []

    def _console(msg):
        if msg.type == "error":
            errors.append(msg.text[:300])

    def _pageerror(err):
        errors.append(f"pageerror: {str(err)[:300]}")

    page.on("console", _console)
    page.on("pageerror", _pageerror)
    return errors


# The error screens of the app, in both languages.
DOM_ERROR = re.compile(r"Something went wrong|Application error:|"
                       r"Une erreur est survenue|Internal Server Error", re.I)


def journey_failed(name: str, e: BaseException) -> int:
    """Normalized failure output, then exit code 1."""
    if isinstance(e, JourneyFailure):
        print(_c("31", f"✗ {e}"))
    elif isinstance(e, TimeLimitExceeded):
        print(_c("31", f"✗ journey « {name} » failed: overall time limit exceeded "
                       f"(the journey is bounded; a step got stuck)"))
    else:
        print(_c("31", f"✗ journey « {name} » failed: {type(e).__name__}: {e}"))
    return 1
