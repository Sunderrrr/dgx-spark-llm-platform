"""Portal configuration, read from the environment.

Extracted from app.py on 28/08, after db.py. Second piece of the shared
core: the sections still in the monolith (SSO, Support, OCR, Helpers) all
hook onto these constants, so nothing more was extractable as long as they
lived in app.py.

This module only imports `os`: it cannot create any cycle.
"""
import os

# ── LDAP ─────────────────────────────────────────────────────────────────────
# Authentik server: accounts live under ou=users, the login identifier is the
# `cn` attribute (the `uid` is an internal Authentik hash, NOT the username),
# and the display name is `displayName`.
LDAP_URI        = os.environ.get('LDAP_URI', 'ldap://100.73.45.103:389')
LDAP_BASE       = os.environ.get('LDAP_BASE', 'dc=example,dc=org')
LDAP_BIND_DN    = os.environ.get('LDAP_BIND_DN', '')
LDAP_BIND_PW    = os.environ.get('LDAP_BIND_PW', '')
# Account RDN, relative to LDAP_BASE (the deployment's own layout).
LDAP_USERS_DN   = os.environ.get('LDAP_USERS_DN', 'ou=users')
# Attribute used as login identifier (cn here; the old lldap was uid).
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
# Image sidecar: was declared in the monolith's image section, although it is
# a service URL like the others — and sidecars.py needs it.
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
# Admin dashboard URL (for the CTA of notification emails). If empty, the
# « Open the Admin dashboard » button is not rendered in the HTML template.
ADMIN_URL     = os.environ.get('ADMIN_URL', '')
# Anti-spam window for « launch a media category » requests (seconds).
MEDIA_REQUEST_COOLDOWN_S = int(os.environ.get('MEDIA_REQUEST_COOLDOWN_S', '1800'))
# Default account budget: 200 M tokens / week (2026-09-08, operator choice —
# previously 0,002 « dollar-ish » inherited from a LiteLLM trial, then 60
# M/day). These defaults only serve fresh installs: the values live in the
# settings table and are editable in the Admin.
KEY_BUDGET    = float(os.environ.get('KEY_MAX_BUDGET', '200000000'))
KEY_DURATION  = os.environ.get('KEY_BUDGET_DURATION', '7d')

# Public URL of the OpenAI-compatible API, shown to users.
PUBLIC_API_URL = os.environ.get('PUBLIC_API_URL', 'https://api.cronos.website/v1')
# Upstream as seen by LiteLLM, and name of the virtual model that follows the active model.
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
OIDC_ADMIN_GROUP   = os.environ.get('OIDC_ADMIN_GROUP', 'admins')
OIDC_ENABLED       = bool(OIDC_METADATA_URL and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET)


# ── WebAuthn / passkeys (2FA by security key) ───────────────────────────────
# The passkey is bound to the EXACT origin (scheme+host). Public access goes
# through https://dgx.cronos.website (Cloudflare → Traefik); that is the
# origin users declare to the browser, hence the one the key is bound to. A
# key registered here will NOT work from another origin (e.g.
# http://dgx.cronos.lan, different scheme/host).
WEBAUTHN_RP_ID   = os.environ.get('WEBAUTHN_RP_ID', 'dgx.cronos.website')
WEBAUTHN_RP_NAME = os.environ.get('WEBAUTHN_RP_NAME', 'Cronos')
WEBAUTHN_ORIGIN  = os.environ.get('WEBAUTHN_ORIGIN', 'https://dgx.cronos.website')
# Require user verification (PIN/biometrics) on top of presence.
# Off by default: a "touch-only" physical key (classic YubiKey) does NO user
# verification — requiring it would block those keys. `preferred` requires
# presence (touch) but accepts a UV when the authenticator offers one (OS
# passkey, 1Password, YubiKey with PIN). Enable only if the whole fleet
# supports it.
WEBAUTHN_REQUIRE_UV = os.environ.get('WEBAUTHN_REQUIRE_UV', '0') == '1'


# ── Apparence (avatars, themes, langues) ─────────────────────────────────────
# Read by the settings, by the conversation history and by the bootstrap
# (purge of vanished avatars): these are shared constants, not details of the
# settings page.
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
