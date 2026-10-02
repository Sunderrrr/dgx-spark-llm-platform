#!/bin/sh
# Lance la suite de tests dans un conteneur jetable construit depuis l'image du
# portail : mêmes dépendances qu'en production, et une base SQLite neuve (aucun
# volume monté) — les tests ne touchent donc jamais aux données réelles.
#
#   ./dgx-portal/run-tests.sh            # tout
#   ./dgx-portal/run-tests.sh test_app   # un seul module
set -e
cd "$(dirname "$0")/.."
# Le nom de l'image construite dépend du nom du DOSSIER du clone
# (`<projet>-dgx-portal`), et le `docker run` plus bas doit viser le même : sans
# cet épinglage, la commande documentée dans le README (« git clone … puis
# ./dgx-portal/run-tests.sh ») échoue partout ailleurs que dans un dossier nommé
# « ai-platform », sur un « Unable to find image ». La CI épingle déjà ce nom
# (.github/workflows/ci.yml), donc les deux suivent désormais la même règle.
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-ai-platform}"
docker compose build dgx-portal >/dev/null
# Les arguments d'unittest se choisissent AVANT l'exec : `${1:+tests.$1}
# ${1:-discover -s tests}` passait le module DEUX fois (« tests.test_app
# test_app »), donc chaque exécution d'un seul module finissait sur
# « FAILED (errors=1) » à cause d'un module introuvable — un faux échec au bout
# d'une commande que la doc recommande.
if [ -n "${1:-}" ]; then
  set -- "tests.$1"
else
  set -- discover -s tests
fi
exec docker run --rm \
  -e SECRET_KEY=test-secret-0123456789abcdef0123456789abcdef \
  -e LITELLM_MASTER_KEY=sk-test \
  -e CRONOS_NO_REAPER=1 \
  --entrypoint python ai-platform-dgx-portal \
  -m unittest "$@" -v
