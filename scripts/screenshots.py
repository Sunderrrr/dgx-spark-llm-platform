#!/usr/bin/env python3
"""Screenshots of the Cronos portal — DARK theme, ENGLISH UI,
dedicated demonstration account.

Why a script rather than hand-taken screenshots: the image set published in
the README must stay consistent (same account, same theme, same language, same
size, no personal data). These properties get replayed at every UI change, and
two of them cannot be seen by eye:
   1. the framing — /admin shows higher up the notification email address and
      lower down the named budget requests, while the repo is PUBLIC;
   2. the real content — a screenshot taken while the account had admin rights
      shows an assistant offering to launch a model, which is not what a normal
      user sees.
Hence: explicit framing, automatic redaction, and an OCR check afterwards (the
model writing this script cannot see the images).

Usage (the three modes chain: capture, verify, install):
    python screenshots.py                 # capture all pages
    python screenshots.py playground      # a single page
    python screenshots.py --media         # the media pages whose model is running
    python screenshots.py --verify        # check the PNGs already produced
    python screenshots.py --install       # copy the validated PNGs into assets/

Proper names to redact (never in a public repo): list them, one per line
(« # » to comment), in a file outside the repo, and point at it with
SHOTS_FORBIDDEN_FILE:
    SHOTS_FORBIDDEN_FILE=/root/shots/noms-interdits.txt python screenshots.py --verify
Missing variable or unreadable file → only the STRUCTURAL patterns (email, key
`sk-…`, access-refusal text) apply, without error.

Prerequisites: playwright + chromium (`playwright install chromium`), and the
password of the demonstration account in SHOTS_PW_FILE (default
/root/shots/demo-credentials, chmod 600, outside the repo). The account is
created with `scripts/create-demo-account.py`; it is NOT an administrator —
/admin and /users require granting is_admin for the duration of the capture,
then removing it (the script refuses to capture those pages without the rights
and deletes the file: a « Administrators only » page must not end up in the
docs).

What the check guarantees, image by image: 3200x2000, dark background,
expected content present, and NO personal data in what the reader will see —
email address, API key, access-refusal text, and the proper names listed in
SHOTS_FORBIDDEN_FILE. Private IPs, on the other hand, are not searched for
AFTERWARDS: they are blurred BEFORE the capture (`SENSIBLE`, below), so they
exist in no pixel — that is the only reliable way not to publish them. The
check itself is done by OCR on the final PNG.
"""
import json
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

BASE = os.environ.get("SHOTS_BASE", "https://dgx.cronos.website")
USER = "demo"
PW_FILE = os.environ.get("SHOTS_PW_FILE", "/root/shots/demo-credentials")
try:
    PW = open(PW_FILE).read().strip()
except FileNotFoundError:
    sys.exit(f"mot de passe du compte de démonstration introuvable : {PW_FILE}\n"
             f"  → crée le compte avec scripts/create-demo-account.py puis écris le mot\n"
             f"    de passe dans ce fichier (chmod 600), ou renseigne SHOTS_PW_FILE.")
OUT = os.environ.get("SHOTS_OUT", "/root/shots/out")
W, H, DPR = 1600, 1000, 2

# (file name, route) — the order follows the README table
PAGES = [
    ("dashboard", "/"),
    ("playground", "/playground"),
    ("support", "/support"),
    ("ocr", "/ocr"),
    ("voice", "/voice"),
    ("video", "/video"),
    ("image", "/image"),
    ("music", "/music"),
    ("memory", "/memory"),
    ("search", "/search"),
    ("request", "/request"),
    ("admin", "/admin"),
    ("users", "/users"),
    ("ranking", "/ranking"),
]

# The portal's content container scrolls internally (the body does not scroll).
# /admin is framed on the backend row, the catalog and the logs — what the
# README advertises — and leaves out of frame the « Notification emails » card
# (y≈204) as well as the request tables (y≥1539).
SCROLL = {"admin": 340}

