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

# Two families:
#  · SANS_CLE — public, no key: registered ENABLED, they work right away;
#  · AVEC_CLE — the usual suspects (GitHub, Sentry, Slack…): registered DISABLED,
#    waiting for the account's own key. Writing the key in Réglages → MCP
#    activates them (see settings_routes.py: `enabled=CASE WHEN ? THEN 1 …`).
# Gmail has no public remote MCP endpoint (verified 2026-10-05: no such host,
# and the aggregators like Zapier/Composio redirect to their own auth): it
# needs either a local server (refused by the platform's SSRF guard, by
# design) or a per-user aggregator URL, registered by hand with its key.
SANS_CLE = [
    ('context7', 'https://mcp.context7.com/mcp',
     "Documentation A JOUR des bibliothèques et frameworks : API, versions, "
     "changements récents. À utiliser dès qu'une question porte sur une "
     "bibliothèque, un SDK ou une intégration (OpenCode, Cursor, Continue…)."),
    ('deepwiki', 'https://mcp.deepwiki.com/mcp',
     "Questions/réponses sur la documentation des dépôts GitHub publics "
     "(llama.cpp, vLLM, LiteLLM…). À utiliser pour comprendre le comportement "
     "d'un projet open source."),
]

AVEC_CLE = [
    ('github', 'https://api.githubcopilot.com/mcp/',
     "Dépôts GitHub : issues, pull requests, code, actions, releases. "
     "Clé : un token personnel GitHub (PAT) — « Bearer ghp_… »."),
    ('sentry', 'https://mcp.sentry.dev/mcp',
     "Sentry : erreurs, issues, releases de tes projets — le complément "
     "naturel du diagnostic de la plateforme. Clé : « Bearer sntryu_… »."),
    ('cloudflare', 'https://mcp.cloudflare.com/mcp',
     "Cloudflare : DNS, Workers, tunnels, réglages du compte (le trafic de la "
     "plateforme passe par là). Clé : « Bearer » + un token API."),
    ('slack', 'https://mcp.slack.com/mcp',
     "Slack : canaux, messages, recherche, envoi — pour joindre l'équipe. "
     "Clé : « Bearer xoxb-… » (token de bot)."),
    ('notion', 'https://mcp.notion.com/mcp',
     "Notion : pages, bases de données, recherche — la documentation "
     "d'équipe. Clé : « Bearer ntn_… » (integration token)."),
    ('linear', 'https://mcp.linear.app/mcp',
     "Linear : issues, projets, cycles — le suivi de chantier. "
     "Clé : « Bearer lin_api_… »."),
    ('figma', 'https://mcp.figma.com/mcp',
     "Figma : fichiers, designs, variables — les maquettes. "
     "Clé : « Bearer figd_… » (personal access token)."),
    ('stripe', 'https://mcp.stripe.com',
     "Stripe : paiements, clients, abonnements, factures. "
     "Clé : « Bearer sk_… » (secret key)."),
]

ESSENTIELS = SANS_CLE + AVEC_CLE

with portal.app.app_context():
  for username in sys.argv[1:]:
    db = get_db()
    for famille, actif in ((SANS_CLE, 1), (AVEC_CLE, 0)):
        for nom, url, desc in famille:
            ok, detail = validate_mcp_url(url)
            if not ok:
                print(f"✗ {nom} : URL refusée ({detail})")
                continue
            db.execute(
                "INSERT OR REPLACE INTO mcp_servers "
                "(username, name, url, auth_header, description, allowed_tools, enabled, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (username, nom, url, None, desc, '', actif,
                 time.strftime('%Y-%m-%dT%H:%M:%S')))
            etat = "actif" if actif else "en attente de sa clé"
            print(f"✓ {username} · {nom} ({etat})")
    db.commit()
PYEOF
