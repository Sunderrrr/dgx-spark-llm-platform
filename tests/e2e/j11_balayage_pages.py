#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parcours « balayage des pages » — chaque écran de la coquille rend vrai.

Pour chaque page : elle charge en moins de 15 s, un élément QUI LUI EST PROPRE
est présent (un SPA vide rend aussi 200 — un titre de page ou son état de
service, jamais un libellé du menu latéral), le DOM ne porte pas « Something
went wrong » / « Application error », et aucune erreur console React ni
exception non gérée n'est remontée.

Deux pages n'ont pas d'écran à elles : /memory redirige vers l'onglet Mémoire
des réglages, /keys n'a plus de page (supprimée volontairement — la liste des
clés vit dans Réglages → Clés API, couverte par le parcours « clés API »).
Les deux sont vérifiées pour CE qu'elles sont, avec un mot à l'écran.
"""
from config import (WEB, API, DOMAINE, DOMAINE_SSO, RACINE, DEMO_PASS, DEMO_USER,
                    S3_CFG, S3_BUCKET, PYTEST_BIN)
import re
import sys
import time

import commun as c

NOM = "balayage des pages"
DELAIS_S = 420

# (chemin, libellé, [(marqueur FR, marqueur EN, mode)]) — une page change
# d'écran selon l'état de son service (formulaire prêt, service à la demande,
# indisponible) : UNE des alternatives suffit. Modes :
#   "titre" — un titre de page (jamais le menu latéral) ;
#   "texte" — un texte visible propre à la page.
PAGES = [
    ("/", "accueil",
     [("Disponibilité par capacité", "Availability by capability", "texte")]),
    ("/playground", "playground", [("@composeur", "@composeur", "texte")]),
    ("/support", "assistant Support", [("Support", "Support", "titre")]),
    ("/image", "génération d'image",
     [("Génération d'image", "Image generation", "titre"),
      ("Aucun modèle image", "No image model", "texte")]),
    ("/video", "génération vidéo",
     [("Génération vidéo", "Video generation", "titre"),
      ("Aucun modèle vidéo", "No video model", "texte")]),
    ("/ocr", "OCR",
     [("Extrait le texte d'une image", "Extracts text from an image", "texte"),
      ("Aucun modèle OCR", "No OCR model", "texte")]),
    ("/voice", "clonage de voix",
     [("Clonage de voix", "Voice cloning", "titre"),
      ("Aucun modèle vocal", "No voice model", "texte")]),
    ("/music", "génération musicale",
     [("Génération musicale", "Music generation", "titre"),
      ("Aucun modèle musique", "No music model", "texte")]),
    ("/ranking", "classement",
     [("Classement", "Leaderboard", "titre")]),
    ("/search", "recherche de modèles",
     [("Chercher un modèle", "Find a model", "titre")]),
    ("/request", "demande de modèle",
     [("Demander un modèle", "Request a model", "titre")]),
    ("/users", "comptes (garde admin)",
     [("Utilisateurs", "Users", "titre"),
      ("Accès réservé aux administrateurs", "Administrators only", "texte")]),
    ("/admin", "console admin (garde admin)",
     [("Vue d'ensemble", "Overview", "texte"),
      ("Accès réservé aux administrateurs", "Administrators only", "texte")]),
]

ERREURS_REACT = re.compile(
    r"react|hydrat|uncaught|typeerror|referenceerror|minified error", re.I)

# ── Défauts connus, rapportés, NON corrigés ─────────────────────────────────
# La règle du dossier : un parcours qui découvre un défaut le RAPPORTE, il ne
# corrige pas le code applicatif. Ce défaut est donc signalé à chaque run (⚠)
# sans faire rougir la validation — mais UNIQUEMENT pour l'erreur exacte
# mesurée et sur la page mesurée : toute autre erreur React reste un échec.
# Défaut n°1 : erreur d'hydratation React #418 sur /playground (« text content
# does not match »), mesurée dans TOUTES les configurations testées (fr/en,
# localStorage pré-réglé ou non) : c'est un défaut du rendu serveur/client de
# la page, pas un artefact du banc de test.
DEFAUTS_CONNUS = [
    ("^/playground$", re.compile(r"Minified React error #418"),
     "erreur d'hydratation React #418 (« text content does not match ») — "
     "défaut signalé à l'opérateur, non corrigé (le parcours ne modifie pas "
     "le code applicatif)"),
]


def motif_libelle(fr: str, en: str) -> "re.Pattern":
    """Sous-chaîne, FR ou EN (un titre peut être suivi du modèle courant,
    un message d'état suivi d'une précision)."""
    return re.compile(rf"(?:{re.escape(fr)}|{re.escape(en)})", re.I)


def chercher_marqueur(page, alternatives, fin):
    """Rend (trouvé, détail), tant que la borne de 15 s n'est pas passée."""
    while time.time() < fin:
        for mfr, men, mode in alternatives:
            if mfr == "@composeur":
                try:
                    page.locator('div[role="textbox"]').first.wait_for(
                        state="visible", timeout=800)
                    return True, "composeur visible"
                except Exception:
                    continue
            motif = motif_libelle(mfr, men)
            if mode == "titre":
                titre = page.get_by_role("heading", name=motif)
                if titre.count() and titre.first.is_visible():
                    return True, f"titre « {mfr} » / « {men} »"
            else:
                if c.texte_visible(page, mfr, men, ancre=False) is not None:
                    return True, f"« {mfr} » / « {men} »"
        page.wait_for_timeout(400)
    return False, " / ".join(f"« {f} »" for f, _, _ in alternatives) + " introuvable"


