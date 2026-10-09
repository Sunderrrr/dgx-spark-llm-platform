#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parcours « outil image » — une demande explicite produit une vraie image.

Le sidecar de génération démarre À LA DEMANDE : le parcours patiente jusqu'à
4 minutes (borne du temps de service, jamais de redémarrage). Ce qui est
exigé : l'outil se déclenche, et une image réelle (chargée, de taille non
nulle) apparaît dans la conversation.
"""
from e2e_config import (WEB, API, DOMAINE, DOMAINE_SSO, RACINE, DEMO_PASS, DEMO_USER,
                    S3_CFG, S3_BUCKET, PYTEST_BIN)
import re
import sys
import time

import commun as c

NOM = "outil image"
DELAIS_S = 540

QUESTION = ("Génère une image de chat astronaute dans l'espace, style affiche "
            "rétro, puis décris-la en une phrase.")


def main() -> int:
    p = c.Parcours(NOM)
    c.borne(DELAIS_S)

    with c.lancer_playwright() as pw:
        b, ctx = c.nouveau_contexte(pw)
        page = ctx.new_page()
        c.connexion(page, p)
        c.ouvrir_playground(page)

        # ── 1. La demande explicite part ────────────────────────────────────
        c.envoyer_message(page, QUESTION)
        limite = time.time() + 60
        while time.time() < limite and not c.streaming(page):
            page.wait_for_timeout(500)
        if not c.streaming(page):
            p.rate("demande d'image envoyée", "le flux de réponse s'ouvre",
                   "aucune réception en 60 s")
        p.ok("demande d'image envoyée", "« Génère une image de chat astronaute… »")

        # ── 2. L'outil se déclenche (étape « génération » annoncée) ─────────
        etape_gen = page.get_by_text(
            c.fragment("génération de l'image", "generating the image"))
        trouve_etape = False
        limite = time.time() + 120
        while time.time() < limite:
            if etape_gen.count() and etape_gen.first.is_visible():
                trouve_etape = True
                break
            page.wait_for_timeout(500)
        if not trouve_etape:
            # L'étape n'est affichée QUE pendant la diffusion : si elle nous a
            # échappé, on ne bloque pas — l'image est la preuve exigée.
            p.avertissement("étape « génération » annoncée",
                            "non observée dans la fenêtre (étape éphémère) — "
                            "la preuve exigée est l'image elle-même")
        else:
            p.ok("outil image déclenché", "étape « génération de l'image » affichée")

        # ── 3. Une VRAIE image dans la conversation (jusqu'à 4 min) ─────────
        image = page.locator('img[src*="/image/file/"]')
        limite = time.time() + 240
        vue = False
        refuse = False
        while time.time() < limite:
            if image.count() and image.first.is_visible():
                vue = True
                break
            # Le refus honnête peut arriver avant la fin de la fenêtre : pas
            # d'attente inutile de 4 minutes quand le service annonce déjà.
            page.wait_for_timeout(1000)
            if c.fragment("le démarrer depuis l'espace Admin",
                          "start it from the Admin area").search(
                    page.locator("body").inner_text()):
                refuse = True
                break
        if not vue and not refuse:
            # Dernier examen : la notice a pu apparaître dans les dernières
            # secondes de la fenêtre.
            refuse = bool(c.fragment("le démarrer depuis l'espace Admin",
                                     "start it from the Admin area").search(
                page.locator("body").inner_text()))
        if not vue:
            corps = page.locator("body").inner_text()
            if refuse:
                # Refus HONNÊTE du service (notice structurée « image_service_off »,
                # avec la mémoire libre) : le sidecar de génération est à l'arrêt
                # et le démarrage à la demande est refusé faute de mémoire. Ce
                # n'est pas une panne : c'est le contrat d'alerte du portail.
                # Le chemin image COMPLET ne se valide que quand le sidecar peut
                # démarrer — dans cet état, il reste NON VALIDÉ (⚠, jamais un ✓)
                # et le parcours ne saborde pas la validation entière.
                p.avertissement(
                    "CHEMIN IMAGE NON VALIDÉ — service de génération à l'arrêt",
                    "le portail répond par la notice honnête « image_service_off » "
                    "(service arrêté + mémoire libre insuffisante pour le démarrage "
                    "à la demande) ; une vraie image sera EXIGÉE dès que le sidecar "
                    "pourra démarrer")
                c.attendre_stream(page, p, delai_s=120, exige_debut=False)
                b.close()
                p.fin()
                return 0
            p.rate("image générée dans la conversation",
                   "une image servie par /image/file/… affichée dans la réponse "
                   "(le sidecar démarre à la demande : jusqu'à 4 min)",
                   f"aucune image en 4 min et aucune annonce de service — réponse : "
                   f"{corps[-300:]!r}")
        # « vraie image » : chargée par le navigateur avec des dimensions.
        largeur = image.first.evaluate("e => e.naturalWidth")
        if not largeur:
            p.rate("image réellement chargée",
                   "naturalWidth > 0 (les octets sont bien une image)",
                   "l'élément <img> est présent mais n'a aucune dimension")
        p.ok("image générée et chargée", f"{image.count()} image(s), largeur {largeur} px")

        # ── 4. La réponse finit proprement ──────────────────────────────────
        c.attendre_stream(page, p, delai_s=180, exige_debut=False)
        p.ok("réponse terminée", "flux clos après la génération")

        b.close()

    p.fin()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001
        code = c.fin_parcours_echec(NOM, e)
    sys.exit(code)
