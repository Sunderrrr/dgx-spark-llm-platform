# -*- coding: utf-8 -*-
"""Commun aux parcours e2e (tests/e2e/) — le socle, jamais le parcours lui-même.

Chaque parcours (j*.py) est un script autonome lancé par `scripts/valider.sh`
avec {PYTEST_BIN} (Playwright y est installé). Ce module donne :

- la configuration (compte démo, bases, mot de passe lu dans un fichier hors
  dépôt — jamais affiché, jamais journalisé) ;
- des messages de parcours lisibles : « ✓ étape » / « ✗ parcours « X » a échoué
  à l'étape « Y » : attendu …, obtenu … » ;
- une borne de temps HARD par parcours (SIGALRM) : aucun parcours ne peut
  tourner indéfiniment, même bloqué dans Playwright ;
- des utilitaires de navigateur (connexion, composeur, attente de fin de
  diffusion) et d'HTTP (appels API domaine, contrôles de sécurité).

Règles d'or rappelées : le compte démo SEUL, aucune action destructive, aucun
redémarrage de modèle — un parcours envoie des requêtes, il ne pilote jamais
l'infrastructure.
"""
from __future__ import annotations

from config import (WEB, API, DOMAINE, DOMAINE_SSO, RACINE, DEMO_PASS, DEMO_USER,
                    S3_CFG, S3_BUCKET, PYTEST_BIN)
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
import json as _json

# ── Configuration ────────────────────────────────────────────────────────────
BASE = os.environ.get("E2E_BASE", WEB)
API = os.environ.get("E2E_API", API)
UTILISATEUR = os.environ.get("E2E_USER", "demo")
FICHIER_MDP = os.environ.get("E2E_PW_FILE", "{DEMO_PASS}")
# User-Agent réel : Cloudflare refuse l'UA par défaut de Python (erreur 1010).
UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

COULEURS = sys.stdout.isatty()


def _c(code: str, texte: str) -> str:
    return f"\033[{code}m{texte}\033[0m" if COULEURS else texte


# ── Erreurs de parcours ──────────────────────────────────────────────────────
class EchecParcours(Exception):
    """Un geste attendu ne s'est pas produit. Le message dit où et quoi."""

    def __init__(self, parcours: str, etape: str, attendu: str, obtenu: str):
        self.parcours, self.etape = parcours, etape
        self.attendu, self.obtenu = attendu, obtenu
        super().__init__(str(self))

    def __str__(self) -> str:
        return (f"parcours « {self.parcours} » a échoué à l'étape « {self.etape} » : "
                f"attendu {self.attendu}, obtenu {self.obtenu}")


class DelaiDepasse(Exception):
    pass


def _alarme(_sig, _frm):  # pragma: no cover — appelé par SIGALRM
    raise DelaiDepasse("délai dépassé")


class Parcours:
    """Horloge + impressions d'étapes d'un parcours."""

    def __init__(self, nom: str):
        self.nom = nom
        self.debut = time.time()
        self._dernier = self.debut
        self.nb_etapes = 0
        print(f"▶ parcours « {nom} »")

    def ok(self, etape: str, detail: str = "") -> None:
        self.nb_etapes += 1
        maintenant = time.time()
        duree = maintenant - self._dernier
        self._dernier = maintenant
        suffixe = f" — {detail}" if detail else ""
        print(f"  {_c('32', '✓')} {etape}{suffixe} ({duree:.1f} s)")

    def avertissement(self, etape: str, detail: str = "") -> None:
        suffixe = f" — {detail}" if detail else ""
        print(f"  {_c('33', '⚠')} {etape}{suffixe}")

    def rate(self, etape: str, attendu: str, obtenu: str) -> None:
        raise EchecParcours(self.nom, etape, attendu, obtenu)

    def fin(self) -> None:
        print(f"{_c('32', '✓')} parcours « {self.nom} » — OK "
              f"({self.nb_etapes} étapes, {time.time() - self.debut:.1f} s)")


def borne(secondes: int):
    """Pose la borne de temps hard du parcours (SIGALRM)."""
    signal.signal(signal.SIGALRM, _alarme)
    signal.alarm(secondes)


def mot_de_passe() -> str:
    """Le mot de passe du compte de démonstration. Jamais affiché."""
    try:
        with open(FICHIER_MDP, encoding="utf-8") as f:
            return f.read().strip()
    except OSError as e:
        print(f"✗ impossible de lire {FICHIER_MDP} : {e}", file=sys.stderr)
        raise SystemExit(2)


def verifier_demo(username: str) -> None:
    """Garde : un parcours ne parle qu'au compte de démonstration."""
    if username != UTILISATEUR:
        raise SystemExit(f"refusé : ce parcours n'utilise que le compte « {UTILISATEUR} »")


# ── Libellés bilingues ───────────────────────────────────────────────────────
# Le compte de démonstration affiche l'ANGLAIS (préférence de compte enregistrée
# côté serveur) : les sélecteurs acceptent donc les deux libellés. La chaîne
# française reste la clé i18n (identité), l'anglais est sa traduction.
def alternance(fr: str, en: str) -> "re.Pattern":
    """Motif « libellé exact » dans les deux langues."""
    return re.compile(rf"^(?:{re.escape(fr)}|{re.escape(en)})$", re.I)


