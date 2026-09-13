#!/usr/bin/env python3
"""Captures d'écran du portail Cronos — thème SOMBRE, interface ANGLAISE,
compte de démonstration dédié.

Pourquoi un script plutôt que des captures à la main : le jeu d'images publié
dans le README doit rester cohérent (même compte, même thème, même langue, même
taille, aucune donnée personnelle). Ces propriétés se rejouent à chaque
évolution de l'interface, et deux d'entre elles ne se voient pas à l'œil :
   1. le cadrage — /admin expose plus haut l'adresse email de notification et
      plus bas les demandes de budget nominatives, or le dépôt est PUBLIC ;
   2. le contenu réel — une capture prise pendant que le compte avait des
      droits admin montre un assistant qui propose de lancer un modèle, ce qui
      n'est pas ce que voit un utilisateur normal.
D'où : cadrage explicite, censure automatique, et vérification par OCR après
coup (le modèle qui écrit ce script ne voit pas les images).

Usage (les trois modes s'enchaînent : capturer, vérifier, installer) :
    python screenshots.py                 # capture toutes les pages
    python screenshots.py playground      # une seule page
    python screenshots.py --media         # les pages média dont le modèle tourne
    python screenshots.py --verify        # contrôle les PNG déjà produits
    python screenshots.py --install       # copie les PNG validés dans assets/

Prérequis : playwright + chromium (`playwright install chromium`), et le mot de
passe du compte de démonstration dans SHOTS_PW_FILE (défaut
/root/shots/demo-credentials, chmod 600, hors dépôt). Le compte se crée avec
`scripts/create-demo-account.py` ; il n'est PAS administrateur — /admin et
/users demandent d'accorder is_admin le temps de la prise de vue, puis de le
retirer (le script refuse de capturer ces pages sans les droits et supprime le
fichier : une page « Administrators only » ne doit pas finir dans la doc).

Ce que la vérification garantit, image par image : 3200x2000, fond sombre,
contenu attendu présent, et AUCUNE donnée personnelle (email, clé API, IP privée,
nom d'un autre compte) — le contrôle se fait par OCR sur le PNG final, donc sur
ce que le lecteur verra réellement.
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

# (nom de fichier, route) — l'ordre suit le tableau du README
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

# Le conteneur de contenu du portail défile en interne (le body ne défile pas).
# /admin est cadré sur la ligne backend, le catalogue et les logs — ce que le
# README annonce — et laisse hors champ la carte « Notification emails » (y≈204)
# comme les tableaux de demandes (y≥1539).
SCROLL = {"admin": 340}

# Aucune capture publiée ne doit contenir d'adresse email, de clé API, d'IP
# privée ou du nom d'un autre compte : ce filet floute le texte fautif avant la
# prise de vue, et le PNG final est revérifié par OCR.
SENSIBLE = (r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}"
            r"|sk-[A-Za-z0-9_\-]{6,}"
            r"|\b(?:10|172\.(?:1[6-9]|2[0-9]|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b")

# Pages média : leur intérêt tient à un RÉSULTAT à l'écran, or aucune machine
# média n'est chargée la plupart du temps (seul un modèle de chat tourne, la
# mémoire unifiée ne permettant pas plus). Sans modèle chargé, la page affiche
# « No OCR model is available » — publier cela à la place d'une démonstration
# serait une régression. On garde donc la capture publiée tant qu'aucun modèle
# média n'est chargé ; `screenshots.py ocr` force la prise de vue quand l'un
# d'eux tourne.
SKIP_REFRESH = {"ocr", "voice", "video", "image", "music"}

# Pages internes : jamais publiées (elles listent des comptes, cf. .gitignore),
# donc contrôlées pour la forme mais pas pour la confidentialité.
INTERNAL = {"users", "ranking"}

# Pages réservées aux administrateurs : leur capture demande d'accorder
# TEMPORAIREMENT is_admin au compte de démonstration (puis de le retirer).
ADMIN_ONLY = {"admin", "users"}

# Pages qui n'ont d'intérêt qu'avec un échange réel à l'écran : on efface
# l'historique du compte puis on pose une question en anglais et on attend la
# fin du flux avant de déclencher.
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
    """Backends média réellement chargés, vus par le portail lui-même.

    `/api/health` est la bonne source : les sidecars sont on-demand, donc leur
    absence est un état normal et non une panne, et c'est le portail qui sait
    lequel répond (celui qu'affiche l'Admin)."""
    return page.evaluate("""() => fetch('/api/health').then(r => r.json())
        .then(d => Object.entries(d.services || {})
          .filter(([k, v]) => ['ocr', 'voice', 'video', 'image', 'music'].includes(k)
                            && v && v.ready)
          .map(([k]) => k))""")


def login(page):
    """Attend l'hydratation React AVANT de remplir : sinon les champs sont
    réécrits par le premier rendu client et le bouton reste désactivé."""
    page.goto(f"{BASE}/login", wait_until="networkidle")
    page.wait_for_selector('input[type="password"]')
    page.wait_for_timeout(1500)
    page.locator('input[type="text"]').first.fill(USER)
    page.locator('input[type="password"]').first.fill(PW)
    page.get_by_role("button", name=re.compile("Sign in|Se connecter", re.I)).first.click()
    page.wait_for_url(re.compile(rf"{re.escape(BASE)}/(?!login)"), timeout=30000)
    log(f"connecté en tant que {USER}")


def prep(page):
    """Clé API (le playground et le Support facturent le compte) et quelques
    souvenirs : sinon les captures montrent des pages vides."""
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
    # Déterministe plutôt qu'idempotent : on purge puis on réécrit les cinq
    # faits. Un simple « n'ajoute que s'il manque » laissait le graphe dériver —
    # le modèle extrait ses propres souvenirs des conversations de capture, et le
    # compte finissait avec 37 faits (ou 8, ou 5) selon l'historique des
    # exécutions. Or la page Mémoire est publiée : elle doit montrer la même chose
    # à chaque prise de vue.
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
    """Efface l'historique pour que la capture montre l'échange qu'on vient de
    poser, et non un résidu d'une exécution précédente (qui pourrait, par
    exemple, dater d'une session où le compte avait des droits admin)."""
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
    """Pose une question et attend la fin du flux (l'écran se stabilise)."""
    page.wait_for_timeout(2500)
    # ChatComposerInput (Astryx) est un contentEditable, pas un textarea : les
    # <textarea> de la page appartiennent aux panneaux de réglages, rendus dans
    # des <dialog> fermés (taille 0). fill() ne s'applique pas non plus.
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
        # Le mode sombre est purement local (localStorage) ; la langue et la
        # palette viennent des préférences du compte (/api/whoami), donc rien à
        # forcer ici — forcer la langue provoquerait un écart d'hydratation.
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
            # /admin et /users exigent un compte administrateur : sans droits, la
            # page affiche un refus, qu'il ne faut PAS publier comme capture de la
            # fonctionnalité. On supprime le fichier et on le signale.
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
    """Texte réellement visible dans l'image (tesseract) : seule preuve fiable
    pour un script qui ne peut pas juger la capture à l'œil."""
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


def verify(folder="assets"):
    """Contrôle chaque capture publiée : taille, thème, contenu, données perso."""
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
    forbidden = re.compile(
        r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}|sk-[A-Za-z0-9_\-]{6,}"
        r"|Administrators only|does not have the rights|Traceback"
        r"|\b(aabdou|bmaziane|ccrespy|cestienne|fgerber|kflorentin|lbozier|mboitel"
        r"|mbouchet|mpigeon|nlerou|teych|yidjahurtos|zolan|Bozier|Boitel)\b", re.I)
    ko = 0
    for name, marks in expected.items():
        path = f"{folder}/{name}.png"
        if not os.path.exists(path):
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
        # Ne jamais écraser une capture publiée par une prise de vue qu'on n'a
        # pas voulue : les pages média ne sont installées que si on les nomme.
        if name in SKIP_REFRESH and name not in noms and "--media" not in sys.argv:
            print(f"  conservé : assets/{name}.png (page média, non recapturée)")
            continue
        src = f"{OUT}/{name}.png"
        if os.path.exists(src):
            shutil.copy(src, f"assets/{name}.png")
            print(f"  installé : assets/{name}.png")
else:
    main()
