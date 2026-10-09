#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parcours « clés API » — la boucle complète, de la création au refus.

Depuis l'interface (Réglages → Clés API) : création avec un libellé ; la clé
est affichée UNE fois (contrat : jamais réaffichée ensuite) ; elle figure dans
la liste ; elle génère une complétion via API avec le
modèle `auto-model` (l'APPEL passe par le domaine public, jamais par un port
interne) ; puis elle est RÉVOQUÉE et l'API la refuse désormais (401).

Nettoyage GARANTI (try/finally) : la clé créée par le test est toujours
révoquée, même en cas d'échec — le compte démo n'est pas pollué.
"""
from config import (WEB, API, DOMAINE, DOMAINE_SSO, RACINE, DEMO_PASS, DEMO_USER,
                    S3_CFG, S3_BUCKET, PYTEST_BIN)
import re
import sys
import time

import commun as c

NOM = "clés API"
DELAIS_S = 300


def ouvrir_reglages_compte(page):
    """La roue crantée de la COQUILLE ouvre le dialogue du compte
    (Réglages → Clés API) — pas celle du playground."""
    boutons = page.get_by_role("button", name=c.alternance("Réglages", "Settings"))
    for i in range(boutons.count()):
        bt = boutons.nth(i)
        if not bt.is_visible():
            continue
        bt.click()
        page.wait_for_timeout(800)
        if page.locator(".playground-settings-panel").count():
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
            continue
        if c.texte_visible(page, "Clés API", "API keys") is not None:
            return
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
    raise c.EchecParcours(NOM, "Réglages → Clés API",
                          "le dialogue du compte avec la section « Clés API »",
                          "aucune roue crantée n'ouvre ce dialogue")


def lire_cle_visible(page):
    """La clé complète affichée dans le DOM (elle n'y est qu'à la création)."""
    return page.evaluate("""() => {
        const out = [];
        for (const e of document.querySelectorAll("*")) {
            if (e.children.length) continue;
            const t = (e.textContent || "").trim();
            if (/^sk-[A-Za-z0-9_.-]{16,}$/.test(t)) out.push(t);
        }
        return out;
    }""")


def appeler_api(cle: str, delai_s: int = 90):
    """Vrai appel HTTP vers le DOMAINE de l'API (pas un port interne)."""
    return c.http("POST", f"{c.API}/v1/chat/completions",
                  corps={"model": "auto-model",
                         "messages": [{"role": "user", "content": "dis ok"}],
                         "max_tokens": 16},
                  entetes={"Authorization": f"Bearer {cle}"},
                  delai_s=delai_s)


