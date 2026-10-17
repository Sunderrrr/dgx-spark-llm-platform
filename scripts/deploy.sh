#!/usr/bin/env bash
# Deploy = build + replace + VERIFY. Never the first two alone.
#
# « we should have a verification pipeline whenever we change something »
# — the gap this closes is simple and it bit us: `docker compose up -d` prints
# « Started » whether the platform works or not. This refuses to call a deploy
# done until `scripts/smoke.sh` says the RUNNING service does its job, and it
# prints the rollback line when it does not.
#
#   ./scripts/deploy.sh                # portal + frontend
#   ./scripts/deploy.sh litellm searxng
# The outside world (paths, domains, bucket) comes from .env — see
# scripts/config.sh: nothing hardcoded in this script.
. "$(dirname "${BASH_SOURCE[0]}")/config.sh"

set -eu
cd ${DGX_ROOT}

SERVICES=${*:-"portal-rs dgx-portal-frontend"}

echo "· build: $SERVICES"
# The heap is capped in the Dockerfile: a build must never push the served
# model into swap (CLAUDE.md, « Any heavy build is memory-capped »).
# shellcheck disable=SC2086
docker compose build $SERVICES

echo "· replace"
# shellcheck disable=SC2086
docker compose up -d $SERVICES

echo "· waiting for it to come back"
sleep 10

echo "· verifying the running service"
if ./scripts/smoke.sh; then
  echo
  printf '\033[32m✓ Deploy done and verified.\033[0m\n'
else
  echo
  printf '\033[31m✗ The deploy changed something that breaks.\033[0m\n'
  printf '   Roll back: git checkout <commit> && ./scripts/deploy.sh %s\n' "$SERVICES"
  exit 1
fi