# No published screenshot may contain an email address, an API key, a private
# IP or another account's name: this net blurs the offending text before the
# shot, and the final PNG is re-checked by OCR.
SENSIBLE = (r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}"
            r"|sk-[A-Za-z0-9_\-]{6,}"
            r"|\b(?:10|172\.(?:1[6-9]|2[0-9]|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b")

# Media pages: their point lies in a RESULT on screen, yet no media model is
# loaded most of the time (only a chat model runs, the unified memory allowing
# no more). With no model loaded, the page shows « No OCR model is available » —
# publishing that instead of a demo would be a regression. So we keep the
# published capture as long as no media model is loaded; `screenshots.py ocr`
# forces the shot when one of them is running.
MEDIA = {"ocr", "voice", "video", "image", "music"}
SKIP_REFRESH = MEDIA          # une page média ne se capture qu'avec son backend

# Internal pages: never published (they list accounts, see .gitignore), so
# checked for shape but not for confidentiality.
INTERNAL = {"users", "ranking"}

# Admin-only pages: capturing them requires granting is_admin TEMPORARILY to
# the demonstration account (then removing it).
ADMIN_ONLY = {"admin", "users"}

# Pages that only make sense with a real exchange on screen: we clear the
# account's history, then ask a question in English and wait for the end of the
# stream before triggering.
PROMPTS = {
    "playground": "Explain in three sentences what this platform does.",
    "support": "Which models are available right now?",
}

MEM_FACTS = [
    ("This platform", "runs on", "a single DGX Spark (GB10, 128 GB of unified memory)"),
    ("LiteLLM", "acts as", "the single gateway to the models, with a per-account quota"),
    ("the playground", "bills", "tokens to the user's own key, never the master key"),
    ("API keys", "are created", "from the My API keys page"),
    ("Support", "can", "create a key, request budget or request a model"),
]


def log(msg):
    print(f"  {msg}", flush=True)


def media_prets(page):
    """Media backends actually loaded, seen by the portal itself.

    `/api/health` is the right source: the sidecars are on-demand, so their
    absence is a normal state and not a failure, and the portal is the one that
    knows which answers (the one the Admin shows)."""
    return page.evaluate("""() => fetch('/api/health').then(r => r.json())
        .then(d => Object.entries(d.services || {})
          .filter(([k, v]) => ['ocr', 'voice', 'video', 'image', 'music'].includes(k)
                            && v && v.ready)
          .map(([k]) => k))""")


def login(page):
    """Wait for React hydration BEFORE filling: otherwise the fields are
    rewritten by the first client render and the button stays disabled."""
    page.goto(f"{BASE}/login", wait_until="networkidle")
    page.wait_for_selector('input[type="password"]')
    page.wait_for_timeout(1500)
    page.locator('input[type="text"]').first.fill(USER)
    page.locator('input[type="password"]').first.fill(PW)
    page.get_by_role("button", name=re.compile("Sign in|Se connecter", re.I)).first.click()
    page.wait_for_url(re.compile(rf"{re.escape(BASE)}/(?!login)"), timeout=30000)
    log(f"connecté en tant que {USER}")