def main() -> int:
    p = c.Parcours(NOM)
    c.borne(DELAIS_S)
    alias = f"e2e-valider-{time.strftime('%H%M%S')}"
    cle = None
    revokue = False

    with c.lancer_playwright() as pw:
        b, ctx = c.nouveau_contexte(pw)
        page = ctx.new_page()
        # Les confirm()/prompt() natifs de l'interface (révocation) : on accepte.
        page.on("dialog", lambda d: d.accept())
        c.connexion(page, p)
        try:
            # ── 1. Réglages → Clés API ──────────────────────────────────────
            ouvrir_reglages_compte(page)
            c.texte_visible(page, "Clés API", "API keys").click()
            page.wait_for_timeout(800)
            if c.texte_visible(page, "Mes clés API", "My API keys") is None:
                p.rate("section « Clés API »", "le panneau « Mes clés API »",
                       "le panneau ne s'est pas ouvert")
            p.ok("Réglages → Clés API", "panneau « Mes clés API » ouvert")

            # ── 2. Création avec un libellé ─────────────────────────────────
            champ = page.get_by_placeholder(
                c.alternance("Nom (ex: mon-laptop)", "Name (e.g. my-laptop)")).first
            champ.fill(alias)
            c.bouton(page, "Nouvelle clé", "New key").first.click()
            page.wait_for_timeout(2500)

            # ── 3. La clé est affichée UNE fois ─────────────────────────────
            cles = lire_cle_visible(page)
            if not cles:
                p.rate("clé créée et affichée",
                       "la valeur de la clé visible dans la ligne (affichage "
                       "unique au moment de la création)",
                       f"aucune clé « sk-… » dans le DOM — libellé {alias}")
            cle = cles[0]
            p.ok("clé créée et affichée",
                 f"{cle[:10]}…{cle[-4:]} (valeur complète lue dans le DOM)")

            # ── 4. Elle figure dans la liste ────────────────────────────────
            if c.texte_visible(page, alias, alias) is None:
                p.rate("clé listée", f"le libellé « {alias} » dans la liste",
                       "libellé absent de la liste des clés")
            p.ok("clé listée", f"libellé « {alias} » présent")

            # ── 5. Contrat « affichée une seule fois » ──────────────────────
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            ouvrir_reglages_compte(page)
            c.texte_visible(page, "Clés API", "API keys").click()
            page.wait_for_timeout(1500)
            if cle in lire_cle_visible(page):
                p.rate("clé jamais réaffichée",
                       "après rechargement, la valeur masquée (sk-…xxxx) sans "
                       "la clé complète dans le DOM",
                       "la clé complète est encore affichée après rechargement")
            masquee = page.evaluate("""() => {
                for (const e of document.querySelectorAll("*")) {
                    if (e.children.length) continue;
                    const t = (e.textContent || "").trim();
                    if (/^sk-.+….+$/.test(t)) return t;
                }
                return null;
            }""")
            if not masquee:
                p.rate("forme masquée affichée",
                       "la valeur tronquée « sk-… » dans la liste",
                       "aucune valeur masquée trouvée")
            p.ok("affichage unique", f"après rechargement : « {masquee} » seulement")

            # ── 6. La clé génère une complétion via le DOMAINE de l'API ────
            statut, _, corps = appeler_api(cle)
            donnees = c.corps_json(statut, corps)
            contenu = (((donnees or {}).get("choices") or [{}])[0]
                       .get("message", {}).get("content") if donnees else None)
            if statut != 200 or not (contenu or "").strip():
                p.rate("complétion via API",
                       f"200 avec une réponse (modèle auto-model) depuis {c.API}",
                       f"HTTP {statut} — {corps[:200]!r}")
            p.ok("complétion via le domaine de l'API",
                 f"{c.API}/v1/chat/completions → 200, réponse « {contenu.strip()[:40]}… »")

            # ── 7. Révocation par l'interface ───────────────────────────────
            ligne = page.get_by_text(alias, exact=True).first
            cliquee = False
            for i in range(1, 9):
                ancetre = ligne.locator(f"xpath=ancestor::*[{i}]")
                bouton_r = ancetre.get_by_role("button", name=c.alternance("Révoquer", "Revoke"))
                if bouton_r.count():
                    bouton_r.first.click()
                    cliquee = True
                    break
            if not cliquee:
                p.rate("bouton « Révoquer »", "un bouton Révoquer sur la ligne de la clé",
                       "aucun bouton révocable trouvé pour cette clé")
            page.wait_for_timeout(2500)
            if c.texte_visible(page, alias, alias) is not None:
                p.rate("clé révoquée et retirée de la liste",
                       f"le libellé « {alias} » disparaît de la liste",
                       "toujours présent après la révocation")
            p.ok("clé révoquée", "retirée de la liste par l'interface")
            revokue = True

            # ── 8. L'API la refuse désormais ───────────────────────────────
            refus = None
            limite = time.time() + 60
            while time.time() < limite:
                statut, _, corps = appeler_api(cle, delai_s=30)
                refus = statut
                if statut in (401, 403):
                    break
                time.sleep(3)
            if refus not in (401, 403):
                p.rate("clé révoquée refusée par l'API",
                       f"401/403 de {c.API} pour une clé révoquée",
                       f"HTTP {refus} — la clé révoquée passe encore")
            p.ok("clé révoquée refusée", f"HTTP {refus} pour la clé révoquée")
        finally:
            # ── Nettoyage : la clé du test ne survit JAMAIS au parcours ─────
            if cle and not revokue:
                r = ctx.request.get(f"{c.BASE}/api/csrf")
                jeton = (r.json() or {}).get("token", "") if r.status == 200 else ""
                rep = ctx.request.post(f"{c.BASE}/keys",
                                       form={"action": "revoke", "key": cle},
                                       headers={"X-CSRFToken": jeton})
                p.avertissement("nettoyage forcé de la clé",
                                f"révocation par la route /keys (HTTP {rep.status})")
            elif cle and revokue:
                p.ok("nettoyage", "aucune clé du test laissée sur le compte")

        b.close()

    p.fin()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001
        code = c.fin_parcours_echec(NOM, e)
    sys.exit(code)
