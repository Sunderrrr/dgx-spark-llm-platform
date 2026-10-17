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

# The probes run from portal-rs, the container that now answers the site: it
# sits on the same networks and carries the same upstream addresses.
# The database is the portal-rs data volume, read on the host.
PORTAL_DB="$(docker inspect portal-rs -f '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Source}}{{end}}{{end}}' 2>/dev/null)/portal.db"

echo "· the portal reaches the host engines"
# The portal's URLs target 172.19.0.1 (see the subnet note in
# docker-compose.yml). On another subnet the packets leave with a source the
# firewall drops, and the only symptom is « no model », with nothing logged.
# Any HTTP answer counts (the runner asks for its token): only no answer fails.
code=$(docker exec portal-rs sh -c 'curl -s -o /dev/null -w "%{http_code}" -m 5 "$VLLM_RUNNER_URL/status"' 2>/dev/null) || code=000
if [ "$code" != "000" ] && [ -n "$code" ]; then
  ok "runner reachable from the portal (HTTP $code)"
else
  gw=$(docker inspect portal-rs -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}={{$v.Gateway}} {{end}}' 2>/dev/null)
  ko "runner unreachable from the portal — its networks: ${gw:-?} (expected a 172.19.0.1 gateway)"
fi

echo "· the model serves an answer"
# The key is read from the database file (read-only) and handed to curl through
# stdin, so it never appears in a command line or in the output.
KEY=$(python3 - "$PORTAL_DB" <<'PY' 2>/dev/null
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
row = db.execute("SELECT key_value FROM api_keys LIMIT 1").fetchone()
print(row[0] if row else "")
PY
) || KEY=""
BODY='{"model":"auto-model","messages":[{"role":"user","content":"say ok"}],"max_tokens":6}'
code=000
if [ -n "$KEY" ]; then
  code=$(printf 'Authorization: Bearer %s\n' "$KEY" | docker exec -i portal-rs sh -c \
    'IFS= read -r H; curl -s -o /dev/null -w "%{http_code}" -m 90 -H "$H" -H "Content-Type: application/json" -d "$1" "$LITELLM_URL/chat/completions"' \
    sh "$BODY" 2>/dev/null) || code=000
fi
[ "$code" = "200" ] && ok "a real key generates an answer (200)" || ko "generation fails (HTTP $code)"
unset KEY

echo "· web search finds something"
# SearXNG, asked the way the portal asks it (websearch.rs). crawl4ai is not probed here.
if docker exec portal-rs sh -c 'curl -s -m 15 -G "$SEARXNG_URL/search" --data-urlencode q=test -d format=json -d language=fr' 2>/dev/null \
    | python3 -c 'import json, sys; sys.exit(0 if json.load(sys.stdin).get("results") else 1)' >/dev/null 2>&1; then
  ok "SearXNG returns results"
else
  ko "web search returns nothing"
fi

echo "· the services answer"
code=$(docker exec portal-rs sh -c 'curl -s -o /dev/null -w "%{http_code}" -m 10 "$LITELLM_URL/health/liveliness"' 2>/dev/null) || code=000
[ "$code" = "200" ] && ok "litellm (200)" || ko "litellm does not answer (HTTP $code)"

if [ "$FAILED" -eq 0 ]; then
  echo
  printf '\033[32m✓ The platform answers as expected.\033[0m\n'
else
  echo
  printf '\033[31m✗ One or more checks fail — do not announce the deploy.\033[0m\n'
fi
exit $FAILED
