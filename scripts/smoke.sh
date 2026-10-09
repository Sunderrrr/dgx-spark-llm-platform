#!/usr/bin/env bash
# Post-deploy verification: does the RUNNING platform still do its job?
#
# « il faudrait mettre une pipeline de vérification quand on fait des modif »
# (operator, 2026-10-09) — the pre-push gate and the CI prove the CODE is
# sound; nothing proved the PLATFORM still works after the containers moved.
# Measured gap: a change can pass 724 tests and still leave the site serving
# 500s, because the tests run against a throwaway copy, not against what is
# deployed. This closes it: five probes, each one a thing a user does.
#
# Run it by hand (`./scripts/smoke.sh`) or through `scripts/deploy.sh`, which
# refuses to call a deploy done until this returns 0.
# Le monde extérieur (chemins, domaines, bucket) vient de .env — voir
# scripts/config.sh : rien en dur dans ce script.
. "$(dirname "${BASH_SOURCE[0]}")/config.sh"

set -u

BASE=${SMOKE_BASE:-${DGX_WEB}}
API=${SMOKE_API:-${DGX_API}}
ECHEC=0

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
ko()   { printf '  \033[31m✗\033[0m %s\n' "$*"; ECHEC=1; }

echo "· le site répond"
code=$(curl -s -o /dev/null -w '%{http_code}' -m 15 "$BASE/login")
[ "$code" = "200" ] && ok "page de connexion (200)" || ko "page de connexion (HTTP $code)"

echo "· la passerelle répond"
code=$(curl -s -o /dev/null -w '%{http_code}' -m 15 "$API/v1/models")
# 401 is the RIGHT answer without a key: it proves the gateway is up AND that
# it refuses strangers. Anything else is a problem.
[ "$code" = "401" ] && ok "API sans clé (401, comme attendu)" || ko "API sans clé (HTTP $code, attendu 401)"

echo "· le modèle sert une réponse"
if docker exec dgx-portal python3 - <<'PY' >/dev/null 2>&1
import sqlite3, sys
sys.path.insert(0, '/app')
import requests
db = sqlite3.connect('/app/data/portal.db')
row = db.execute("SELECT key_value FROM api_keys LIMIT 1").fetchone()
if not row:
    sys.exit(1)
r = requests.post('http://litellm:4001/chat/completions',
                  headers={'Authorization': 'Bearer ' + row[0], 'Content-Type': 'application/json'},
                  json={'model': 'auto-model', 'messages': [{'role': 'user', 'content': 'dis ok'}],
                        'max_tokens': 6}, timeout=90)
sys.exit(0 if r.status_code == 200 else 1)
PY
then ok "une clé réelle génère une réponse (200)"; else ko "la génération échoue"; fi

echo "· la recherche web trouve quelque chose"
if docker exec dgx-portal python3 - <<'PY' >/dev/null 2>&1
import sys
sys.path.insert(0, '/app')
import app as portal, websearch
with portal.app.app_context():
    r = websearch.rechercher('test', nombre=1)
    sys.exit(0 if r else 1)
PY
then ok "SearXNG renvoie des résultats"; else ko "la recherche web ne renvoie rien"; fi

echo "· les services répondent"
docker exec dgx-portal python3 -c "
import requests, sys
code = requests.get('http://litellm:4001/health/liveliness', timeout=10).status_code
sys.exit(0 if code == 200 else 1)
" >/dev/null 2>&1 && ok "litellm (200)" || ko "litellm ne répond pas"

if [ "$ECHEC" -eq 0 ]; then
  echo
  printf '\033[32m✓ La plateforme répond comme attendu.\033[0m\n'
else
  echo
  printf '\033[31m✗ Un ou plusieurs contrôles échouent — ne pas annoncer le déploiement.\033[0m\n'
fi
exit $ECHEC
