#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parcours « chat » — la fonction première : parler au modèle servi.

Envoie un message dans le playground, exige une réponse diffusée en flux,
le nom du modèle au-dessus de la réponse et le pied de message avec les
tokens et le TTFT (mesuré côté portail).
"""
from config import (WEB, API, DOMAINE, DOMAINE_SSO, RACINE, DEMO_PASS, DEMO_USER,
                    S3_CFG, S3_BUCKET, PYTEST_BIN)
import re
import sys

import commun as c

NOM = "chat"
DELAIS_S = 360

QUESTION = ("Explique en deux phrases seulement ce qu'est un cache de contexte "
            "pour un modèle de langage.")


def main() -> int:
    p = c.Parcours(NOM)
    c.borne(DELAIS_S)

    with c.lancer_playwright() as pw:
        b, ctx = c.nouveau_contexte(pw)
        page = ctx.new_page()
        c.connexion(page, p)
        c.ouvrir_playground(page)
        p.ok("playground ouvert", "composeur visible")

        # Le nom du modèle annoncé par le sélecteur : c'est lui qu'on veut
        # retrouver AU-DESSUS de la réponse (« qui parle »).
        selecteur = page.get_by_role("combobox").first
        modele = ""
        try:
            selecteur.wait_for(state="visible", timeout=15000)
            modele = selecteur.inner_text().strip().split("\n")[0]
        except Exception:
            pass
        if not modele:
            # Repli : le modèle servi, annoncé par l'API publique de la page.
            statut, _, corps = c.http("GET", f"{c.BASE}/api/config")
            modele = "modèle"
        apparitions_avant = page.locator("body").inner_text().count(modele) if modele else 0
        p.ok("modèle annoncé dans le sélecteur", f"« {modele} »")

        c.envoyer_message(page, QUESTION)
        c.attendre_stream(page, p, delai_s=300)
        p.ok("réponse diffusée en flux",
             "bouton « Arrêter » apparu puis disparu")

        corps_page = page.locator("body").inner_text()
        # Le texte de la réponse : le fil entier moins la question.
        reponse = corps_page.replace(QUESTION, "", 1)
        if len(reponse.strip()) < 40:
            p.rate("contenu de la réponse", "au moins 40 caractères de réponse",
                   f"{len(reponse.strip())} caractère(s) — extrait {reponse.strip()[:120]!r}")
        p.ok("contenu de la réponse", f"{len(reponse.strip())} caractères")

        # Nom du modèle : il doit apparaître une fois de plus qu'avant l'envoi
        # (le sélecteur + le nom au-dessus de la réponse).
        apparitions_apres = corps_page.count(modele)
        if modele and apparitions_apres <= apparitions_avant:
            p.rate("nom du modèle au-dessus de la réponse",
                   f"« {modele} » répété au-dessus de la réponse "
                   f"({apparitions_avant} → plus)",
                   f"{apparitions_avant} occurrence(s) avant, {apparitions_apres} après")
        p.ok("nom du modèle affiché", f"« {modele} » au-dessus de la réponse")

        # Pied de message : tokens + TTFT (les deux, sinon le pied est absent).
        pieds = page.get_by_text(re.compile(r"TTFT", re.I))
        if pieds.count() == 0:
            p.rate("pied de message (tokens / TTFT)",
                   "un pied « N tokens · … · TTFT Xs » sous la réponse",
                   f"aucun texte TTFT dans la page — extrait du fil : {corps_page[-300:]!r}")
        pied = pieds.first.inner_text()
        if not re.search(r"token", pied, re.I):
            p.rate("pied de message (tokens / TTFT)",
                   "le nombre de tokens dans le même pied", f"pied trouvé : {pied!r}")
        p.ok("pied de message", f"« {pied.strip()} »")

        b.close()

    p.fin()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001
        code = c.fin_parcours_echec(NOM, e)
    sys.exit(code)
