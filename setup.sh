#!/bin/bash
# Génère le fichier .env avec des clés aléatoires
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

sed -i "s|LITELLM_MASTER_KEY=sk-changeme|LITELLM_MASTER_KEY=${LITELLM_KEY}|" .env
sed -i "s|POSTGRES_PASSWORD=changeme|POSTGRES_PASSWORD=${POSTGRES_PASS}|" .env
sed -i "s|WEBUI_SECRET_KEY=changeme|WEBUI_SECRET_KEY=${WEBUI_KEY}|" .env

echo "✓ .env généré"
echo ""
echo "  LITELLM_MASTER_KEY = ${LITELLM_KEY}"
echo "  (note cette clé — tu en auras besoin pour le dashboard LiteLLM)"
echo ""
echo "Remplis maintenant DISCORD_WEBHOOK_URL, SMTP_* et ADMIN_EMAIL dans .env"
echo "puis lance : docker compose up -d"
