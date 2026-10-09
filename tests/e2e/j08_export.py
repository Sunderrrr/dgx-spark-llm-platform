#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parcours « export » — repartir avec la conversation.

Le bouton « Exporter » doit produire un VRAI fichier Markdown : non vide,
titré, avec les tours de parole (le modèle exporté est nommé).
"""
from e2e_config import (WEB, API, DOMAINE, DOMAINE_SSO, RACINE, DEMO_PASS, DEMO_USER,
                    S3_CFG, S3_BUCKET, PYTEST_BIN)
import sys
import time

import commun as c

NOM = "export"
DELAIS_S = 300


def main() -> int:
    p = c.Parcours(NOM)
    c.borne(DELAIS_S)

    with c.lancer_playwright() as pw:
        b, ctx = c.nouveau_contexte(pw)
        page = ctx.new_page()
        c.connexion(page, p)
        c.ouvrir_playground(page)

        # ── 1. Une conversation à exporter ──────────────────────────────────
        question = f"e2e-export-{time.strftime('%H%M%S')} — réponds par le mot « prêt »."
        c.envoyer_message(page, question)
        c.attendre_stream(page, p, delai_s=240)
        p.ok("conversation prête", "question + réponse enregistrées")

        # ── 2. « Exporter » produit un téléchargement ───────────────────────
        try:
            with page.expect_download(timeout=30000) as info:
                c.bouton(page, "Exporter", "Export").first.click()
            telechargement = info.value
        except Exception:
            p.rate("téléchargement de l'export",
                   "un fichier téléchargé après le clic sur « Exporter »",
                   "aucun téléchargement en 30 s")
        chemin = telechargement.path()
        if chemin is None:
            p.rate("fichier exporté", "un fichier sur disque", "aucun chemin de téléchargement")
        with open(chemin, encoding="utf-8") as f:
            contenu = f.read()
        p.ok("fichier exporté", f"{telechargement.suggested_filename!r}, {len(contenu)} caractères")

        # ── 3. C'est du Markdown non vide et structuré ──────────────────────
        problemes = []
        if len(contenu.strip()) < 80:
            problemes.append(f"{len(contenu.strip())} caractère(s)")
        if not contenu.lstrip().startswith("#"):
            problemes.append("pas de titre Markdown « # … »")
        if "Modèle :" not in contenu and "Model:" not in contenu:
            problemes.append("pas de ligne « Modèle : »")
        if not ("Vous :" in contenu or "You:" in contenu):
            problemes.append("pas de tour « Vous : »")
        if not ("Assistant :" in contenu or "Assistant:" in contenu):
            problemes.append("pas de tour « Assistant : »")
        if question[:40] not in contenu:
            problemes.append("la question posée est absente de l'export")
        if problemes:
            p.rate("contenu Markdown de l'export",
                   "un Markdown titré avec les tours de parole et la question",
                   " ; ".join(problemes) + f" — extrait {contenu[:150]!r}")
        p.ok("contenu Markdown", f"{len(contenu.splitlines())} lignes, titres et tours de parole")

        b.close()

    p.fin()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001
        code = c.fin_parcours_echec(NOM, e)
    sys.exit(code)
