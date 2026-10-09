#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parcours « actions des pages » — un vrai geste sur chaque écran.

Là où « la page se charge » ne prouve rien (un SPA vide rend 200), ce parcours
fait CE QU'UN UTILISATEUR FAIT :
  /search   une recherche HuggingFace renvoie des cartes ;
  /ranking  un classement s'affiche (les comptes classés y sont) ;
  /request  le formulaire de demande se soumet — puis la ligne de test est
            EFFACÉE (try/finally) pour ne pas polluer la file d'attente admin ;
  /memory   une préférence se lit et s'écrit (état d'origine rétabli) ;
  /support  un message à l'assistant renvoie une réponse ;
  /image /video /ocr /voice /music : le formulaire est utilisable et l'état du
            service est annoncé honnêtement (disponible / à la demande) —
            pas de génération lourde ici (couverte par « outil image »).
"""
from config import (WEB, API, DOMAINE, DOMAINE_SSO, RACINE, DEMO_PASS, DEMO_USER,
                    S3_CFG, S3_BUCKET, PYTEST_BIN)
import re
import sys
import time

import commun as c

NOM = "actions des pages"
DELAIS_S = 600

MEDIA = [
    ("/image", "Génération d'image", "Image generation"),
    ("/video", "Génération vidéo", "Video generation"),
    ("/ocr", "OCR", "OCR"),
    ("/voice", "Clonage de voix", "Voice cloning"),
    ("/music", "Génération musicale", "Music generation"),
]

ETATS_SERVICE = [
    ("Demander un modèle", "Request a model"),
    ("Aucun modèle", "No model"),
    ("Générer", "Generate"),
    ("indisponible", "unavailable"),
    ("à la demande", "on demand"),
]


def main() -> int:
    p = c.Parcours(NOM)
    c.borne(DELAIS_S)

    with c.lancer_playwright() as pw:
        b, ctx = c.nouveau_contexte(pw)
        page = ctx.new_page()
        c.connexion(page, p)

        # ── 1. /search : une recherche HF renvoie des cartes ────────────────
        page.goto(f"{c.BASE}/search", wait_until="domcontentloaded")
        champ = page.get_by_placeholder(
            c.alternance("Nom de modèle, ex: Qwen, Ornith, Mistral...",
                         "Model name, e.g. Qwen, Ornith, Mistral...")).first
        champ.wait_for(state="visible", timeout=20000)
        champ.fill("Qwen")
        c.bouton(page, "Recherche", "Search").first.click()
        vue_carte = False
        limite = time.time() + 45
        while time.time() < limite:
            corps = page.locator("body").inner_text()
            if "Qwen/" in corps or re.search(r"Qwen[^\s]{0,20}/", corps):
                vue_carte = True
                break
            if c.fragment("Hugging Face ne répond pas",
                          "Hugging Face is not responding").search(corps):
                p.rate("recherche HuggingFace (/search)",
                       "des cartes de résultats pour « Qwen »",
                       "Hugging Face ne répond pas (panne amont) — la page le dit")
            page.wait_for_timeout(700)
        if not vue_carte:
            p.rate("recherche HuggingFace (/search)",
                   "des cartes de résultats (identifiants de dépôts affichés)",
                   f"aucun résultat en 45 s — texte : {page.locator('body').inner_text()[:200]!r}")
        p.ok("recherche HuggingFace", "cartes de résultats affichées pour « Qwen »")

        # ── 2. /ranking : un classement s'affiche ───────────────────────────
        page.goto(f"{c.BASE}/ranking", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        r = ctx.request.get(f"{c.BASE}/api/ranking?period=day&metric=total")
        lignes = (r.json() or {}).get("rows", []) if r.status == 200 else []
        corps = page.locator("body").inner_text()
        if lignes and lignes[0].get("username") in corps:
            p.ok("classement affiché", f"{len(lignes)} compte(s) classé(s), "
                                      f"premier « {lignes[0]['username']} » visible")
        elif not lignes:
            # Aucune consommation sur la période : le classement vide EST un
            # rendu vrai, mais on exige au moins la structure du tableau.
            if c.texte_visible(page, "Rang", "Rank") is None:
                p.rate("classement affiché", "la structure du classement (« Rang »)",
                       "ni comptes classés ni tableau")
            p.avertissement("classement affiché",
                            "période sans consommation : structure du tableau vérifiée")
        else:
            p.rate("classement affiché",
                   f"le premier compte « {lignes[0].get('username')} » dans le tableau",
                   f"tableau non aligné sur /api/ranking — texte : {corps[:200]!r}")

        # ── 3. /request : le formulaire se soumet (puis nettoyage) ─────────
        identifiant = f"e2e-valider/test-{time.strftime('%H%M%S')}"
        page.goto(f"{c.BASE}/request", wait_until="domcontentloaded")
        champ = page.get_by_label(
            c.alternance("Identifiant HuggingFace *", "HuggingFace identifier *")).first
        champ.wait_for(state="visible", timeout=20000)
        champ.fill(identifiant)
        try:
            c.bouton(page, "Envoyer la demande", "Send request").first.click()
            limite = time.time() + 20
            envoyee = False
            while time.time() < limite:
                corps = page.locator("body").inner_text()
                if c.fragment("Demande envoyée", "Request sent").search(corps):
                    envoyee = True
                    break
                page.wait_for_timeout(500)
            if not envoyee:
                p.rate("formulaire de demande (/request)",
                       "« Demande envoyée ! » après l'envoi",
                       f"aucun retour de succès — texte : {corps[:200]!r}")
            p.ok("demande de modèle soumise", f"« {identifiant} » acceptée")
        finally:
            # La ligne de test ne reste PAS dans la file d'attente admin.
            nettoyage = c.sql_portail(
                f"DELETE FROM model_requests WHERE model_id='{identifiant}'")
            p.ok("file admin non polluée",
                 f"ligne de demande de test supprimée ({nettoyage})")

        # ── 4. /memory : une préférence se lit et s'écrit ──────────────────
        page.goto(f"{c.BASE}/memory", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        r = ctx.request.get(f"{c.BASE}/api/memory")
        avant = (r.json() or {}).get("enabled") if r.status == 200 else None
        if avant is None:
            p.rate("préférence mémoire lue (/api/memory)",
                   "l'état {enabled} de la préférence",
                   f"HTTP {r.status} — lecture impossible")
        p.ok("préférence mémoire lue", f"enabled={bool(avant)}")
        cible = not bool(avant)
        # Le libellé n'est pas un <label> cliquable : c'est l'interrupteur lui
        # même (role="switch") qui bascule la préférence.
        interrupteur = page.get_by_role("switch").first
        try:
            interrupteur.wait_for(state="visible", timeout=15000)
            interrupteur.click()
        except Exception:
            p.rate("interrupteur « Activer la mémoire »",
                   "le sélecteur de préférence visible (onglet Mémoire)",
                   "introuvable sur la page /memory (redirigée)")
        page.wait_for_timeout(1500)
        r = ctx.request.get(f"{c.BASE}/api/memory")
        apres = (r.json() or {}).get("enabled") if r.status == 200 else None
        if bool(apres) != cible:
            p.rate("préférence mémoire écrite",
                   f"enabled={cible} après le clic (étape écrite par la page)",
                   f"enabled={bool(apres)} — le clic n'a pas été pris en compte")
        p.ok("préférence mémoire écrite", f"enabled={bool(avant)} → {bool(apres)}")
        # Retour à l'état d'origine : un parcours ne laisse aucun réglage.
        interrupteur.click()
        page.wait_for_timeout(1200)
        r = ctx.request.get(f"{c.BASE}/api/memory")
        restaure = (r.json() or {}).get("enabled") if r.status == 200 else None
        if bool(restaure) != bool(avant):
            p.rate("préférence mémoire rétablie",
                   f"enabled={bool(avant)} (état d'origine)",
                   f"enabled={bool(restaure)}")
        p.ok("préférence rétablie", f"enabled={bool(restaure)}")

        # ── 5. /support : un message à l'assistant renvoie une réponse ─────
        page.goto(f"{c.BASE}/support", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        champ = c.composer(page)
        champ.wait_for(state="visible", timeout=20000)
        champ.click()
        page.keyboard.type("Réponds uniquement par le mot « bonjour ».", delay=5)
        page.keyboard.press("Enter")
        # La question contient déjà « bonjour » : la RÉPONSE est la seconde
        # occurrence du mot (un autre texte « Pas de réponse » = échec).
        repondu = False
        limite = time.time() + 240
        while time.time() < limite:
            corps = page.locator("body").inner_text()
            if c.fragment("Pas de réponse", "No response").search(corps):
                break  # le Support a explicitement renvoyé « pas de réponse »
            if corps.lower().count("bonjour") >= 2:
                repondu = True
                break
            page.wait_for_timeout(1000)
        if not repondu:
            p.rate("réponse de l'assistant Support",
                   "une réponse à « Réponds uniquement par le mot « bonjour » » "
                   "(le mot apparaît une seconde fois, dans la réponse)",
                   f"aucune réponse en 240 s — texte : "
                   f"{page.locator('body').inner_text()[-250:]!r}")
        p.ok("assistant Support répond", "réponse reçue")

        # ── 6. Pages média : formulaire utilisable + état annoncé ───────────
        for chemin, mfr, men in MEDIA:
            page.goto(f"{c.BASE}{chemin}", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            corps = page.locator("body").inner_text()
            etat = next((f"{fr}/{en}" for fr, en in ETATS_SERVICE
                         if c.fragment(fr, en).search(corps)), None)
            if etat is None:
                p.rate(f"état du service annoncé ({chemin})",
                       "un état honnête : disponible (« Générer ») ou à la "
                       "demande (« Demander un modèle » / « Aucun modèle »)",
                       f"aucune annonce d'état — texte : {corps[:200]!r}")
            # Le formulaire répond : s'il y a une zone de saisie, on y tape.
            champs = page.get_by_role("textbox")
            utilisable = 0
            for i in range(min(champs.count(), 4)):
                ch = champs.nth(i)
                if ch.is_visible():
                    try:
                        ch.click()
                        page.keyboard.type("e2e", delay=5)
                        utilisable += 1
                        break
                    except Exception:
                        pass
            if champs.count() and not utilisable:
                p.rate(f"formulaire utilisable ({chemin})",
                       "la zone de saisie accepte le texte",
                       "aucune zone de saisie interactive")
            p.ok(f"page {chemin}",
                 f"état annoncé « {etat} », formulaire répondant")

        b.close()

    p.fin()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001
        code = c.fin_parcours_echec(NOM, e)
    sys.exit(code)