def prep(page):
    """API key (the playground and Support bill the account) and a few
    memories: otherwise the screenshots show empty pages."""
    token = page.evaluate("fetch('/api/csrf').then(r => r.json()).then(d => d.token)")
    have = page.evaluate("fetch('/api/keys').then(r => r.json()).then(d => (d.user_keys || []).length)")
    if have:
        log(f"clé API : {have} déjà présente(s)")
    else:
        st = page.evaluate("""async ([tok]) => {
            const f = new URLSearchParams({action: 'create', key_name: 'captures', csrf_token: tok});
            const res = await fetch('/keys', {method: 'POST', body: f,
                headers: {'X-CSRFToken': tok, 'Content-Type': 'application/x-www-form-urlencoded'}});
            return res.status;
        }""", [token])
        log(f"clé API créée (HTTP {st})")
    # Deterministic rather than idempotent: we purge then rewrite the five
    # facts. A plain "add only if missing" let the graph drift — the model
    # extracts its own memories from the capture conversations, and the account
    # ended with 37 facts (or 8, or 5) depending on the run history. Yet the
    # Memory page is published: it must show the same thing at every shot.
    page.evaluate("""async ([tok]) => {
        await fetch('/api/memory/purge', {method: 'POST',
            headers: {'X-CSRFToken': tok, 'Content-Type': 'application/json'},
            body: JSON.stringify({})});
    }""", [token])
    for subj, rel, fact in MEM_FACTS:
        page.evaluate("""async ([tok, subj, rel, fact]) => {
            await fetch('/api/memory/facts', {method: 'POST',
                headers: {'X-CSRFToken': tok, 'Content-Type': 'application/json'},
                body: JSON.stringify({subject: subj, relation: rel, fact: fact})});
        }""", [token, subj, rel, fact])
    log(f"mémoire purgée puis réécrite : {len(MEM_FACTS)} faits")
    return token


def reset_history(page, token, name):
    """Clear the history so the screenshot shows the exchange we just set up,
    and not a residue of a previous run (which could, for example, date from a
    session where the account had admin rights)."""
    if name == "support":
        page.evaluate("""async ([tok]) => { await fetch('/support/thread/clear', {method: 'POST',
            headers: {'X-CSRFToken': tok}}); }""", [token])
    elif name == "playground":
        page.evaluate("""async ([tok]) => {
            const d = await fetch('/api/conversations').then(r => r.json());
            for (const c of (d.conversations || [])) {
                await fetch('/conversations', {method: 'POST',
                    headers: {'X-CSRFToken': tok, 'Content-Type': 'application/x-www-form-urlencoded'},
                    body: new URLSearchParams({action: 'delete', id: String(c.id)})});
            }
        }""", [token])


def ask(page, prompt, wait_ms=120000):
    """Ask a question and wait for the end of the stream (the screen settles)."""
    page.wait_for_timeout(2500)
    # ChatComposerInput (Astryx) is a contentEditable, not a textarea: the page's
    # <textarea> elements belong to the settings panels, rendered inside closed
    # <dialog>s (size 0). fill() does not apply either.
    box = page.locator('[contenteditable="true"]:visible').last
    box.wait_for(state="visible", timeout=30000)
    box.click()
    box.type(prompt, delay=12)
    page.wait_for_timeout(300)
    box.press("Enter")
    deadline, stable, previous = time.time() + wait_ms / 1000, 0, ""
    while time.time() < deadline:
        page.wait_for_timeout(1500)
        txt = page.locator("body").inner_text()
        if txt == previous:
            stable += 1
            if stable >= 3 and len(txt) > 400:
                break
        else:
            stable, previous = 0, txt
    log(f"réponse obtenue ({len(previous)} caractères à l'écran)")


