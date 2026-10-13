"""Portal configuration, read from the environment.

Extracted from app.py on 28/08, after db.py. Second piece of the shared
core: the sections still in the monolith (SSO, Support, OCR, Helpers) all
hook onto these constants, so nothing more was extractable as long as they
lived in app.py.

This module only imports `os`: it cannot create any cycle.
"""
import os

# LDAP, SSO and SMTP are NOT here: they are set from Admin → Config and read
# live from the database (parametres.py). `.env` only starts the stack.

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
# Image sidecar: was declared in the monolith's image section, although it is
# a service URL like the others — and sidecars.py needs it.
IMAGE_URL = os.environ.get('IMAGE_URL', 'http://image:8007')
# Discord DM notifications: a bot DMs each user who linked their account (OAuth2
# "identify") whenever an announcement fires (model change, site announcement,
# maintenance, new model). The bot token sends DMs; the client id/secret drive
# the account-linking OAuth flow. All optional — absent → the feature is off.
DISCORD_API           = 'https://discord.com/api/v10'
# Anti-spam window for « launch a media category » requests (seconds).
MEDIA_REQUEST_COOLDOWN_S = int(os.environ.get('MEDIA_REQUEST_COOLDOWN_S', '1800'))
# Default account budget: 200 M tokens / week (2026-09-08, operator choice —
# previously 0,002 « dollar-ish » inherited from a LiteLLM trial, then 60
# M/day). These defaults only serve fresh installs: the values live in the
# settings table and are editable in the Admin.
KEY_BUDGET    = float(os.environ.get('KEY_MAX_BUDGET', '200000000'))
KEY_DURATION  = os.environ.get('KEY_BUDGET_DURATION', '7d')

# Public origins of this deployment (.env: DGX_WEB, DGX_API). Example values
# by default: a fresh install must say « example.org », never someone's domain.
PUBLIC_WEB_URL = os.environ.get('DGX_WEB', 'https://example.org').rstrip('/')
# The admin dashboard, for the « Open the Admin dashboard » button of the
# notification e-mails: derived from the public origin, nothing to configure.
ADMIN_URL = f'{PUBLIC_WEB_URL}/admin'
# Public URL of the OpenAI-compatible API, shown to users.
PUBLIC_API_URL = os.environ.get(
    'PUBLIC_API_URL',
    os.environ.get('DGX_API', 'https://api.example.org').rstrip('/') + '/v1')
# Upstream as seen by LiteLLM, and name of the virtual model that follows the active model.
VLLM_API_BASE = os.environ.get('VLLM_API_BASE', 'http://host.docker.internal:8000/v1')
AUTO_MODEL_NAME = os.environ.get('AUTO_MODEL_NAME', 'auto-model')
# LiteLLM database (Postgres) for timestamped consumption stats.
LITELLM_DB_URL = os.environ.get('LITELLM_DATABASE_URL', '')
LOCAL_TZ       = os.environ.get('TZ_DISPLAY', 'Europe/Paris')

# ── WebAuthn / passkeys (2FA by security key) ───────────────────────────────
# The passkey is bound to the EXACT origin (scheme+host): the public web
# origin (DGX_WEB) by default. A key registered there will NOT work from
# another origin (e.g. a LAN address over http, different scheme/host).
# Changing WEBAUTHN_RP_ID invalidates every registered key; the name does not.
WEBAUTHN_ORIGIN  = os.environ.get('WEBAUTHN_ORIGIN', PUBLIC_WEB_URL)
WEBAUTHN_RP_ID   = os.environ.get('WEBAUTHN_RP_ID',
                                  WEBAUTHN_ORIGIN.split('//', 1)[-1].split('/', 1)[0].split(':', 1)[0])
WEBAUTHN_RP_NAME = os.environ.get('WEBAUTHN_RP_NAME', 'DGX')
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
