#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ferme les sessions que la validation a ouvertes pour le compte démo.

Chaque parcours se connecte avec le compte démo et ne se déconnecte pas (un
seul le fait, j01) : chaque passage de validate.sh laissait ~15 sessions
vivantes 12 h, et l'état de la plateforme affichait « 238 sessions actives »
pour 2 personnes. validate.sh appelle ce script à sa sortie, échec compris, avec
son heure de départ : seules les sessions du compte démo ouvertes PENDANT ce
passage sont révoquées (sql_portail refuse toute instruction qui ne vise pas
le compte démo).

    fermer_sessions.py <epoch de départ>
"""
import sys

import commun as c


def main() -> int:
    depuis = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
    filtre = f"username='demo' AND revoked=0 AND created_at >= {depuis:.0f}"
    # Two statements (sql_portail commits before it reads, which RETURNING refuses).
    n = int(c.sql_portail(f"SELECT COUNT(*) FROM user_sessions WHERE {filtre}").strip("[](),") or 0)
    c.sql_portail(f"UPDATE user_sessions SET revoked=1 WHERE {filtre}")
    print(f"✓ {n} session(s) du compte démo fermée(s) (ouvertes par cette validation)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 — un ménage raté ne fait pas échouer la validation
        print(f"⚠ sessions du compte démo non fermées : {e}")
        sys.exit(0)
