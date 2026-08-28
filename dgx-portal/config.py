"""Configuration du portail, lue dans l'environnement.

Extraite de app.py le 28/08, apres db.py. Deuxieme piece du noyau partage : les
sections encore dans le monolithe (SSO, Support, OCR, Helpers) s'accrochent
toutes a ces constantes, donc rien de plus n'etait extractible tant qu'elles
vivaient dans app.py.

Ce module n'importe que `os` : il ne peut creer aucun cycle.

Ce qui reste VOLONTAIREMENT dans app.py : DEBUG_LOGIN_FLAG et DEBUG_USERS_FILE,
avec toute la mecanique de connexion de secours. LDAP et le SSO sont eteints,
c'est donc le seul chemin d'acces a la plateforme — on n'y touche pas pour un
gain de rangement. Seul DEBUG_ADMIN_USERNAMES vient ici : c'est une variable
d'environnement, pas un chemin de fichier, et le SSO en a besoin.
"""
import os

# ── LDAP ─────────────────────────────────────────────────────────────────────
LDAP_URI      = os.environ.get('LDAP_URI', 'ldap://lldap.cronos.lan:3890')
LDAP_BASE     = os.environ.get('LDAP_BASE', 'dc=cronos,dc=website')
LDAP_BIND_DN  = os.environ.get('LDAP_BIND_DN', '')
LDAP_BIND_PW  = os.environ.get('LDAP_BIND_PW', '')

# Comptes administrateurs de la connexion de secours (cf. app.py pour le reste).
DEBUG_ADMIN_USERNAMES = {u.strip() for u in os.environ.get('DEBUG_ADMIN_USERNAMES', '').split(',') if u.strip()}

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
