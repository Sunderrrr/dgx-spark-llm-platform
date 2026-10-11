#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Inventory of the portal's Flask routes — to run in the TEST IMAGE (never
on production):

    docker run --rm -e SECRET_KEY=… -e LITELLM_MASTER_KEY=sk-test \
      -e DGX_NO_REAPER=1 -v "$PWD/tests/e2e/dump_routes.py:/app/dump_routes.py:ro" \
      --entrypoint python ai-platform-dgx-portal /app/dump_routes.py

Prints a single JSON object: {"routes": [{rule, endpoint, methods}, …]}. It is
the raw material of `check_coverage.py`: the comparison with `known_routes.py`
refuses that a route added later slips through SILENTLY — it must be covered by
a journey or excluded with a written reason.
"""
# No dependency on e2e_config: this runs ALONE inside the test image, with
# only this file mounted. Keep it self-contained.
import json
import os
import sys

sys.path.insert(0, "/app")
os.environ.setdefault("DGX_NO_REAPER", "1")

import app as portal  # noqa: E402  — the portal itself, test image only

routes = []
for rule in portal.app.url_map.iter_rules():
    methods = sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS"))
    routes.append({"rule": rule.rule, "endpoint": rule.endpoint, "methods": methods})

routes.sort(key=lambda r: (r["endpoint"], r["rule"]))
print(json.dumps({"routes": routes}, ensure_ascii=False))