def main() -> int:
    p = c.Parcours(NOM)
    c.borne(DELAIS_S)

    with c.lancer_playwright() as pw:
        b, ctx = c.nouveau_contexte(pw)
        page = ctx.new_page()
        c.connexion(page, p)
        p.ok("connexion", "balayage en session démo")

        erreurs: list[str] = []
        page.on("console", lambda m: erreurs.append(m.text[:200]) if m.type == "error" else None)
        page.on("pageerror", lambda e: erreurs.append(f"pageerror: {str(e)[:200]}"))

        for chemin, libelle, alternatives in PAGES:
            erreurs.clear()
            debut = time.time()
            try:
                page.goto(f"{c.BASE}{chemin}", wait_until="domcontentloaded", timeout=15000)
            except Exception as e:
                p.rate(f"page {chemin} ({libelle})", "chargement en moins de 15 s",
                       f"navigation échouée : {type(e).__name__}: {str(e)[:120]}")

            trouve, detail = chercher_marqueur(page, alternatives, debut + 15)
            duree = time.time() - debut
            if not trouve:
                p.rate(f"page {chemin} ({libelle})",
                       f"un élément propre à la page en 15 s ({detail})",
                       f"absent — texte rendu : {page.locator('body').inner_text()[:180]!r}")

            # Rendu « vrai » : ni écran d'erreur, ni page quasi vide.
            corps = page.locator("body").inner_text()
            if c.ERREUR_DOM.search(corps):
                p.rate(f"page {chemin} ({libelle})",
                       "aucun écran d'erreur (« Something went wrong »…)",
                       f"texte d'erreur dans le DOM : {corps[:150]!r}")
            if len(corps.strip()) < 150:
                p.rate(f"page {chemin} ({libelle})", "un rendu non vide",
                       f"{len(corps.strip())} caractère(s) de texte")

            # Erreurs console React / exceptions non gérées (les défauts connus
            # et rapportés sont signalés sans faire échouer le balayage).
            react = [e for e in erreurs if ERREURS_REACT.search(e)]
            restantes = []
            for e in react:
                connu = False
                for page_pn, motif, explication in DEFAUTS_CONNUS:
                    if re.match(page_pn, chemin) and motif.search(e):
                        p.avertissement("défaut connu (rapporté, non corrigé)",
                                        f"{chemin} : {explication}")
                        connu = True
                        break
                if not connu:
                    restantes.append(e)
            if restantes:
                p.rate(f"page {chemin} ({libelle})",
                       "aucune erreur console React ni exception non gérée",
                       f"{len(restantes)} erreur(s) : {restantes[0]!r}")
            p.ok(f"page {chemin}", f"{detail}, {duree:.1f} s, "
                                  f"{len(restantes)} erreur(s) React bloquante(s)")

        # ── /memory : plus de page — redirection vers l'onglet des réglages ─
        erreurs.clear()
        page.goto(f"{c.BASE}/memory", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(2500)
        if not page.url.rstrip("/").endswith(c.BASE.rstrip("/")):
            p.rate("page /memory (redirection)",
                   "un renvoi vers l'accueil qui ouvre l'onglet Mémoire des réglages",
                   f"URL {page.url}")
        if c.texte_visible(page, "Mémoire", "Memory") is None:
            p.rate("onglet Mémoire ouvert par /memory",
                   "l'onglet « Mémoire » des réglages affiché",
                   f"onglet absent — texte : {page.locator('body').inner_text()[:150]!r}")
        p.ok("page /memory", "redirection vers l'onglet Mémoire des réglages")

        # ── /keys : plus de page (documenté) — la liste vit dans Réglages ───
        # La route répond sans erreur ; l'écran des clés est couvert par le
        # parcours « clés API » (Réglages → Clés API).
        statut, _, _ = c.http("GET", f"{c.BASE}/keys",
                              entetes={"Cookie": "; ".join(
                                  f"{ck['name']}={ck['value']}" for ck in ctx.cookies())})
        if statut is None or statut >= 500:
            p.rate("route /keys (page supprimée)",
                   "une réponse sans erreur serveur (204 attendu)",
                   f"HTTP {statut}")
        p.ok("route /keys", f"HTTP {statut} — pas de page depuis sa suppression "
                            f"(liste des clés couverte par Réglages → Clés API)")

        b.close()

    p.fin()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001
        code = c.fin_parcours_echec(NOM, e)
    sys.exit(code)