def fragment(fr: str, en: str) -> "re.Pattern":
    """Motif « libellé contenu dans le texte » dans les deux langues."""
    return re.compile(rf"(?:{re.escape(fr)}|{re.escape(en)})", re.I)


def bouton(page, fr: str, en: str):
    """Le bouton portant ce libellé (accessible name), FR ou EN."""
    return page.get_by_role("button", name=alternance(fr, en))


def texte_visible(parent, fr: str, en: str, ancre: bool = True):
    """Premier élément VISIBLE portant ce texte (un libellé peut exister en
    doublon dans le DOM — un Astryx garde parfois une copie masquée).
    `ancre=False` : recherche en sous-chaîne (titre suivi d'un détail)."""
    motif = alternance(fr, en) if ancre else fragment(fr, en)
    locs = parent.get_by_text(motif)
    for i in range(locs.count()):
        if locs.nth(i).is_visible():
            return locs.nth(i)
    return None


def ouvrir_reglages_playground(page):
    """La roue crantée du PLAYGROUND (celle de la barre du haut ouvre le
    dialogue du compte — même libellé, effet différent)."""
    boutons = page.get_by_role("button", name=alternance("Réglages", "Settings"))
    for i in range(boutons.count()):
        b = boutons.nth(i)
        if not b.is_visible():
            continue
        b.click()
        page.wait_for_timeout(700)
        if page.locator(".playground-settings-panel").count() > 0:
            return
        page.keyboard.press("Escape")  # ce n'était pas la bonne roue
        page.wait_for_timeout(400)
    raise EchecParcours("playground", "panneau de réglages",
                        "le panneau « Réglages du playground »",
                        "aucune des roues crantées ne l'ouvre")


# ── Navigateur ───────────────────────────────────────────────────────────────
def lancer_playwright():
    """Import tardif : les parcours sans navigateur (sécurité) n'en ont pas besoin."""
    from playwright.sync_api import sync_playwright
    return sync_playwright()


def nouveau_contexte(p, largeur=1600, hauteur=1000):
    """Navigateur + contexte neuf (aucun cookie partagé entre parcours)."""
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": largeur, "height": hauteur}, locale="fr-FR")
    ctx.set_default_timeout(30000)
    return b, ctx


def fermer_accueil(page) -> None:
    """Ferme l'éventuel onboarding de première visite (bouton « Skip »/« Close »)."""
    for label in ("Skip", "Close", "Fermer", "Passer"):
        try:
            btn = page.get_by_role("button", name=re.compile(f"^{label}$", re.I))
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                page.wait_for_timeout(500)
                return
        except Exception:
            pass


def connexion(page, p: "Parcours | None" = None) -> None:
    """Connexion du compte démo sur la page de connexion.

    Piège mesuré (une perte sur ~15 exécutions) : remplir les champs AVANT
    l'hydratation React les remplit visuellement sans jamais alimenter l'état
    du formulaire — le bouton « Se connecter » reste alors désactivé et le clic
    expire. On attend donc que le bouton soit actif avant de cliquer, et on
    ré-remplit une fois s'il ne l'est pas.
    """
    verifier_demo(UTILISATEUR)
    page.goto(f"{BASE}/login", wait_until="domcontentloaded")
    champ_user = page.locator('input[type="text"]').first
    champ_mdp = page.locator('input[type="password"]').first
    bouton = page.get_by_role("button", name=re.compile(r"^(Se connecter|Sign in)$", re.I)).first
    champ_user.wait_for(state="visible", timeout=20000)

    bouton_actif = """() => {
        const b = [...document.querySelectorAll('button')].find(
            x => /^(Se connecter|Sign in)$/i.test((x.textContent || '').trim()));
        return !!b && !b.disabled;
    }"""
    for _tentative in range(3):
        champ_user.click()
        champ_user.fill(UTILISATEUR)
        champ_mdp.click()
        champ_mdp.fill(mot_de_passe())
        try:
            page.wait_for_function(bouton_actif, timeout=8000)
            break
        except Exception:
            continue
    bouton.click()
    try:
        page.wait_for_url(lambda u: "/login" not in u, timeout=40000)
    except Exception:
        raise EchecParcours(
            p.nom if p else "connexion", "connexion",
            "une redirection vers l'accueil après identification",
            f"URL restée à {page.url} — extrait de la page : "
            f"{page.locator('body').inner_text()[:150]!r}")
    fermer_accueil(page)


def ouvrir_playground(page) -> None:
    page.goto(f"{BASE}/playground", wait_until="domcontentloaded")
    fermer_accueil(page)
    composer(page).wait_for(state="visible", timeout=30000)


def composer(page):
    """Le composeur du playground est un contenteditable (ChatComposerInput)."""
    return page.locator('div[role="textbox"]').first


