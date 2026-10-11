#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Coverage check of the portal's routes.

Rule: EVERY Flask route must be either exercised by an e2e journey, or
explicitly excluded with a written reason in `tests/e2e/known_routes.py`. A
route added later must never slip through silently: this check says « route
not covered » and asks for a decision.

    python3 tests/e2e/check_coverage.py                 # live inventory
    python3 tests/e2e/check_coverage.py --dump-file F   # frozen inventory

The inventory is produced in the TEST IMAGE (never on production) by
`tests/e2e/dump_routes.py`: the same throwaway container as
`dgx-portal/run-tests.sh`, a fresh SQLite database, no data volume mounted.

Output: readable lines, no color, exit 0 if everything is classified, 1 if not.
"""
import argparse
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from known_routes import ROUTES  # noqa: E402

IMAGE = "ai-platform-dgx-portal"
BUILD_TIMEOUT_S = 300
DUMP_TIMEOUT_S = 120


def inventory(dump_file: str | None) -> list[dict]:
    if dump_file:
        with open(dump_file, encoding="utf-8") as f:
            return json.load(f)["routes"]
    # Test image: the same build as run-tests.sh, then a dump in the throwaway
    # container. DGX_NO_REAPER=1: no background thread.
    build = subprocess.run(["docker", "compose", "build", "dgx-portal"],
                           cwd=REPO, capture_output=True, text=True,
                           timeout=BUILD_TIMEOUT_S)
    if build.returncode != 0:
        raise RuntimeError("docker compose build dgx-portal failed: "
                           + build.stderr.strip()[-300:])
    script = os.path.join(REPO, "tests", "e2e", "dump_routes.py")
    run = subprocess.run(
        ["docker", "run", "--rm",
         "-e", "SECRET_KEY=test-secret-0123456789abcdef0123456789abcdef",
         "-e", "LITELLM_MASTER_KEY=sk-test",
         "-e", "DGX_NO_REAPER=1",
         "-v", f"{script}:/app/dump_routes.py:ro",
         "--entrypoint", "python", IMAGE, "/app/dump_routes.py"],
        capture_output=True, text=True, timeout=DUMP_TIMEOUT_S)
    if run.returncode != 0:
        raise RuntimeError("dump_routes.py failed in the test image: "
                           + (run.stderr.strip()[-300:] or run.stdout.strip()[-300:]))
    return json.loads(run.stdout)["routes"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Route coverage check")
    ap.add_argument("--dump-file", help="saved JSON inventory (default: docker)")
    args = ap.parse_args()

    try:
        routes = inventory(args.dump_file)
    except Exception as e:  # noqa: BLE001 — the inventory must be reliable
        print(f"✗ cannot inventory the routes: {e}")
        return 1

    endpoints = {r["endpoint"] for r in routes}
    problems = 0

    # 1. Every live route must be in the registry.
    for ep in sorted(endpoints - set(ROUTES)):
        rule = next(r["rule"] for r in routes if r["endpoint"] == ep)
        print(f"✗ route not covered: {ep} ({rule}) — decide: add it to a journey of "
              f"tests/e2e/ or give the reason for its exclusion in tests/e2e/known_routes.py")
        problems += 1

    # 2. A registry entry that no longer matches anything must go.
    stale = sorted(set(ROUTES) - endpoints)
    for ep in stale:
        print(f"✗ stale entry in known_routes: {ep} — the route no longer exists, "
              f"delete the entry (or fix its name)")
        problems += 1

    # 3. An exclusion without a reason is not one.
    for ep, (kind, reason) in sorted(ROUTES.items()):
        if ep in stale:
            continue
        if kind not in ("journey", "excluded") or not (reason or "").strip():
            print(f"✗ incomplete entry in known_routes: {ep} — "
                  f"expected (\"journey\", journey) or (\"excluded\", reason)")
            problems += 1

    covered = sum(1 for ep, v in ROUTES.items() if v[0] == "journey" and ep in endpoints)
    excluded = sum(1 for ep, v in ROUTES.items() if v[0] == "excluded" and ep in endpoints)
    print(f"· {len(endpoints)} routes inventoried: {covered} covered by journeys, "
          f"{excluded} excluded (with a reason)")
    if problems:
        print(f"✗ incomplete coverage: {problems} decision(s) to make")
        return 1
    print("✓ every route has a written test decision")
    return 0


if __name__ == "__main__":
    sys.exit(main())
