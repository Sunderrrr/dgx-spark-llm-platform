#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parcours « connexion » — la porte d'entrée du service, jusqu'au bout.

Couvre : la page de connexion et le couple identifiants du compte démo ;
l'identité de l'instance dans l'en-tête ; une session invalide qui renvoie au
login (pas d'écran blanc, pas de 500) ; la déconnexion qui efface vraiment la
session ; et la PRÉSENCE du parcours SSO (le bouton + la redirection OIDC vers
le fournisseur — traverser Authentik est impossible en automatisé : 2FA/LDAP,
c'est le contrat testable ici).
"""
import re
import sys

import commun as c

NOM = "connexion"
DELAIS_S = 180


def main() -> int:
    p = c.Parcours(NOM)
    c.borne(DELAIS_S)

    # ── 1. La configuration publique nomme l'instance et annonce le SSO ─────
    statut, _, corps = c.http("GET", f"{c.BASE}/api/config")
    cfg = c.corps_json(statut, corps)
    if statut != 200 or not cfg:
        p.rate("configuration publique (/api/config)",
               "200 avec {branding, oidc_enabled}", f"HTTP {statut} — {corps[:120]!r}")
    nom_instance = (cfg.get("branding") or {}).get("name") or ""
    oidc = bool(cfg.get("oidc_enabled"))
    p.ok("configuration publique", f"instance « {nom_instance} », oidc_enabled={oidc}")

    with c.lancer_playwright() as pw:
        # ── 2. La page de connexion se charge et accepte le compte démo ─────
        b, ctx = c.nouveau_contexte(pw)
        page = ctx.new_page()
        page.goto(f"{c.BASE}/login", wait_until="domcontentloaded")
        page.locator('input[type="text"]').first.wait_for(state="visible", timeout=20000)
        if not page.locator('input[type="password"]').first.is_visible():
            p.rate("formulaire de connexion", "un champ mot de passe visible",
                   "aucun champ mot de passe")
        p.ok("page de connexion", "identifiant + mot de passe affichés")

        c.connexion(page, p)
        if "/login" in page.url:
            p.rate("identification", "redirection vers l'accueil", f"URL {page.url}")
        p.ok("identification du compte démo", f"atterrie à {page.url}")

        # ── 3. L'en-tête porte le nom de l'instance ─────────────────────────
        page.wait_for_timeout(1200)
        corps_page = page.locator("body").inner_text()
        if nom_instance and nom_instance not in corps_page:
            p.rate("identité de l'instance dans l'en-tête",
                   f"le nom « {nom_instance} » rendu par la coquille",
                   f"absent du texte de la page (extrait : {corps_page[:120]!r})")
        p.ok("identité de l'instance", f"« {nom_instance} » visible dans l'en-tête")

        # ── 4. Déconnexion : la session est bien effacée ─────────────────────
        c.bouton(page, "Déconnexion", "Log out").first.click()
        page.wait_for_url(lambda u: "/login" in u, timeout=20000)
        p.ok("déconnexion", "retour sur la page de connexion")
        page.goto(f"{c.BASE}/", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        if "/login" not in page.url:
            p.rate("session effacée après déconnexion",
                   "une page protégée renvoie à /login", f"URL {page.url}")
        p.ok("session effacée", "une page protégée renvoie au login")
        b.close()

        # ── 5. Session invalide : on revient au login, sans écran blanc ─────
        b2, ctx2 = c.nouveau_contexte(pw)
        ctx2.add_cookies([{"name": "session", "value": "jeton.invalide.e2e",
                           "domain": "dgx.cronos.website", "path": "/"}])
        page2 = ctx2.new_page()
        page2.goto(f"{c.BASE}/playground", wait_until="domcontentloaded")
        page2.wait_for_timeout(2500)
        if "/login" not in page2.url:
            p.rate("session invalide renvoyée au login",
                   "atterrissage sur /login", f"URL {page2.url}")
        if not page2.locator('input[type="password"]').first.is_visible():
            p.rate("session invalide : écran exploitable",
                   "le formulaire de connexion réaffiché",
                   f"formulaire absent (texte : {page2.locator('body').inner_text()[:120]!r})")
        p.ok("session invalide", "retour au login, formulaire intact")
        b2.close()

        # ── 6. SSO : présence du parcours (le bouton) ───────────────────────
        if not oidc:
            p.avertissement("SSO", "oidc_enabled=false : parcours SSO non offert, contrôle sauté")
        else:
            b3, ctx3 = c.nouveau_contexte(pw)
            page3 = ctx3.new_page()
            page3.goto(f"{c.BASE}/login", wait_until="domcontentloaded")
            bouton = page3.get_by_role("button", name=re.compile(r"SSO", re.I)).first
            try:
                bouton.wait_for(state="visible", timeout=15000)
            except Exception:
                p.rate("bouton SSO offert", "un bouton « Se connecter avec le SSO » visible",
                       "aucun bouton SSO sur la page de connexion")
            p.ok("bouton SSO", "offert sur la page de connexion")
            b3.close()

    # ── 7. SSO : /login/sso redirige vers le fournisseur OIDC ───────────────
    if oidc:
        statut, entetes, corps = c.http("GET", f"{c.BASE}/login/sso",
                                        suivre_redirections=False)
        location = entetes.get("Location") or entetes.get("location") or ""
        if statut not in (301, 302, 303, 307, 308):
            p.rate("redirection SSO (/login/sso)", "un code 30x",
                   f"HTTP {statut} — {corps[:120]!r}")
        if "gjallarhorn.cronos.website" not in location:
            p.rate("fournisseur OIDC", "Location vers gjallarhorn.cronos.website",
                   f"Location {location[:120]!r}")
        p.ok("redirection SSO", f"30x vers {location.split('/')[2]}")

    p.fin()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001 — un échec doit parler, pas remonter
        code = c.fin_parcours_echec(NOM, e)
    sys.exit(code)
