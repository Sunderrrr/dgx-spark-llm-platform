#!/bin/bash
# Génère le fichier .env avec des secrets aléatoires.
#
# Tous les secrets PUREMENT INTERNES (clé maître LiteLLM, mot de passe Postgres,
# clé de session Flask, jeton portail↔runner) sont générés ici : après ce
# script, la pile démarre sans qu'aucune valeur « changeme » ne subsiste sur un
# chemin interne. Seuls restent à remplir à la main les secrets qui dépendent
# de services externes (LDAP, OIDC/Authentik, SMTP, Discord).
set -e

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

sed -i "s|LITELLM_MASTER_KEY=sk-changeme|LITELLM_MASTER_KEY=${LITELLM_KEY}|" .env
sed -i "s|POSTGRES_PASSWORD=changeme|POSTGRES_PASSWORD=${POSTGRES_PASS}|" .env
sed -i "s|WEBUI_SECRET_KEY=changeme|WEBUI_SECRET_KEY=${WEBUI_KEY}|" .env
sed -i "s|RUNNER_TOKEN=changeme|RUNNER_TOKEN=${RUNNER_TOK}|" .env

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
