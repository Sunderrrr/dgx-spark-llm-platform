#!/usr/bin/env bash
# Deploy = build + replace + VERIFY. Never the first two alone.
#
# « il faudrait mettre une pipeline de vérification quand on fait des modif »
# — the gap this closes is simple and it bit us: `docker compose up -d` prints
# « Started » whether the platform works or not. This refuses to call a deploy
# done until `scripts/smoke.sh` says the RUNNING service does its job, and it
# prints the rollback line when it does not.
#
#   ./scripts/deploy.sh                # portal + frontend
#   ./scripts/deploy.sh litellm searxng
# Le monde extérieur (chemins, domaines, bucket) vient de .env — voir
# scripts/config.sh : rien en dur dans ce script.
. "$(dirname "${BASH_SOURCE[0]}")/config.sh"

set -eu
cd ${DGX_ROOT}

SERVICES=${*:-"dgx-portal dgx-portal-frontend"}

echo "· construction : $SERVICES"
# The heap is capped in the Dockerfile: a build must never push the served
# model into swap (CLAUDE.md, « Any heavy build is memory-capped »).
# shellcheck disable=SC2086
docker compose build $SERVICES

echo "· remplacement"
# shellcheck disable=SC2086
docker compose up -d $SERVICES

echo "· attente de la remontée"
sleep 10

echo "· vérification du service en fonctionnement"
if ./scripts/smoke.sh; then
  echo
  printf '\033[32m✓ Déploiement terminé et vérifié.\033[0m\n'
else
  echo
  printf '\033[31m✗ Le déploiement a changé quelque chose qui casse.\033[0m\n'
  printf '   Revenir en arrière : git checkout <commit> && ./scripts/deploy.sh %s\n' "$SERVICES"
  exit 1
fi
