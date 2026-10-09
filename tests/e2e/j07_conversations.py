#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parcours « conversations » — créer, retrouver, renommer, rouvrir.

Une conversation naît d'un message ; elle apparaît dans l'historique ; elle
porte un nom qu'on peut changer ; et en la rouvrant on retrouve l'échange.
"""
from config import (WEB, API, DOMAINE, DOMAINE_SSO, RACINE, DEMO_PASS, DEMO_USER,
                    S3_CFG, S3_BUCKET, PYTEST_BIN)
import re
import sys
import time

import commun as c

NOM = "conversations"
DELAIS_S = 360


def main() -> int:
    p = c.Parcours(NOM)
    c.borne(DELAIS_S)
    marqueur = f"e2e-conversations-{time.strftime('%H%M%S')}"
    question = f"{marqueur} — réponds simplement par le mot « reçu »."

    with c.lancer_playwright() as pw:
        b, ctx = c.nouveau_contexte(pw)
        page = ctx.new_page()
        c.connexion(page, p)
        c.ouvrir_playground(page)

        # ── 1. Une conversation naît d'un message ───────────────────────────
        c.bouton(page, "Nouvelle conversation", "New conversation").first.click()
        page.wait_for_timeout(500)
        c.envoyer_message(page, question)
        c.attendre_stream(page, p, delai_s=240)
        p.ok("conversation créée", f"question « {marqueur} » envoyée et répondue")

        # ── 2. Elle est listée dans l'historique ────────────────────────────
        c.bouton(page, "Historique", "History").first.click()
        dialogue = page.get_by_role("dialog")
        dialogue.last.wait_for(state="visible", timeout=15000)
        # On filtre sur le marqueur : la liste de la démo est longue, et le
        # fil permet de viser la bonne ligne pour la suite.
        recherche = dialogue.last.get_by_placeholder(
            c.alternance("Rechercher une conversation", "Search conversations")).first
        recherche.fill(marqueur)
        page.wait_for_timeout(800)
        corps_dialogue = dialogue.last.inner_text()
        if marqueur not in corps_dialogue:
            p.rate("conversation listée dans l'historique",
                   f"une ligne contenant « {marqueur} »",
                   f"absente du dialogue d'historique — contenu : {corps_dialogue[:200]!r}")
        p.ok("listée dans l'historique", "trouvée par la recherche de l'historique")

        # ── 3. Elle se renomme ─────────────────────────────────────────────
        nouveau_nom = f"Valider e2e {time.strftime('%H%M%S')}"
        c.bouton(dialogue.last, "Renommer", "Rename").first.click()
        page.wait_for_timeout(500)
        champ_nom = dialogue.last.get_by_label(c.alternance("Renommer", "Rename")).first
        try:
            champ_nom.wait_for(state="visible", timeout=10000)
        except Exception:
            p.rate("champ de renommage", "un champ « Renommer » dans la ligne",
                   "aucun champ de saisie apparu après le clic sur « Renommer »")
        champ_nom.fill(nouveau_nom)
        c.bouton(dialogue.last, "Valider", "Confirm").first.click()
        page.wait_for_timeout(1200)
        # Le filtre de recherche porte encore l'ancien marqueur : on cherche
        # maintenant le NOUVEAU nom (sinon la liste reste vide et le contrôle
        # croirait le renommage raté).
        recherche.fill(nouveau_nom)
        page.wait_for_timeout(800)
        corps_dialogue = dialogue.last.inner_text()
        if nouveau_nom not in corps_dialogue:
            p.rate("conversation renommée",
                   f"le nouveau nom « {nouveau_nom} » dans l'historique",
                   f"toujours « {corps_dialogue[:200]!r} »")
        p.ok("conversation renommée", f"« {nouveau_nom} »")

        # ── 4. Elle se rouvre avec son contenu ──────────────────────────────
        dialogue.last.get_by_text(nouveau_nom).first.click()
        page.wait_for_timeout(1500)
        corps_page = page.locator("body").inner_text()
        if marqueur not in corps_page:
            p.rate("réouverture de la conversation",
                   f"le fil rouvert avec la question « {marqueur} »",
                   f"question absente de la page — extrait : {corps_page[-200:]!r}")
        p.ok("conversation rouverte", "le fil retrouve la question posée")

        b.close()

    p.fin()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001
        code = c.fin_parcours_echec(NOM, e)
    sys.exit(code)
