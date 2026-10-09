#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Inventaire des routes Flask du portail — à faire tourner dans l'IMAGE DE
TEST (jamais sur la production) :

    docker run --rm -e SECRET_KEY=… -e LITELLM_MASTER_KEY=sk-test \
      -e DGX_NO_REAPER=1 -v "$PWD/tests/e2e/dump_routes.py:/app/dump_routes.py:ro" \
      --entrypoint python ai-platform-dgx-portal /app/dump_routes.py

Sort un unique objet JSON : {"routes": [{rule, endpoint, methods}, …]}.
C'est la matière première de `verifier_couverture.py` : la comparaison avec
`routes_connues.py` refuse qu'une route ajoutée plus tard passe entre les
mailles SILENCIEUSEMENT — elle doit être couverte par un parcours ou exclue
avec une motivation écrite.
"""
# No dependency on e2e_config: this runs ALONE inside the test image, with
# only this file mounted. Keep it self-contained.
import json
import os
import sys

sys.path.insert(0, "/app")
os.environ.setdefault("DGX_NO_REAPER", "1")

import app as portal  # noqa: E402  — import du portail, image de test uniquement

routes = []
for regle in portal.app.url_map.iter_rules():
    methodes = sorted(m for m in regle.methods if m not in ("HEAD", "OPTIONS"))
    routes.append({"rule": regle.rule, "endpoint": regle.endpoint, "methods": methodes})

routes.sort(key=lambda r: (r["endpoint"], r["rule"]))
print(json.dumps({"routes": routes}, ensure_ascii=False))