def main():
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    pages = [p for p in PAGES if not only or p[0] in only]
    report, errors = {}, []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--force-color-profile=srgb", "--hide-scrollbars"])
        ctx = browser.new_context(
            viewport={"width": W, "height": H}, device_scale_factor=DPR,
            color_scheme="dark", locale="en-US",
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
        # Dark mode is purely local (localStorage); the language and the
        # palette come from the account preferences (/api/whoami), so nothing to
        # force here — forcing the language would cause a hydration mismatch.
        ctx.add_init_script("localStorage.setItem('cronos_theme_mode','dark');")
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        login(page)
        token = prep(page)
        if "--media" in sys.argv:
            prets = media_prets(page)
            if prets:
                log(f"backends média chargés : {', '.join(prets)}")
                only = sorted(set(only) | set(prets))
            else:
                log("aucun backend média n'est chargé — rien à capturer. Démarre-en un "
                    "depuis Admin (ou attends qu'un utilisateur s'en serve), puis relance")
                log("  les cinq pages resteront sur leur capture publiée.")
                pages = []
        for name, route in pages:
            if name in SKIP_REFRESH and not only:
                log(f"{name} : ignorée (aucun modèle média chargé — capture publiée conservée)")
                continue
            if name in PROMPTS:
                reset_history(page, token, name)
            page.goto(f"{BASE}{route}", wait_until="networkidle")
            if name in PROMPTS:
                ask(page, PROMPTS[name])
            else:
                page.wait_for_timeout(2500)
            if name in SCROLL:
                page.evaluate("""(y) => {
                    const sc = [...document.querySelectorAll('*')].find(el =>
                        el.scrollHeight - el.clientHeight > 120 && el.clientHeight > 300);
                    if (sc) sc.scrollTop = y;
                }""", SCROLL[name])
                page.wait_for_timeout(1200)
            blurred = page.evaluate("""(src) => {
                const re = new RegExp(src, 'i');
                let n = 0;
                for (const el of document.querySelectorAll('*')) {
                    if (el.children.length === 0 && re.test(el.textContent || '')) {
                        el.style.filter = 'blur(5px)'; n++;
                    }
                }
                return n;
            }""", SENSIBLE)
            page.screenshot(path=f"{OUT}/{name}.png")
            text = page.locator("body").inner_text()
            # /admin and /users require an administrator account: without
            # rights, the page shows a refusal, which must NOT be published as a
            # screenshot of the feature. We delete the file and report it.
            if name in ADMIN_ONLY and re.search(r"Administrators only|does not have the rights", text):
                os.remove(f"{OUT}/{name}.png")
                log(f"REFUSÉ : {name} demande un compte admin — capture supprimée "
                    f"(accorder is_admin temporairement, cf. docstring)")
                continue
            open(f"{OUT}/{name}.txt", "w").write(text)
            report[name] = {"route": route, "chars": len(text), "redacted": blurred}
            log(f"capture {name:<10} {route:<12} {len(text):5} caractères"
                + (f", {blurred} masqué(s)" if blurred else ""))
        browser.close()
    if errors:
        log(f"erreurs JS : {sorted(set(errors))[:2]}")
    json.dump(report, open(f"{OUT}/report.json", "w"), indent=1, ensure_ascii=False)
    log("terminé — enchaîner avec --verify puis --install")


def _ocr(path):
    """Text actually visible in the image (tesseract): the only reliable proof
    for a script that cannot judge the screenshot by eye."""
    import shutil
    import subprocess
    import tempfile
    if not shutil.which("tesseract"):
        sys.exit("tesseract est requis par --verify (lecture du texte des PNG) : "
                 "l'installer ou vérifier les captures à l'œil.")
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["tesseract", path, f"{d}/t", "-l", "eng", "--psm", "6"],
                       capture_output=True)
        return open(f"{d}/t.txt").read()


def _forbidden_re():
    """« données personnelles » patterns: structural ones always active (email
    address, key `sk-…`, access-refusal text), plus the proper names listed in
    the file pointed at by SHOTS_FORBIDDEN_FILE (one pattern per line, `#` for
    comments). Missing variable or unreadable file → structural ones only."""
    motif = (r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}|sk-[A-Za-z0-9_\-]{6,}"
             r"|Administrators only|does not have the rights|Traceback")
    chemin = os.environ.get("SHOTS_FORBIDDEN_FILE")
    noms = []
    if chemin:
        try:
            with open(chemin, encoding="utf-8") as f:
                noms = [l.strip() for l in f
                        if l.strip() and not l.strip().startswith("#")]
        except OSError:
            noms = []
    if noms:
        motif += r"|\b(" + "|".join(re.escape(n) for n in noms) + r")\b"
    return re.compile(motif, re.I)


