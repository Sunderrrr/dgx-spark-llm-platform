#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parcours « sécurité » — deux trous fermés, vérifiés de l'extérieur.

Depuis l'extérieur (le même chemin que n'importe quel visiteur : Cloudflare →
Traefik) :
  - /metrics n'est PAS servi publiquement (refus explicite, pas un 200) ;
  - /internal/authcheck (forwardAuth de Traefik) n'est PAS joignable depuis
    l'extérieur du réseau du proxy — ni sur le domaine web, ni sur le domaine
    API.
"""
import sys

import commun as c

NOM = "sécurité"
DELAIS_S = 120

REFUS_ATTENDUS = (401, 403, 404)


def main() -> int:
    p = c.Parcours(NOM)
    c.borne(DELAIS_S)

    # ── 1. /metrics : pas de métriques publiques ────────────────────────────
    for domaine in (c.BASE, c.API):
        statut, _, corps = c.http("GET", f"{domaine}/metrics", delai_s=20)
        if statut in (200, 206) or (statut is not None and 200 <= statut < 300):
            p.rate(f"{domaine}/metrics non servi publiquement",
                   f"un refus {REFUS_ATTENDUS} (403/401 attendus)",
                   f"HTTP {statut} — des métriques sont exposées publiquement")
        if "prometheus" in corps.lower() or "python_info" in corps:
            p.rate(f"{domaine}/metrics non servi publiquement",
                   "aucune métrique dans le corps de la réponse",
                   f"corps Prometheus reçu : {corps[:120]!r}")
        if statut not in REFUS_ATTENDUS:
            p.rate(f"{domaine}/metrics non servi publiquement",
                   f"un refus explicite {REFUS_ATTENDUS}",
                   f"HTTP {statut} — réponse ambiguë, la fermeture doit être nette")
        p.ok(f"{domaine}/metrics", f"refusé (HTTP {statut})")

    # ── 2. /internal/authcheck : hors de portée depuis l'extérieur ─────────
    for domaine in (c.BASE, c.API):
        statut, entetes, corps = c.http("GET", f"{domaine}/internal/authcheck",
                                        delai_s=20)
        if statut == 200:
            p.rate(f"{domaine}/internal/authcheck hors de portée",
                   "aucun service de ce chemin depuis l'extérieur (404)",
                   f"HTTP 200 — le contrôle d'authentification interne répond "
                   f"depuis l'extérieur : {corps[:120]!r}")
        if corps and corps.strip() and corps.strip() not in ("{}", "null"):
            # Même en cas de refus, la réponse ne doit rien révéler.
            if any(mot in corps for mot in ("username", "is_admin", "session")):
                p.rate(f"{domaine}/internal/authcheck hors de portée",
                       "une réponse vide, sans donnée d'identité",
                       f"corps révélateur : {corps[:120]!r}")
        if statut not in REFUS_ATTENDUS and statut is not None:
            p.rate(f"{domaine}/internal/authcheck hors de portée",
                   f"un refus {REFUS_ATTENDUS}",
                   f"HTTP {statut}")
        p.ok(f"{domaine}/internal/authcheck",
             f"HTTP {statut} — non joignable depuis l'extérieur")

    p.fin()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as e:  # noqa: BLE001
        code = c.fin_parcours_echec(NOM, e)
    sys.exit(code)
