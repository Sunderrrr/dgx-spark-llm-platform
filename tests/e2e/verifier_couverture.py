#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Contrôle de couverture des routes du portail.

Règle (celle que l'opérateur a demandée) : TOUTE route Flask doit être soit
touchée par un parcours e2e, soit explicitement exclue avec une motivation
écrite dans `tests/e2e/routes_connues.py`. Une route ajoutée plus tard ne doit
jamais passer entre les mailles silencieusement : ce contrôle dit « route non
couverte » et demande une décision.

    python3 tests/e2e/verifier_couverture.py                 # inventaire live
    python3 tests/e2e/verifier_couverture.py --dump-file F   # inventaire figé

L'inventaire est produit dans l'IMAGE DE TEST (jamais sur la production) par
`tests/e2e/dump_routes.py` : même conteneur jetable que `dgx-portal/run-tests.sh`,
base SQLite neuve, aucun volume de données monté.

Sorties : lignes lisibles, sans couleur, code 0 si tout est classé, 1 sinon.
"""
from config import (WEB, API, DOMAINE, DOMAINE_SSO, RACINE, DEMO_PASS, DEMO_USER,
                    S3_CFG, S3_BUCKET, PYTEST_BIN)
import argparse
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from routes_connues import ROUTES  # noqa: E402

IMAGE = "ai-platform-dgx-portal"
DELAI_BUILD_S = 300
DELAI_DUMP_S = 120


def inventaire(dump_file: str | None) -> list[dict]:
    if dump_file:
        with open(dump_file, encoding="utf-8") as f:
            return json.load(f)["routes"]
    # Image de test : build identique à run-tests.sh, puis dump dans le
    # conteneur jetable. CRONOS_NO_REAPER=1 : aucun thread de fond.
    build = subprocess.run(["docker", "compose", "build", "dgx-portal"],
                           cwd=REPO, capture_output=True, text=True,
                           timeout=DELAI_BUILD_S)
    if build.returncode != 0:
        raise RuntimeError("docker compose build dgx-portal a échoué : "
                           + build.stderr.strip()[-300:])
    script = os.path.join(REPO, "tests", "e2e", "dump_routes.py")
    run = subprocess.run(
        ["docker", "run", "--rm",
         "-e", "SECRET_KEY=test-secret-0123456789abcdef0123456789abcdef",
         "-e", "LITELLM_MASTER_KEY=sk-test",
         "-e", "CRONOS_NO_REAPER=1",
         "-v", f"{script}:/app/dump_routes.py:ro",
         "--entrypoint", "python", IMAGE, "/app/dump_routes.py"],
        capture_output=True, text=True, timeout=DELAI_DUMP_S)
    if run.returncode != 0:
        raise RuntimeError("dump_routes.py a échoué dans l'image de test : "
                           + (run.stderr.strip()[-300:] or run.stdout.strip()[-300:]))
    return json.loads(run.stdout)["routes"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Contrôle de couverture des routes")
    ap.add_argument("--dump-file", help="inventaire JSON sauvegardé (sinon : docker)")
    args = ap.parse_args()

    try:
        routes = inventaire(args.dump_file)
    except Exception as e:  # noqa: BLE001 — l'inventaire doit être fiable
        print(f"✗ impossible d'inventorier les routes : {e}")
        return 1

    endpoints = {r["endpoint"] for r in routes}
    problemes = 0

    # 1. Toute route vivante doit être dans le registre.
    non_couvertes = sorted(endpoints - set(ROUTES))
    for ep in non_couvertes:
        regle = next(r["rule"] for r in routes if r["endpoint"] == ep)
        print(f"✗ route non couverte : {ep} ({regle}) — décide : ajoute-la à un "
              f"parcours de tests/e2e/ ou motive son exclusion dans "
              f"tests/e2e/routes_connues.py")
        problemes += 1

    # 2. Une entrée du registre qui ne correspond plus à rien doit disparaître.
    obsoletes = sorted(set(ROUTES) - endpoints)
    for ep in obsoletes:
        print(f"✗ entrée obsolète dans routes_connues : {ep} — la route n'existe "
              f"plus, supprime l'entrée (ou corrige son nom)")
        problemes += 1

    # 3. Une exclusion sans motivation n'en est pas une.
    for ep, (genre, motif) in sorted(ROUTES.items()):
        if ep in obsoletes:
            continue
        if genre not in ("parcours", "exclu") or not (motif or "").strip():
            print(f"✗ entrée incomplète dans routes_connues : {ep} — "
                  f"attendu (\"parcours\", parcours) ou (\"exclu\", motivation)")
            problemes += 1

    couverts = sum(1 for ep, v in ROUTES.items()
                   if v[0] == "parcours" and ep in endpoints)
    exclus = sum(1 for ep, v in ROUTES.items()
                 if v[0] == "exclu" and ep in endpoints)
    print(f"· {len(endpoints)} routes inventoriées : {couverts} couvertes par les "
          f"parcours, {exclus} exclues (motivées)")
    if problemes:
        print(f"✗ couverture incomplète : {problemes} décision(s) à prendre")
        return 1
    print("✓ chaque route a une décision de test écrite")
    return 0


if __name__ == "__main__":
    sys.exit(main())
