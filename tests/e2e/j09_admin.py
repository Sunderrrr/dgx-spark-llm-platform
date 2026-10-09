#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parcours « admin » — la console d'administration, lue en vrai.

Nécessite `is_admin` : le flag est POSÉ puis RETIRÉ sur le compte démo
(`local_users.is_admin`), toujours en try/finally — un parcours ne laisse
jamais un droit derrière lui. Motif : la page Admin n'est pas testable autrement.

Couvre : les cinq onglets ; le lien profond ?tab=models ; le filtre du
catalogue qui restreint vraiment la liste ; la page /users avec la liste des
comptes. Lecture SEULEMENT : aucun lancement, aucune suppression, aucun
redémarrage — le parcours ne pilote jamais le modèle servi.
"""
from e2e_config import (WEB, API, DOMAINE, DOMAINE_SSO, RACINE, DEMO_PASS, DEMO_USER,
                    S3_CFG, S3_BUCKET, PYTEST_BIN)
import re
import sys

import commun as c

NOM = "admin"
DELAIS_S = 300

ONGLETS = [
    ("overview", "Vue d'ensemble", "Overview", "Mode maintenance", "Maintenance mode"),
    ("models", "Modèles", "Models", "Filtrer le catalogue", "Filter the catalog"),
    ("users", "Utilisateurs", "Users", "Groupes", "Groups"),
    ("requests", "Demandes", "Requests", "Budget actuel", "Current budget"),
    ("system", "Système", "System", "Publier une annonce", "Publish an announcement"),
]


def basculer_admin(valeur: int) -> str:
    c.sql_portail(f"UPDATE local_users SET is_admin={valeur} WHERE username='demo'")
    return c.sql_portail("SELECT is_admin FROM local_users WHERE username='demo'")


def main() -> int:
    p = c.Parcours(NOM)
    c.borne(DELAIS_S)

    # ── 1. Droit temporaire (TOUJOURS retiré plus bas) ─────────────────────
    etat = basculer_admin(1)
    if "1" not in etat:
        p.rate("activation temporaire de is_admin", "is_admin=1 pour le compte démo",
               f"lecture retour : {etat}")
    p.ok("droit admin temporaire", "local_users.is_admin=1 pour le compte démo")

    try:
        with c.lancer_playwright() as pw:
            b, ctx = c.nouveau_contexte(pw)
            page = ctx.new_page()
            c.connexion(page, p)

            # ── 2. /admin se charge pour un admin ───────────────────────────
            page.goto(f"{c.BASE}/admin", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            corps = page.locator("body").inner_text()
            if c.alternance("Accès réservé aux administrateurs",
                            "Administrators only").search(corps):
                p.rate("accès à /admin", "la console pour un compte admin",
                       "« Accès réservé aux administrateurs » — le flag is_admin "
                       "n'a pas été lu (session ou garde de rôle)")
            if c.texte_visible(page, "Vue d'ensemble", "Overview") is None:
                p.rate("console admin rendue", "les onglets de la console",
                       f"aucun onglet — extrait : {corps[:200]!r}")
            p.ok("/admin rendu", "console d'administration affichée")

            # ── 3. Les cinq onglets se rendent ──────────────────────────────
            # Les onglets portent data-tab-value : sans ce filtre, le libellé
            # « Utilisateurs » vise AUSSI l'entrée du menu latéral, qui mène à
            # la page /users (piège mesuré — le clic quittait la console).
            for valeur, fr, en, mfr, men in ONGLETS:
                onglet = page.locator(f'button[data-tab-value="{valeur}"]')
                if not onglet.count():
                    p.rate(f"onglet « {fr} »", "un onglet cliquable",
                           f"aucun bouton data-tab-value={valeur}")
                onglet.first.click()
                page.wait_for_timeout(1200)
                if c.texte_visible(page, mfr, men) is None:
                    p.rate(f"contenu de l'onglet « {fr} »",
                           f"un élément propre à l'onglet (« {mfr} » / « {men} »)",
                           f"contenu absent — texte : {page.locator('body').inner_text()[-200:]!r}")
                p.ok(f"onglet « {fr} »", f"« {mfr} » rendu")

            # ── 4. Le lien profond ?tab=models atterrit sur Modèles ─────────
            page.goto(f"{c.BASE}/admin?tab=models", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            if "tab=models" not in page.url:
                p.rate("lien profond ?tab=models", "l'URL conserve tab=models",
                       f"URL {page.url}")
            actif = page.locator('button[data-tab-value="models"]').first
            if not actif.count() or actif.get_attribute("aria-current") != "page":
                p.rate("lien profond ?tab=models",
                       "l'onglet Modèles marqué actif (aria-current=page)",
                       "l'onglet Modèles n'est pas l'onglet actif")
            if c.texte_visible(page, "Filtrer le catalogue", "Filter the catalog") is None:
                p.rate("lien profond ?tab=models",
                       "l'onglet Modèles actif (« Filtrer le catalogue » visible)",
                       "l'onglet Modèles ne s'est pas ouvert depuis le lien")
            p.ok("lien profond ?tab=models", "onglet Modèles ouvert et actif")

            # ── 5. Le filtre du catalogue restreint la liste ────────────────
            # Session navigateur : l'API admin est protégée, on repasse par le
            # contexte authentifié.
            r = ctx.request.get(f"{c.BASE}/api/admin")
            data = r.json() if r.status == 200 else None
            noms = [m.get("name", "") for m in (data or {}).get("model_cfgs", []) if m.get("name")]
            if len(noms) < 2:
                p.rate("catalogue du modèle", "au moins 2 entrées au catalogue",
                       f"{len(noms)} entrée(s) — le filtre ne peut pas se vérifier")
            lance = page.get_by_role("button", name=c.alternance("Lancer", "Launch"))
            avant = lance.count()
            filtre = page.get_by_label(c.alternance("Filtrer le catalogue",
                                                    "Filter the catalog")).first
            filtre.fill(noms[0])
            page.wait_for_timeout(900)
            apres = page.get_by_role("button", name=c.alternance("Lancer", "Launch")).count()
            if not (0 < apres < avant):
                p.rate("filtre du catalogue",
                       f"la liste restreinte (1 entrée pour « {noms[0]} », "
                       f"sur {avant})",
                       f"{apres} entrée(s) visible(s) pour « {noms[0]} » "
                       f"(avant le filtre : {avant})")
            p.ok("filtre du catalogue",
                 f"« {noms[0]} » : {avant} → {apres} entrée(s) affichée(s)")

            # ── 6. La page /users liste les comptes ─────────────────────────
            page.goto(f"{c.BASE}/users", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            corps = page.locator("body").inner_text()
            if "demo" not in corps:
                p.rate("liste des comptes (/users)",
                       "le compte « demo » dans la liste des comptes",
                       f"liste absente ou incomplète — extrait : {corps[-300:]!r}")
            if c.texte_visible(page, "Utilisateurs", "Users") is None:
                p.rate("page /users", "le titre « Utilisateurs »",
                       "titre absent")
            p.ok("liste des comptes", "le compte démo figure dans la liste")

            b.close()
    finally:
        # ── 7. Le droit est TOUJOURS retiré ─────────────────────────────────
        etat = basculer_admin(0)
        if "0" not in etat:
            print(c._c("31", f"✗ nettoyage : is_admin n'a pas été retiré ({etat}) — "
                             f"corrige à la main : UPDATE local_users SET is_admin=0 "
                             f"WHERE username='demo'"))
            raise SystemExit(1)
        p.ok("droit admin retiré", "local_users.is_admin=0 (nettoyage)")

    p.fin()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001
        code = c.fin_parcours_echec(NOM, e)
    sys.exit(code)
