#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parcours « compétences » — les commandes « / » du composeur.

Tape « / » : le menu des compétences s'ouvre. Sélectionne une compétence :
son prompt est inséré dans le composeur (et son prompt système est appliqué).
"""
from config import (WEB, API, DOMAINE, DOMAINE_SSO, RACINE, DEMO_PASS, DEMO_USER,
                    S3_CFG, S3_BUCKET, PYTEST_BIN)
import sys

import commun as c

NOM = "compétences"
DELAIS_S = 120

PROMPT_RESUMER = ("Résume ce texte en 3 points clairs et concis : ",
                  "Summarize this text in 3 clear, concise points: ")


def main() -> int:
    p = c.Parcours(NOM)
    c.borne(DELAIS_S)

    with c.lancer_playwright() as pw:
        b, ctx = c.nouveau_contexte(pw)
        page = ctx.new_page()
        c.connexion(page, p)
        c.ouvrir_playground(page)
        p.ok("playground ouvert", "composeur visible")

        # ── 1. « / » ouvre le menu des compétences ──────────────────────────
        champ = c.composer(page)
        champ.click()
        page.keyboard.type("/", delay=30)
        menu = page.get_by_text(c.alternance("Tapez / pour appeler une compétence",
                                             "Type / to call a skill"))
        try:
            menu.first.wait_for(state="visible", timeout=10000)
        except Exception:
            p.rate("menu des compétences ouvert par « / »",
                   "le menu « Compétences » sous le composeur",
                   "aucun menu affiché après la frappe de « / »")
        # Le menu est prouvé par son aide contextuelle ET par ses entrées
        # (compétences de base listées avec leur commande « /alias »).
        if c.texte_visible(page, "Résumer", "Summarise") is None and \
                page.get_by_text("/summarize").count() == 0:
            p.rate("entrées du menu", "les compétences listées avec leur /alias",
                   "menu ouvert mais aucune compétence listée")
        p.ok("menu des compétences", "ouvert après la frappe de « / », compétences listées")

        # ── 2. Sélectionner une compétence met son prompt au composeur ─────
        # Le nom de la compétence est le libellé ACCESSIBLE de la carte : on
        # clique sur son badge « /summarize » (le clic remonte à la carte).
        cible = page.get_by_text("/summarize", exact=True).first
        try:
            cible.wait_for(state="visible", timeout=10000)
            cible.click()
        except Exception:
            p.rate("compétence « Résumer » sélectionnable",
                   "une ligne « /summarize » dans le menu",
                   "introuvable dans le menu des compétences")
        page.wait_for_timeout(600)
        texte = c.composer(page).inner_text().strip()
        attendu = tuple(a.strip() for a in PROMPT_RESUMER)
        if not any(a in texte for a in attendu):
            p.rate("prompt de la compétence au composeur",
                   f"le composeur pré-rempli avec {attendu[0]!r}",
                   f"composeur = {texte!r}")
        p.ok("prompt inséré", f"« {texte[:60]}… »")

        b.close()

    p.fin()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001
        code = c.fin_parcours_echec(NOM, e)
    sys.exit(code)