def envoyer_message(page, texte: str) -> None:
    """Écrit dans le composeur et envoie (Entrée)."""
    champ = composer(page)
    champ.wait_for(state="visible", timeout=20000)
    champ.click()
    # keyboard.type : le composeur est contenteditable, un fill() React le perd.
    page.keyboard.type(texte, delay=5)
    page.wait_for_timeout(300)
    page.keyboard.press("Enter")


BOUTON_STOP = re.compile(r"^(Arr[eê]ter|Stop)$", re.I)


def streaming(page) -> bool:
    return page.get_by_role("button", name=BOUTON_STOP).count() > 0


def attendre_stream(page, p: "Parcours", delai_s: int = 300,
                    exige_debut: bool = True) -> None:
    """Attend que la diffusion commence (bouton « Arrêter ») puis qu'elle finisse.
    `exige_debut=False` : on n'attend que la FIN (le flux peut déjà être clos)."""
    limite = time.time() + delai_s
    if exige_debut:
        while time.time() < limite:
            if streaming(page):
                break
            page.wait_for_timeout(500)
        else:
            raise EchecParcours(
                p.nom, "diffusion lancée",
                "le bouton « Arrêter » apparaît (réception en flux)",
                "aucune diffusion détectée")
    while time.time() < limite:
        if not streaming(page):
            page.wait_for_timeout(800)  # laisser le pied de message se remplir
            return
        page.wait_for_timeout(1000)
    raise EchecParcours(
        p.nom, "diffusion terminée",
        f"la réponse se termine en moins de {delai_s} s",
        "le flux tourne encore (bouton « Arrêter » toujours présent)")


def derniere_reponse(page) -> str:
    """Texte de la dernière bulle assistant (le fil entier moins la question)."""
    return page.locator("body").inner_text()


# ── HTTP (urllib, sans dépendance) ───────────────────────────────────────────
class _SansRedirection(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http(methode: str, url: str, corps=None, entetes=None, delai_s: int = 60,
         suivre_redirections: bool = True):
    """Appel HTTP brut. Retourne (statut, entêtes, corps_texte)."""
    data = None
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if corps is not None:
        if isinstance(corps, (dict, list)):
            data = _json.dumps(corps).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif isinstance(corps, str):
            data = corps.encode("utf-8")
        else:
            data = corps
    if entetes:
        headers.update(entetes)
    req = urllib.request.Request(url, data=data, headers=headers, method=methode)
    handlers = [] if suivre_redirections else [_SansRedirection()]
    op = urllib.request.build_opener(*handlers)
    try:
        with op.open(req, timeout=delai_s) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read().decode("utf-8", "replace")
    except Exception as e:  # timeout, DNS, TLS…
        return None, {}, f"{type(e).__name__}: {e}"


def corps_json(statut, corps):
    try:
        return _json.loads(corps)
    except Exception:
        return None


# ── SQL de test (compte démo uniquement) ─────────────────────────────────────
def sql_portail(sql: str):
    """Exécute une instruction SQL dans le conteneur du portail et renvoie les
    lignes. Usage STRICTEMENT réservé au compte de démonstration :
    - poser/retirer `local_users.is_admin` pour tester la page Admin
      (TOUJOURS en try/finally, cf. j09_admin) ;
    - nettoyer la ligne de demande de modèle créée par j12 (idem).
    Rien d'autre : un parcours ne touche jamais aux données réelles.
    """
    import subprocess
    if "demo" not in sql and "e2e" not in sql.lower():
        raise SystemExit(f"refusé : une instruction SQL de test vise le compte démo — {sql!r}")
    programme = ("import sqlite3,sys;"
                 "db=sqlite3.connect('/app/data/portal.db');"
                 "cur=db.execute(sys.argv[1]);db.commit();"
                 "print(cur.fetchall())")
    r = subprocess.run(["docker", "exec", "dgx-portal", "python3", "-c", programme, sql],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"SQL échoué ({sql[:60]}…) : {r.stderr.strip()[:200]}")
    return r.stdout.strip()


# ── Journalisation des erreurs console (balayage de pages) ───────────────────
def surveiller_console(page):
    """Collecte les erreurs console / React d'une page. Renvoie la liste."""
    erreurs: list[str] = []

    def _console(msg):
        if msg.type == "error":
            erreurs.append(msg.text[:300])

    def _pageerror(err):
        erreurs.append(f"pageerror: {str(err)[:300]}")

    page.on("console", _console)
    page.on("pageerror", _pageerror)
    return erreurs


ERREUR_DOM = re.compile(r"Something went wrong|Application error:|"
                        r"Une erreur est survenue|Internal Server Error", re.I)


def fin_parcours_echec(nom: str, e: BaseException) -> int:
    """Impression normalisée d'un échec puis code de sortie 1."""
    if isinstance(e, EchecParcours):
        print(_c("31", f"✗ {e}"))
    elif isinstance(e, DelaiDepasse):
        print(_c("31", f"✗ parcours « {nom} » a échoué : délai global dépassé "
                       f"(le parcours est borné ; une étape a bloqué)"))
    else:
        print(_c("31", f"✗ parcours « {nom} » a échoué : {type(e).__name__}: {e}"))
    return 1