def verify(folder="assets"):
    """Check each published screenshot: size, theme, content, personal data."""
    from PIL import Image
    expected = {
        "dashboard": [r"Demo", r"Availability"],
        "playground": [r"LiteLLM|platform"],
        "support": [r"Cronos", r"message"],
        "ocr": [r"OCR"], "voice": [r"Voice"], "video": [r"Video"],
        "image": [r"Image"], "music": [r"Music"],
        "memory": [r"Memor"], "search": [r"model"], "request": [r"Request"],
        "admin": [r"Logs", r"Add a model"],
        "users": [r"User"],
        "ranking": [r"Leaderboard|Rank"],
    }
    forbidden = _forbidden_re()
    # The markers above are not enough for the media pages: a page whose backend
    # is off still shows its title (« OCR »), so an EMPTY capture would pass them
    # all while announcing « Ask an admin to start… ». That is precisely the
    # screenshot not to publish, we refuse it by name.
    vide = re.compile(r"Ask an admin to (?:add|start|launch) an? \w+ model"
                      r"|Demande à un admin (?:d'ajouter|de démarrer)", re.I)
    # Media pages whose published capture only showed the empty state: removed
    # from the repo (README → Screenshots). Missing = normal, not a failure; as
    # soon as a new capture exists, it goes through the same checks as the
    # others, empty-state guard included.
    retirees = {"voice", "music"}
    ko = 0
    for name, marks in expected.items():
        path = f"{folder}/{name}.png"
        if not os.path.exists(path):
            if name in retirees:
                print(f"  {name:<10} · retirée (capture d'état vide) — non publiée")
            else:
                print(f"  {name:<10} ABSENT")
                ko += 1
            continue
        im = Image.open(path)
        g = im.convert("L").resize((160, 100))
        px = list(g.getdata())
        lum = sum(px) / len(px)
        txt = _ocr(path)
        problems = []
        if im.size != (W * DPR, H * DPR):
            problems.append(f"taille {im.size}")
        if lum >= 90:
            problems.append(f"thème clair (luminosité {lum:.0f})")
        if len(set(px)) < 20:
            problems.append("image quasi vide")
        if name not in INTERNAL and forbidden.search(txt):
            problems.append("donnée personnelle : " + forbidden.search(txt).group(0)[:38])
        if name in INTERNAL and forbidden.search(txt):
            print(f"  {name:<10} · interne, hors dépôt — contient "
                  f"« {forbidden.search(txt).group(0)[:24]} » (normal, non publiée)")
        for m in marks:
            if not re.search(m, txt, re.I):
                problems.append(f"contenu attendu absent ({m})")
        if name in MEDIA and vide.search(txt):
            problems.append("page média SANS backend chargé (état vide) : "
                            "démarre le sidecar puis refais la capture")
        if problems:
            ko += 1
            print(f"  {name:<10} ✗ " + " ; ".join(problems))
        else:
            print(f"  {name:<10} ✓ {im.size[0]}x{im.size[1]}, sombre ({lum:.0f}), "
                  f"contenu et confidentialité OK")
    return ko


if "--verify" in sys.argv:
    i = sys.argv.index("--verify")
    nb = verify(sys.argv[i + 1] if len(sys.argv) > i + 1 else "assets")
    print(f"  {nb} capture(s) en échec" if nb else "  toutes les captures sont conformes")
    sys.exit(1 if nb else 0)
if "--install" in sys.argv:
    import shutil
    noms = [a for a in sys.argv[1:] if not a.startswith("-")]
    for name, _ in PAGES:
        # Never overwrite a published screenshot with a shot we did not intend:
        # media pages are only installed if named.
        if name in SKIP_REFRESH and name not in noms and "--media" not in sys.argv:
            print(f"  conservé : assets/{name}.png (page média, non recapturée)")
            continue
        src = f"{OUT}/{name}.png"
        if os.path.exists(src):
            shutil.copy(src, f"assets/{name}.png")
            print(f"  installé : assets/{name}.png")
else:
    main()
