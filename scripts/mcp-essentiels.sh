#!/usr/bin/env bash
# Register the ESSENTIAL MCP servers for a Cronos account.
#
# Why a script and not just rows in the database: these two servers are what
# the Support assistant needs to answer « how do I integrate X » from CURRENT
# documentation instead of its training data — and a database that is rebuilt
# (or a new account) should not quietly lose them.
#
#   scripts/mcp-essentiels.sh mboitel        # one account
#   scripts/mcp-essentiels.sh mboitel demo   # several
#
# Both servers are public MCP endpoints (Streamable HTTP, no key):
#   · context7  — up-to-date library/framework docs (API, versions, changes)
#   · deepwiki  — Q&A over GitHub repositories' documentation
# They are validated with the platform's OWN SSRF check before being written,
# exactly like a registration done from Settings → MCP.
#
# Per-account by design: a user who does not want external tools in their
# Support chat simply has nothing registered. The guardrail already in place
# stays: once third-party tool output has entered a turn, privileged tools
# (keys, budget, model, services) are refused for that turn.
set -eu
cd "$(dirname "$0")/.."

[ $# -ge 1 ] || { echo "usage : scripts/mcp-essentiels.sh <utilisateur>…"; exit 1; }

args=""
for u in "$@"; do args="$args '$u'"; done

docker exec -i dgx-portal python3 - "$@" <<'PYEOF'
import sys
import time

sys.path.insert(0, '/app')
import app as portal                      # noqa: E402
from db import get_db                     # noqa: E402
from mcp_client import validate_mcp_url   # noqa: E402

ESSENTIELS = [
    ('context7', 'https://mcp.context7.com/mcp',
     "Documentation A JOUR des bibliothèques et frameworks : API, versions, "
     "changements récents. À utiliser dès qu'une question porte sur une "
     "bibliothèque, un SDK ou une intégration (OpenCode, Cursor, Continue…)."),
    ('deepwiki', 'https://mcp.deepwiki.com/mcp',
     "Questions/réponses sur la documentation des dépôts GitHub publics "
     "(llama.cpp, vLLM, LiteLLM…). À utiliser pour comprendre le comportement "
     "d'un projet open source."),
]

with portal.app.app_context():
  for username in sys.argv[1:]:
    db = get_db()
    for nom, url, desc in ESSENTIELS:
        ok, detail = validate_mcp_url(url)
        if not ok:
            print(f"✗ {nom} : URL refusée ({detail})")
            continue
        db.execute(
            "INSERT OR REPLACE INTO mcp_servers "
            "(username, name, url, auth_header, description, allowed_tools, enabled, created_at) "
            "VALUES (?,?,?,?,?,?,1,?)",
            (username, nom, url, None, desc, '', time.strftime('%Y-%m-%dT%H:%M:%S')))
        print(f"✓ {username} · {nom}")
    db.commit()
PYEOF
