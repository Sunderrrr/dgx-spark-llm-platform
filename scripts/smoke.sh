#!/usr/bin/env bash
# Post-deploy verification: does the RUNNING platform still do its job?
#
# « we should have a verification pipeline whenever we change something »
# (operator, 2026-10-09) — the pre-push gate and the CI prove the CODE is
# sound; nothing proved the PLATFORM still works after the containers moved.
# Measured gap: a change can pass 724 tests and still leave the site serving
# 500s, because the tests run against a throwaway copy, not against what is
# deployed. This closes it: five probes, each one a thing a user does.
#
# Run it by hand (`./scripts/smoke.sh`) or through `scripts/deploy.sh`, which
# refuses to call a deploy done until this returns 0.
# The outside world (paths, domains, bucket) comes from .env — see
# scripts/config.sh: nothing hardcoded in this script.
DGX_CHECK_DOMAINS=1 . "$(dirname "${BASH_SOURCE[0]}")/config.sh"

set -u

BASE=${SMOKE_BASE:-${DGX_WEB}}
API=${SMOKE_API:-${DGX_API}}
FAILED=0

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
ko()   { printf '  \033[31m✗\033[0m %s\n' "$*"; FAILED=1; }

echo "· the site answers"
code=$(curl -s -o /dev/null -w '%{http_code}' -m 15 "$BASE/login")
[ "$code" = "200" ] && ok "login page (200)" || ko "login page (HTTP $code)"

echo "· the gateway answers"
code=$(curl -s -o /dev/null -w '%{http_code}' -m 15 "$API/v1/models")
# 401 is the RIGHT answer without a key: it proves the gateway is up AND that
# it refuses strangers. Anything else is a problem.
[ "$code" = "401" ] && ok "API without a key (401, as expected)" || ko "API without a key (HTTP $code, expected 401)"

echo "· the model serves an answer"
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
                  json={'model': 'auto-model', 'messages': [{'role': 'user', 'content': 'say ok'}],
                        'max_tokens': 6}, timeout=90)
sys.exit(0 if r.status_code == 200 else 1)
PY
then ok "a real key generates an answer (200)"; else ko "generation fails"; fi

echo "· web search finds something"
if docker exec dgx-portal python3 - <<'PY' >/dev/null 2>&1
import sys
sys.path.insert(0, '/app')
import app as portal, websearch
with portal.app.app_context():
    results, _error = websearch.search('test', count_value=1)
    sys.exit(0 if results else 1)
PY
then ok "SearXNG returns results"; else ko "web search returns nothing"; fi

echo "· the services answer"
docker exec dgx-portal python3 -c "
import requests, sys
code = requests.get('http://litellm:4001/health/liveliness', timeout=10).status_code
sys.exit(0 if code == 200 else 1)
" >/dev/null 2>&1 && ok "litellm (200)" || ko "litellm does not answer"

if [ "$FAILED" -eq 0 ]; then
  echo
  printf '\033[32m✓ The platform answers as expected.\033[0m\n'
else
  echo
  printf '\033[31m✗ One or more checks fail — do not announce the deploy.\033[0m\n'
fi
exit $FAILED
