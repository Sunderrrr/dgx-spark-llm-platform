"""Configuration du portail, lue dans l'environnement.

Extraite de app.py le 28/08, apres db.py. Deuxieme piece du noyau partage : les
sections encore dans le monolithe (SSO, Support, OCR, Helpers) s'accrochent
toutes a ces constantes, donc rien de plus n'etait extractible tant qu'elles
vivaient dans app.py.

Ce module n'importe que `os` : il ne peut creer aucun cycle.
"""
import os

# ── LDAP ─────────────────────────────────────────────────────────────────────
# Serveur Authentik : les comptes vivent sous ou=users, l'identifiant de
# connexion est l'attribut `cn` (le `uid` est un hash interne Authentik, PAS le
# nom d'utilisateur), et le nom affiché est `displayName`.
LDAP_URI        = os.environ.get('LDAP_URI', 'ldap://100.73.45.103:389')
LDAP_BASE       = os.environ.get('LDAP_BASE', 'dc=cronos,dc=lan')
LDAP_BIND_DN    = os.environ.get('LDAP_BIND_DN', '')
LDAP_BIND_PW    = os.environ.get('LDAP_BIND_PW', '')
# RDN des comptes, relatif à LDAP_BASE (ou=users ici ; l'ancien lldap était ou=people).
LDAP_USERS_DN   = os.environ.get('LDAP_USERS_DN', 'ou=users')
# Attribut utilisé comme identifiant de connexion (cn ici ; l'ancien lldap était uid).
LDAP_LOGIN_ATTR = os.environ.get('LDAP_LOGIN_ATTR', 'cn')

# ── Services internes ────────────────────────────────────────────────────────
LITELLM_URL   = os.environ.get('LITELLM_URL', 'http://litellm:4000')
LITELLM_KEY   = os.environ.get('LITELLM_MASTER_KEY', '')
VLLM_API      = os.environ.get('VLLM_API_URL', 'http://host.docker.internal:8000/v1')
RUNNER_URL    = os.environ.get('VLLM_RUNNER_URL', 'http://host.docker.internal:8001')
RUNNER_TOKEN  = os.environ.get('RUNNER_TOKEN', '')
# ComfyUI (MiniMax H3 video generation) — host process, never exposed (127.0.0.1
# only on the host, reached via host.docker.internal like the vLLM runner).
COMFYUI_URL   = os.environ.get('COMFYUI_URL', 'http://host.docker.internal:8188')
# OCR (baidu/Unlimited-OCR) — container on the internal docker network, never
# a port published on the host.
OCR_URL       = os.environ.get('OCR_URL', 'http://ocr:8000/v1')
# Voice (Chatterbox, cloning) — same reasoning as OCR, dedicated network.
VOICE_URL     = os.environ.get('VOICE_URL', 'http://voice:8004')
# Transcription (dictation) — same.
ASR_URL       = os.environ.get('ASR_URL', 'http://asr:8006')
MUSIC_URL     = os.environ.get('MUSIC_URL', 'http://music:8008')
# Sidecar image : etait declare dans la section image du monolithe, alors que
# c'est une URL de service comme les autres — et sidecars.py en a besoin.
IMAGE_URL = os.environ.get('IMAGE_URL', 'http://image:8007')
DISCORD_WH    = os.environ.get('DISCORD_WEBHOOK_URL', '')
# Discord DM notifications: a bot DMs each user who linked their account (OAuth2
# "identify") whenever an announcement fires (model change, site announcement,
# maintenance, new model). The bot token sends DMs; the client id/secret drive
# the account-linking OAuth flow. All optional — absent → the feature is off.
DISCORD_BOT_TOKEN     = os.environ.get('DISCORD_BOT_TOKEN', '')
DISCORD_CLIENT_ID     = os.environ.get('DISCORD_CLIENT_ID', '')
DISCORD_CLIENT_SECRET = os.environ.get('DISCORD_CLIENT_SECRET', '')
DISCORD_REDIRECT_URI  = os.environ.get('DISCORD_REDIRECT_URI', '')
DISCORD_LINK_ENABLED  = bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET)
DISCORD_API           = 'https://discord.com/api/v10'
SMTP_HOST     = os.environ.get('SMTP_HOST', '')
SMTP_PORT     = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER     = os.environ.get('SMTP_USER', '')
SMTP_PASS     = os.environ.get('SMTP_PASSWORD', '')
SMTP_FROM     = os.environ.get('SMTP_FROM', '')
ADMIN_EMAIL   = os.environ.get('ADMIN_EMAIL', '')
# URL du dashboard admin (pour le CTA des emails de notification). Si vide, le
# bouton « Open the Admin dashboard » n'est pas rendu dans le gabarit HTML.
ADMIN_URL     = os.environ.get('ADMIN_URL', '')
# Fenêtre d'anti-spam pour les demandes « lancer une catégorie média » (secondes).
MEDIA_REQUEST_COOLDOWN_S = int(os.environ.get('MEDIA_REQUEST_COOLDOWN_S', '1800'))
KEY_BUDGET    = float(os.environ.get('KEY_MAX_BUDGET', '0.002'))
KEY_DURATION  = os.environ.get('KEY_BUDGET_DURATION', '1d')

