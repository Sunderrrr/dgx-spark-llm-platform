#!/bin/bash
# Génère le fichier .env avec des secrets aléatoires.
#
# Tous les secrets PUREMENT INTERNES (clé maître LiteLLM, mot de passe Postgres,
# clé de session Flask, jeton portail↔runner) sont générés ici : après ce
# script, la pile démarre sans qu'aucune valeur « changeme » ne subsiste sur un
# chemin interne. Seuls restent à remplir à la main les secrets qui dépendent
# de services externes (LDAP, OIDC/Authentik, SMTP, Discord).
set -e

# ── Artefacts techniques dérivés de secrets, générés AVANT la garde sur .env ──
# Ils sont volontairement au-dessus du « .env existe déjà » : relancer setup.sh
# sur une installation en place doit pouvoir réparer un artefact manquant, sans
# exiger de supprimer .env (ce qui régénérerait tous les secrets internes).

# SearXNG lit TOUT le dossier monté (`./searxng:/etc/searxng`). Sans settings.yml,
# l'image écrit le sien, qui n'active pas le format JSON que le portail demande :
# la recherche web répond alors 403, sans message clair côté portail.
mkdir -p searxng
if [ ! -s searxng/settings.yml ]; then
  sed "s|secret_key: \"changeme\"|secret_key: \"$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')\"|" \
    searxng/settings.yml.example > searxng/settings.yml
  chmod 600 searxng/settings.yml
  echo "✓ searxng/settings.yml généré (format JSON activé, secret aléatoire, 0600)"
fi

# Jeton de service crawl4ai : il refuse d'écouter ailleurs qu'en loopback sans
# credential, et le compose le monte depuis ./secrets/. Le fichier doit être
# lisible par le conteneur NON privilégié → 0644 (un 0600 root le rendrait
# illisible, et le conteneur refuserait de démarrer).
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
