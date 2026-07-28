#!/bin/sh
# Lance la suite de tests dans un conteneur jetable construit depuis l'image du
# portail : mêmes dépendances qu'en production, et une base SQLite neuve (aucun
# volume monté) — les tests ne touchent donc jamais aux données réelles.
#
#   ./dgx-portal/run-tests.sh            # tout
#   ./dgx-portal/run-tests.sh test_app   # un seul module
set -e
cd "$(dirname "$0")/.."
docker compose build dgx-portal >/dev/null
exec docker run --rm \
  -e SECRET_KEY=test-secret \
  -e LITELLM_MASTER_KEY=sk-test \
  --entrypoint python ai-platform-dgx-portal \
  -m unittest ${1:+tests.$1} ${1:-discover -s tests} -v
