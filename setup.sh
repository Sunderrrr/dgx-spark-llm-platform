#!/bin/bash
# Generates the .env file with random secrets.
#
# All PURELY INTERNAL secrets (LiteLLM master key, Postgres password, Flask
# session key, portal↔runner token) are generated here: after this script, the
# stack starts with no "changeme" value left anywhere on an internal path. Only
# the secrets that depend on external services (LDAP, OIDC/Authentik, SMTP,
# Discord) remain to be filled in by hand.
set -e

# ── Technical artifacts derived from secrets, generated BEFORE the .env guard ──
# They deliberately sit above the ".env already exists" check: rerunning
# setup.sh on an existing install must be able to repair a missing artifact,
# without requiring .env to be deleted (which would regenerate every internal
# secret).

# SearXNG reads the WHOLE mounted folder (`./searxng:/etc/searxng`). Without
# settings.yml, the image writes its own, which does not enable the JSON format
# the portal requires: web search then answers 403, with no clear message on the
# portal side.
mkdir -p searxng
if [ ! -s searxng/settings.yml ]; then
  sed "s|secret_key: \"changeme\"|secret_key: \"$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')\"|" \
    searxng/settings.yml.example > searxng/settings.yml
  chmod 600 searxng/settings.yml
  echo "✓ searxng/settings.yml généré (format JSON activé, secret aléatoire, 0600)"
fi

# crawl4ai service token: it refuses to listen beyond loopback without a
# credential, and the compose mounts it from ./secrets/. The file must be
# readable by the UNPRIVILEGED container → 0644 (a 0600 root-owned file would be
# unreadable, and the container would refuse to start).
mkdir -p secrets && chmod 700 secrets
if [ ! -s secrets/crawl4ai_token ]; then
  python3 -c "import secrets;print(secrets.token_urlsafe(32))" > secrets/crawl4ai_token
  echo "✓ secrets/crawl4ai_token généré (0644, lisible par le conteneur)"
fi
chmod 644 secrets/crawl4ai_token

if [ -f .env ]; then
  echo ".env existe déjà. Supprime-le si tu veux le regénérer."
  exit 0
fi

generate_key() {
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"
}

cp .env.example .env

LITELLM_KEY="sk-$(generate_key)"
POSTGRES_PASS="$(generate_key)"
WEBUI_KEY="$(generate_key)"
RUNNER_TOK="$(generate_key)"
SEARXNG_SECRET="$(generate_key)"

sed -i "s|LITELLM_MASTER_KEY=sk-changeme|LITELLM_MASTER_KEY=${LITELLM_KEY}|" .env
sed -i "s|POSTGRES_PASSWORD=changeme|POSTGRES_PASSWORD=${POSTGRES_PASS}|" .env
sed -i "s|WEBUI_SECRET_KEY=changeme|WEBUI_SECRET_KEY=${WEBUI_KEY}|" .env
sed -i "s|RUNNER_TOKEN=changeme|RUNNER_TOKEN=${RUNNER_TOK}|" .env
sed -i "s|SEARXNG_SECRET=changeme|SEARXNG_SECRET=${SEARXNG_SECRET}|" .env

chmod 600 .env

echo "✓ .env généré (secrets internes aléatoires, permissions 600)"
echo ""
echo "  LITELLM_MASTER_KEY written to .env (needed for the LiteLLM dashboard —"
echo "  read it with: grep LITELLM_MASTER_KEY .env). Not printed here to keep it"
echo "  out of terminal scrollback / CI logs."
echo ""
echo "À remplir à la main (dépendances externes) : LDAP_BIND_PW,"
echo "OIDC_CLIENT_SECRET / AUTHENTIK_LITELLM_CLIENT_SECRET, SMTP_*, ADMIN_EMAIL,"
echo "DISCORD_WEBHOOK_URL — puis : docker compose up -d"
