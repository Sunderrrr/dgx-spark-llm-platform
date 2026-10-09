#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parcours « réflexion » — la pensée visible du modèle.

Force la réflexion à « Toujours » dans les réglages du playground, envoie une
question qui fait réfléchir, et exige : le bloc de réflexion apparaît ET se
déploie (le texte de la pensée est lisible).
"""
import re
import sys
import time

import commun as c

NOM = "réflexion"
DELAIS_S = 360

QUESTION = ("Explique étape par étape, en pensant à voix haute, pourquoi le ciel "
            "est bleu le jour et rouge au coucher du soleil.")


def main() -> int:
    p = c.Parcours(NOM)
    c.borne(DELAIS_S)

    with c.lancer_playwright() as pw:
        b, ctx = c.nouveau_contexte(pw)
        page = ctx.new_page()
        c.connexion(page, p)
        c.ouvrir_playground(page)
        p.ok("playground ouvert", "composeur visible")

        # ── 1. Réglages : la réflexion forcée à « Toujours » ────────────────
        c.ouvrir_reglages_playground(page)
        panneau = page.locator(".playground-settings-panel")
        panneau.wait_for(state="visible", timeout=15000)
        p.ok("panneau de réglages ouvert", "« Réglages du playground »")

        toujours = c.texte_visible(panneau, "Toujours", "Always")
        if toujours is None:
            p.rate("option « Toujours » (réflexion forcée)",
                   "un choix « Toujours » dans la section Raisonnement",
                   "introuvable dans le panneau de réglages")
        toujours.click()
        page.wait_for_timeout(400)
        c.bouton(panneau, "Fermer", "Close").first.click()
        p.ok("réflexion forcée", "option « Toujours » sélectionnée")

        # ── 2. Une question qui fait réfléchir ──────────────────────────────
        c.envoyer_message(page, QUESTION)

        # ── 3. Le bloc de réflexion apparaît (en-tête) ──────────────────────
        entete = page.locator(".raison-entete")
        trouve = False
        limite = time.time() + 240
        while time.time() < limite:
            if entete.count() > 0 and entete.first.is_visible():
                trouve = True
                break
            page.wait_for_timeout(500)
        if not trouve:
            p.rate("bloc de réflexion visible",
                   "l'en-tête « Réflexion » au-dessus de la réponse "
                   "(réflexion forcée à « Toujours »)",
                   f"aucun .raison-entete en 240 s — fil : "
                   f"{page.locator('body').inner_text()[-300:]!r}")
        p.ok("bloc de réflexion apparu", "en-tête « Réflexion » présent")

        # ── 4. La réponse finit, puis la pensée reste dépliable ─────────────
        c.attendre_stream(page, p, delai_s=240)
        p.ok("réponse écrite après la réflexion", "flux terminé")

        # Le bloc se replie tout seul à l'écriture : s'il n'est pas ouvert,
        # un clic sur l'en-tête doit déplier la pensée.
        panneau_r = page.locator(".raison-panneau")
        if not (panneau_r.count() and panneau_r.first.is_visible()):
            entete.first.scroll_into_view_if_needed()
            entete.first.click()
            page.wait_for_timeout(800)
        if not (panneau_r.count() and panneau_r.first.is_visible()):
            p.rate("bloc de réflexion déplié",
                   "la bulle de pensée visible après le clic sur l'en-tête",
                   "le panneau .raison-panneau n'apparaît pas")
        pensee = panneau_r.first.inner_text().strip()
        if len(pensee) < 20:
            p.rate("texte de la réflexion", "au moins 20 caractères de pensée",
                   f"{len(pensee)} caractère(s) — {pensee[:120]!r}")
        p.ok("réflexion dépliée", f"{len(pensee)} caractères de pensée lisibles")

        b.close()

    p.fin()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001
        code = c.fin_parcours_echec(NOM, e)
    sys.exit(code)
