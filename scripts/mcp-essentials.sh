#!/usr/bin/env bash
# Register the ESSENTIAL MCP servers for a DGX account.
#
# Why a script and not just rows in the database: these two servers are what
# the Support assistant needs to answer « how do I integrate X » from CURRENT
# documentation instead of its training data — and a database that is rebuilt
# (or a new account) should not quietly lose them.
#
#   scripts/mcp-essentials.sh mdupont        # one account
#   scripts/mcp-essentials.sh mdupont demo   # several
#
# Both servers are public MCP endpoints (Streamable HTTP, no key):
#   · context7  — up-to-date library/framework docs (API, versions, changes)
#   · deepwiki  — Q&A over GitHub repositories' documentation
# No registration-time SSRF check here (it was Flask-only, mcp_client.py): the
# platform checks every URL again when it connects (portal-rs/src/mcp.rs,
# skillstore::validated_target), so a refused address never reaches the network.
#
# Per-account by design: a user who does not want external tools in their
# Support chat simply has nothing registered. The guardrail already in place
# stays: once third-party tool output has entered a turn, privileged tools
# (keys, budget, model, services) are refused for that turn.
set -eu
cd "$(dirname "$0")/.."

[ $# -ge 1 ] || { echo "usage: scripts/mcp-essentials.sh <username>…"; exit 1; }

# The portal database: the data volume of portal-rs, written on the host
# (the same file, the same table; no Python inside any container).
PORTAL_DB="$(docker inspect portal-rs -f '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Source}}{{end}}{{end}}')/portal.db" \
  || { echo "portal-rs data volume not found"; exit 1; }

python3 - "$PORTAL_DB" "$@" <<'PYEOF'
import sqlite3
import sys
import time

# Two families (the descriptions are content shown in the UI and to the
# model, hence French like the rest of the product's text):
#  · KEYLESS — public, no key: registered ENABLED, they work right away;
#  · WITH_KEY — the usual suspects (GitHub, Sentry, Slack…): registered DISABLED,
#    waiting for the account's own key. Writing the key in Settings → MCP
#    activates them (see settings_routes.py: `enabled=CASE WHEN ? THEN 1 …`).
# Gmail has no public remote MCP endpoint (verified 2026-10-05: no such host,
# and the aggregators like Zapier/Composio redirect to their own auth): it
# needs either a local server (refused by the platform's SSRF guard, by
# design) or a per-user aggregator URL, registered by hand with its key.
KEYLESS = [
    ('context7', 'https://mcp.context7.com/mcp',
     "Documentation A JOUR des bibliothèques et frameworks : API, versions, "
     "changements récents. À utiliser dès qu'une question porte sur une "
     "bibliothèque, un SDK ou une intégration (OpenCode, Cursor, Continue…)."),
    ('deepwiki', 'https://mcp.deepwiki.com/mcp',
     "Questions/réponses sur la documentation des dépôts GitHub publics "
     "(llama.cpp, vLLM, LiteLLM…). À utiliser pour comprendre le comportement "
     "d'un projet open source."),
    ('huggingface', 'https://huggingface.co/mcp',
     "Hugging Face : recherche de modèles, jeux de données, détail des dépôts "
     "du Hub — LA source du catalogue de modèles de la plateforme. À utiliser "
     "pour « quel modèle pour X », « qu'y a-t-il de nouveau », « que vaut ce "
     "dépôt ». Sans clé."),
    ('exa', 'https://mcp.exa.ai/mcp',
     "Recherche web + lecture de page (Exa) — la seule façon pour l'assistant "
     "d'aller voir ce qui se passe EN DEHORS de la documentation : nouveautés, "
     "incidents connus, annonces. Sans clé."),
]

WITH_KEY = [
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
    ('discord', 'https://mcp.discord.com/mcp',
     "Discord : serveurs, canaux, messages — la plateforme y notifie déjà les "
     "demandes de modèle et de budget. Clé : « Bearer » + un token d'application."),
]

ESSENTIALS = KEYLESS + WITH_KEY

db = sqlite3.connect(sys.argv[1], timeout=30)
for username in sys.argv[2:]:
    for family, enabled in ((KEYLESS, 1), (WITH_KEY, 0)):
        for name, url, desc in family:
            db.execute(
                "INSERT OR REPLACE INTO mcp_servers "
                "(username, name, url, auth_header, description, allowed_tools, enabled, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (username, name, url, None, desc, '', enabled,
                 time.strftime('%Y-%m-%dT%H:%M:%S')))
            state = "enabled" if enabled else "waiting for its key"
            print(f"✓ {username} · {name} ({state})")
    db.commit()
db.close()
PYEOF
