#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parcours « recherche web » — chercher, lire, citer.

Une demande de recherche doit montrer l'avancement (« recherche « … » »,
« lecture de N page(s) ») PENDANT le flux, et la réponse doit citer des
sources (des adresses web apparaissent dans la réponse).
"""
import re
import sys
import time

import commun as c

NOM = "recherche web"
DELAIS_S = 480

QUESTION = ("Fais une recherche web sur les récents progrès de l'exploration "
            "de Mars, puis résume en deux phrases et cite tes sources.")


def main() -> int:
    p = c.Parcours(NOM)
    c.borne(DELAIS_S)

    with c.lancer_playwright() as pw:
        b, ctx = c.nouveau_contexte(pw)
        page = ctx.new_page()
        c.connexion(page, p)
        c.ouvrir_playground(page)

        # Références externes AVANT l'envoi : la comparaison après la réponse
        # montre les sources CITÉES, pas un lien du menu.
        liens_avant = page.locator('a[href^="http"]').count()

        # ── 1. La demande de recherche part ─────────────────────────────────
        c.envoyer_message(page, QUESTION)
        limite = time.time() + 60
        while time.time() < limite and not c.streaming(page):
            page.wait_for_timeout(500)
        if not c.streaming(page):
            p.rate("demande de recherche envoyée", "le flux de réponse s'ouvre",
                   "aucune réception en 60 s")
        p.ok("demande de recherche envoyée", "« Fais une recherche web sur… »")

        # ── 2. L'avancement est montré pendant le flux ──────────────────────
        etapes = page.get_by_text(
            re.compile(r"(recherche «|searching for|lecture de \d|reading \d|"
                       r"résultat\(s\)|result\(s\))"))
        vue_etape = None
        limite = time.time() + 180
        while time.time() < limite:
            for i in range(etapes.count()):
                if etapes.nth(i).is_visible():
                    vue_etape = etapes.nth(i).inner_text().strip()
                    break
            if vue_etape:
                break
            page.wait_for_timeout(500)
        if not vue_etape:
            p.rate("avancement de la recherche affiché",
                   "une étape « recherche « … » » / « lecture de N page(s) » "
                   "visible pendant le flux",
                   f"aucune étape visible en 180 s — fil : "
                   f"{page.locator('body').inner_text()[-300:]!r}")
        p.ok("avancement affiché", f"« {vue_etape[:80]} »")

        # ── 3. La réponse cite des sources ──────────────────────────────────
        c.attendre_stream(page, p, delai_s=240, exige_debut=False)
        p.ok("réponse reçue", "flux terminé")

        liens_apres = page.locator('a[href^="http"]').count()
        texte = page.locator("body").inner_text()
        reponse = texte[texte.find(QUESTION) + len(QUESTION):] if QUESTION in texte else texte
        if liens_apres <= liens_avant and "http" not in reponse:
            p.rate("réponse citant des sources",
                   "des adresses web (liens) dans la réponse",
                   f"{liens_apres} lien(s) externe(s) contre {liens_avant} avant "
                   f"l'envoi, et aucune adresse dans le texte — réponse : "
                   f"{reponse.strip()[-300:]!r}")
        p.ok("sources citées",
             f"{liens_apres - liens_avant} nouveau(x) lien(s) externe(s) dans la réponse")

        b.close()

    p.fin()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001
        code = c.fin_parcours_echec(NOM, e)
    sys.exit(code)