# Public URL of the OpenAI-compatible API, shown to users.
PUBLIC_API_URL = os.environ.get('PUBLIC_API_URL', 'https://api.cronos.website/v1')
# Amont vu par LiteLLM, et nom du modele virtuel qui suit le modele actif.
VLLM_API_BASE = os.environ.get('VLLM_API_BASE', 'http://host.docker.internal:8000/v1')
AUTO_MODEL_NAME = os.environ.get('AUTO_MODEL_NAME', 'auto-model')
# LiteLLM database (Postgres) for timestamped consumption stats.
LITELLM_DB_URL = os.environ.get('LITELLM_DATABASE_URL', '')
LOCAL_TZ       = os.environ.get('TZ_DISPLAY', 'Europe/Paris')

# ── SSO / OIDC (Authentik) ───────────────────────────────────────────────────
OIDC_METADATA_URL  = os.environ.get('OIDC_METADATA_URL', '')
OIDC_CLIENT_ID     = os.environ.get('OIDC_CLIENT_ID', '')
OIDC_CLIENT_SECRET = os.environ.get('OIDC_CLIENT_SECRET', '')
OIDC_REDIRECT_URI  = os.environ.get('OIDC_REDIRECT_URI', '')
OIDC_LOGOUT_URL    = os.environ.get('OIDC_LOGOUT_URL', '')
OIDC_ADMIN_GROUP   = os.environ.get('OIDC_ADMIN_GROUP', 'adm_cronos')
OIDC_ENABLED       = bool(OIDC_METADATA_URL and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET)


# ── WebAuthn / passkeys (2FA par clé de sécurité) ────────────────────────────
# La passkey est liée à l'ORIGINE exacte (scheme+host). L'accès public passe
# par https://dgx.cronos.website (Cloudflare → Traefik) ; c'est l'origine que
# les utilisateurs déclarent au navigateur, donc celle à laquelle la clé est
# rattachée. Une clé enregistrée ici ne fonctionnera PAS depuis une autre
# origine (ex. http://dgx.cronos.lan, scheme/host différent).
WEBAUTHN_RP_ID   = os.environ.get('WEBAUTHN_RP_ID', 'dgx.cronos.website')
WEBAUTHN_RP_NAME = os.environ.get('WEBAUTHN_RP_NAME', 'Cronos')
WEBAUTHN_ORIGIN  = os.environ.get('WEBAUTHN_ORIGIN', 'https://dgx.cronos.website')
# Exiger la vérification utilisateur (PIN/biometrie) en plus de la présence.
# Off par défaut : une clé physique "touch-only" (YubiKey classique) ne fait PAS
# de vérification utilisateur — l'exiger bloquerait ces clés. `preferred` exige
# la présence (touch) mais accepte une UV quand l'authentificateur en offre une
# (passkey OS, 1Password, YubiKey avec PIN). À activer seulement si toute la
# flotte le supporte.
WEBAUTHN_REQUIRE_UV = os.environ.get('WEBAUTHN_REQUIRE_UV', '0') == '1'


# ── Apparence (avatars, themes, langues) ─────────────────────────────────────
# Lues par les reglages, par l'historique de conversations et par l'amorcage
# (purge des avatars disparus) : ce sont des constantes partagees, pas des
# details de la page de reglages.
AVATAR_IDS = [
    'claude', 'anthropic', 'openai', 'copilot', 'gemini', 'grok', 'mistral',
    'deepseek', 'qwen', 'meta', 'ollama', 'huggingface', 'perplexity',
    'nvidia', 'langchain',
]
# Offered palettes: each maps to an Astryx theme built on the
# frontend via defineTheme({extends: neutralTheme, color: {accent}}) — the
# official design-system path. We never override --color-* in :root.
THEME_IDS = ['neutral', 'indigo', 'violet', 'rose', 'ambre', 'emeraude',
             'cyan', 'ardoise', 'brique', 'prune']
LANGS = ['fr', 'en']

AVATAR_LABELS = {
    'claude': 'Claude', 'anthropic': 'Anthropic', 'openai': 'ChatGPT',
    'copilot': 'GitHub Copilot', 'gemini': 'Gemini', 'grok': 'Grok',
    'mistral': 'Mistral', 'deepseek': 'DeepSeek', 'qwen': 'Qwen',
    'meta': 'Llama (Meta)', 'ollama': 'Ollama', 'huggingface': 'Hugging Face',
    'perplexity': 'Perplexity', 'nvidia': 'NVIDIA', 'langchain': 'LangChain',
}
