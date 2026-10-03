import os, re, time, requests
from flask import Flask, request, session, redirect, url_for, flash, g, jsonify, Response
from datetime import datetime, timedelta
from urllib.parse import urlparse

from werkzeug.middleware.proxy_fix import ProxyFix

_SECRET_FAIBLES = {'changeme', 'secret', 'dev', 'test', 'password', 'changeme!'}

app = Flask(__name__)

# The session-cookie signing key is the portal's most critical secret: the Flask
# cookie is SIGNED but not encrypted, so whoever knows the key can forge
# `{'username': 'admin', 'is_admin': True}` and impersonate that account.
# `os.environ[...]` already failed if the variable was missing, but accepted any
# value — including the `changeme` of `.env.example`, with which a forged
# session is accepted (verified on a throwaway app). So we refuse at startup,
# loudly, rather than at exploitation time.
_SECRET_KEY = os.environ['SECRET_KEY']
if len(_SECRET_KEY) < 32 or _SECRET_KEY.strip().lower() in _SECRET_FAIBLES:
    raise RuntimeError(
        "SECRET_KEY absente ou non aléatoire : il faut au moins 32 caractères "
        "aléatoires (elle est générée par setup.sh dans .env — ne jamais "
        "recopier la valeur d'exemple).")
app.secret_key = _SECRET_KEY

# Behind Traefik (TLS terminated at the proxy, forwarded as HTTP to the container):
# trust the X-Forwarded-* headers so Flask knows the real
# scheme (https) and the external host (dgx.cronos.website).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ── Session hardening ────────────────────────────────────────────────────────
# HttpOnly: the session cookie is not readable in JS (anti-theft via XSS).
# SameSite=Lax: the cookie is not sent on cross-site requests of type
#   POST/sub-resource (→ protects against CSRF on POST routes), BUT it IS sent
#   on a top-level GET navigation — which is needed so the OIDC
#   return (Authentik → /api/oauth2-redirect) recovers the OAuth state in session.
# Secure: cookie sent only over HTTPS. Production is TLS-gated (Cloudflare →
# Traefik), so the SECURE default is on. A LAN-only box reached over plain
# HTTP (http://dgx.cronos.lan:5000) must set SESSION_COOKIE_SECURE=0 in .env,
# otherwise the browser won't send the cookie over HTTP and login breaks there.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', '1') != '0',
    # Werkzeug parses the multipart BEFORE our application guards (the CSRF guard reads
    # request.form on each POST). Without a cap, an unauthenticated multi-GB POST
    # writes to disk before any check. 16 MB covers the
    # largest legitimate uploads (OCR/video image 15 MB); beyond that Werkzeug
    # returns 413 without parsing anything.
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    # Werkzeug 3.1 caps the memory of ONE form field at 500 Ko
    # (`MAX_FORM_MEMORY_SIZE`), independently of MAX_CONTENT_LENGTH. Yet
    # conversations are saved as form-data (`POST /conversations`, `messages`
    # field) with an intended 2 Mo cap: any slightly long conversation was thus
    # refused with 413 BEFORE reaching the route, and since the frontend swallows
    # the failure, it simply vanished on reload. 4 Mo covers CONV_MAX_CHARS (2 M)
    # plus the serialization margin, while staying well under MAX_CONTENT_LENGTH.
    MAX_FORM_MEMORY_SIZE=4 * 1024 * 1024,
)

# LDAP identifier validation regex (defense in depth against
# filter/DN injection, on top of escaping).


# Flask no longer serves any HTML document (the Jinja templates are removed,
# `grep render_template` is empty): only JSON, redirects and
# files. The old server UI's 'unsafe-inline' and cdn.jsdelivr.net therefore
# serve no purpose and need not allow inline script on the
# responses relayed through the Next proxy (which itself sets a nonce CSP).
_CSP = ("default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")

@app.after_request
def _security_headers(resp):
    # The HTML preview sets its OWN policy (sandbox): replacing it with the
    # portal's would render its scripts inert again.
    if resp.headers.get('Content-Security-Policy', '').startswith('sandbox'):
        return resp
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'same-origin')
    resp.headers.setdefault('Content-Security-Policy', _CSP)
    # Powerful features the app does not use, denied. `microphone` (dictation)
    # and `camera` (photo capture offered by file inputs on mobile) stay for
    # 'self' — denying them would silently break those two flows.
    resp.headers.setdefault('Permissions-Policy',
                            'geolocation=(), payment=(), usb=(), '
                            'camera=(self), microphone=(self)')
    # HSTS: ignored over HTTP, applied behind Traefik's TLS.
    resp.headers.setdefault('Strict-Transport-Security', 'max-age=63072000; includeSubDomains')
    return resp


# ── Re-export surface, do NOT "clean up" ─────────────────────────────────────
# The `from <module> import (...)` blocks below (lines ~100 to 335) bring back
# here the names the monolith carried before the extraction into modules:
# app.py remains the WSGI entry point (`gunicorn app:app`) AND the module that
# operations scripts import (`scripts/create-demo-account.py` does
# `import app as portal`). pyflakes flags them as "imported but unused" since
# app.py does not use them itself: this is expected, and removing them would
# break a `from app import <name>` outside the repo without changing behavior.


# ── CSRF protection (per-session token) ──────────────────────────────────────
# Each session carries a token; every unsafe request (POST/PUT/PATCH/DELETE)
# must send it back via the hidden `csrf_token` field (forms) or the
# X-CSRFToken header (fetch/JSON calls). Defense in depth on top of SameSite=Lax.

# Authentication foundation (CSRF, fallback, LDAP, anti-brute-force, session):
# see auth.py. The two hooks below lost their decorator there, which would have
# required the `app` object — so we register them here, explicitly.
from auth import (  # noqa: E402
    LOGIN_LOCK, LOGIN_MAX_FAILS, LOGIN_WINDOW, USERNAME_RE, _admin_username_cache,
    _apply_session, _client_ip, _csrf_protect, _ensure_csrf,
    _inject_csrf, _is_admin_group, _login_fail, _login_locked,
    _login_reset, _revoke_current_session, _revoke_user_sessions,
    est_bloque, is_admin_username,
    ldap_authenticate, ldap_lookup_admin, ldap_lookup_email,
    ldap_resolve_sso_identity,
)
app.before_request(_csrf_protect)
app.context_processor(_inject_csrf)
# Configuration: see config.py (2nd piece of the shared core, with db.py).
from config import (  # noqa: E402
    AUTO_MODEL_NAME, AVATAR_IDS, AVATAR_LABELS, LANGS, THEME_IDS, VLLM_API_BASE,
    LDAP_URI, LDAP_BASE, LDAP_BIND_DN, LDAP_BIND_PW,
    LITELLM_URL, LITELLM_KEY, VLLM_API,
    RUNNER_URL, RUNNER_TOKEN, COMFYUI_URL, OCR_URL,
    VOICE_URL, ASR_URL, MUSIC_URL, DISCORD_WH,
    DISCORD_BOT_TOKEN, DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_REDIRECT_URI,
    DISCORD_LINK_ENABLED, DISCORD_API, SMTP_HOST, SMTP_PORT,
    SMTP_USER, SMTP_PASS, SMTP_FROM, ADMIN_EMAIL,
    KEY_BUDGET, KEY_DURATION, PUBLIC_API_URL, LITELLM_DB_URL,
    MEDIA_REQUEST_COOLDOWN_S,
    LOCAL_TZ, OIDC_METADATA_URL, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET,
    OIDC_REDIRECT_URI, OIDC_LOGOUT_URL, OIDC_ADMIN_GROUP, OIDC_ENABLED,
)
# Account auth: local_users (hashed) → LDAP → SSO, in that order. The
# plaintext debug/file fallback was removed (see git history at the 4a59c6f
# migration): its 13 accounts are now local_users entries, so nothing is lost.



from db import (  # noqa: E402  (see comment below)
    DB_PATH, _spend_conn, close_db, get_db, get_setting, init_db, log_audit,
    maintenance_active, notification_unread, set_setting,
)

# ── SSO / OIDC (Authentik) ───────────────────────────────────────────────────

oauth = None
if OIDC_ENABLED:
    from authlib.integrations.flask_client import OAuth
    oauth = OAuth(app)
    # gjallarhorn (Cloudflare) returns 403 to non-browser User-Agents,
    # including authlib's when it fetches the OIDC metadata. So we fetch it
    # ourselves (browser UA) and pass the endpoints directly (via **_md);
    # `user_agent` covers the later token/userinfo calls.
    _md = {}
    try:
        import requests as _requests
        _md = _requests.get(OIDC_METADATA_URL, timeout=15,
                            headers={'User-Agent': 'Mozilla/5.0 (Cronos portal)'}).json()
    except Exception as e:
        app.logger.warning("SSO: metadata OIDC injoignable (%s)", e)
    oauth.register(
        name='authentik',
        client_id=OIDC_CLIENT_ID,
        client_secret=OIDC_CLIENT_SECRET,
        server_metadata_url=(OIDC_METADATA_URL if not _md else None),
        client_kwargs={'scope': 'openid profile email'},
        user_agent='Mozilla/5.0 (Cronos portal)',
        **_md,
    )

# ── DB ─────────────────────────────────────────────────────────────────────

# get_db / close_db / DB_PATH live in db.py since 28/08: they are the core
# shared by the modules extracted from the monolith, and a module imported by
# app.py cannot re-import app.py.
app.teardown_appcontext(close_db)
# _sse_msg / maintenance_block_sse : cf. guards.py
from guards import _sse_msg, maintenance_block_sse  # noqa: E402

# maintenance_block_json / media_rate_block / _chat_rate_limited : cf. guards.py
from guards import (  # noqa: E402
    CHAT_RATE_MAX, CHAT_RATE_WINDOW, _chat_rate_limited,
    maintenance_block_json, media_rate_block,
)

# LiteLLM client (keys, budgets, accounts): see litellm_client.py
from litellm_client import (  # noqa: E402
    _ensure_litellm_user, _infos_cles, _litellm_user_info, create_litellm_key,
    get_user_keys, litellm_headers, litellm_update_user_budget, renommer_cle_litellm,
    revoke_litellm_key,
)

# get_running_models / _rm_cache : cf. vllm_health.py
from vllm_health import get_running_models  # noqa: E402

# Announcements (recording + broadcast): see announcements.py
from announcements import _announce_launch, add_announcement  # noqa: E402

# Runner and sidecars (state, launch, logs, probes): see sidecars.py
from sidecars import (  # noqa: E402
    _drop_log_noise, _image_launch, _mem_guard, _music_launch, _ocr_launch,
    _runner_headers, _sidecar_action, _sidecar_proc_status, _sidecar_start_json,
    VOICE_REPO_IDS, _sidecar_status, _voice_launch, get_image_model,
    get_music_model,
    get_ocr_model, get_voice_model, image_ready, music_ready, runner_launch,
    runner_logs, runner_metrics, runner_status, runner_stop,
    IMAGE_MODEL_IDS, _HF_ID_RE, _LOG_NOISE_RE,
)

# ── ComfyUI (MiniMax H3 video generation) ────────────────────────────────────
# Moved to comfyui_client.py on 28/08: this section had a single dependency
# (COMFYUI_URL, from the environment), so it was the least risky cut from the
# monolith. The /api/video/* routes stay here.
from comfyui_client import (  # noqa: E402
    comfyui_is_up,
    comfyui_generate, comfyui_status, comfyui_fetch_video,
    _comfyui_output_file, _local_video_path, _cache_video_local,
    VIDEO_FILES_DIR, COMFYUI_OUTPUT_DIR,
)

# vLLM probe (health, throughput, context) + HF search: see vllm_health.py
from vllm_health import (  # noqa: E402
    GB10_TAG, HF_TASKS, HfIndisponible, _CTX_FLAG, _SEARCH_PAGE_SIZE, _SEQS_FLAG,
    _prom_sum, ctx_of, ctx_split, effective_ctx, guess_engine, hf_jeton_present,
    hf_modele_hors_gb10, max_seqs_of, search_hf_models_page, vllm_health,
)

# Notifications (admin mail, Discord webhook): see notify.py
from notify import (  # noqa: E402
    notify_budget_discord, notify_budget_email, notify_discord, notify_email,
    notify_media_request_email, send_user_email,
)

# ── Notifications Discord ────────────────────────────────────────────────────
# Moved to discord_notify.py on 28/08 (see db.py and config.py).
from discord_notify import _discord_announce, discord_broadcast  # noqa: E402

# ── Authentication guards ────────────────────────────────────────────────────
# Moved to auth.py on 28/08: a blueprint must be able to import them without
# re-importing app.py. See the docstring of auth.py.
from auth import (  # noqa: E402
    SESSION_MAX_AGE, _API_FETCH_PATHS, _is_api_request, _session_expired,
    admin_required, login_required,
)

# ── Local users managed by the admin (local_users table) ─────────────────────
# Local accounts (authentication, budget): see local_users.py
from local_users import (  # noqa: E402
    _local_group, _local_user_auth, _local_user_effective_budget,
    _local_user_is_admin, _parse_budget, _record_user_source,
    _sync_local_user_budget, gestion_mot_de_passe,
)

# ── Discord account linking (OAuth2 "identify") ──────────────────────────────
# ── Discord account linking ──────────────────────────────────────────────────
# Blueprint: see discord_routes.py (28/08).
from discord_routes import bp as discord_bp  # noqa: E402

# ── User settings ────────────────────────────────────────────────────────────
# Blueprint: see settings_routes.py (28/08).
from settings_routes import bp as settings_bp  # noqa: E402

# ── Playground conversation history ──────────────────────────────────────────
# Blueprint: see conversation_routes.py (28/08).
from conversation_routes import (  # noqa: E402
    CONVERSATIONS_MAX, CONV_MAX_CHARS, MSG_MAX_CHARS, bp as conversations_bp,
)

# ── Support (AI assistant) ───────────────────────────────────────────────────
# Support assistant (tools, execution, context): see support.py
from support import (  # noqa: E402
    _clean_reply, _exec_support_tool, _mask_key, _sse_tool_event,
    _support_context, _support_tools, _user_extra_tools,
    GUARDED_TOOLS, SUPPORT_SYSTEM, TOOL_LABELS, _exec_mcp_tool, _exec_skill,
    _support_tool_target,
)

# ── Memory: per-user knowledge graph ─────────────────────────────────────────
# First blueprint out of the monolith (28/08): see memory_routes.py.
from memory_routes import bp as memory_bp  # noqa: E402

# ── Preview of a generated HTML page ─────────────────────────────────────────
# Blueprint: see preview_routes.py (28/08).
from preview_routes import bp as preview_bp  # noqa: E402

# ── Chat (playground + support), SSE streaming ───────────────────────────────
# Blueprint: see chat_routes.py (28/08).
from chat_routes import bp as chat_bp  # noqa: E402

# ── Usage statistics (LiteLLM Postgres database) ─────────────────────────────
# Statistics (aggregates, rankings, active users): see stats.py
from stats import (  # noqa: E402
    RANKING_METRICS, _account_activity, _active_users, _inflight_end, _inflight_start,
    _real_tokens_by_user, _tokens_by_model,
    ranking_full, user_hourly,
)

# ── Administration (models, sidecars, accounts, announcements) ───────────────
# Blueprint: see admin_routes.py (28/08). The `admin` endpoint becomes
# `admin.admin`; the project's 34 url_for calls, all in this block, followed.
from admin_routes import bp as admin_bp  # noqa: E402

# ── Video (MiniMax H3 via ComfyUI) ──
# Blueprint: see video_routes.py (28/08).
from video_routes import bp as video_bp  # noqa: E402

# ── Image generation (diffusers sidecar) ──
# Blueprint: see image_routes.py (28/08).
from image_routes import bp as image_bp  # noqa: E402  (get_image_model/image_ready already imported from sidecars)

# ── Music (diffusers sidecar) ──
# Blueprint: see music_routes.py (28/08).
from music_routes import bp as music_bp  # noqa: E402  (get_music_model/music_ready already imported from sidecars)

# ── OCR ──────────────────────────────────────────────────────────────────────
# Blueprint: see ocr_routes.py (28/08). Boundary redrawn — see its docstring.
from ocr_routes import bp as ocr_bp  # noqa: E402  (get_ocr_model already imported from sidecars)

# ── Voice and dictation ──────────────────────────────────────────────────────
# Boundary redrawn on 28/08: this banner covered voice + dictation + bootstrapping.
# Voice goes to voice_routes.py, dictation to asr_routes.py,
# bootstrapping stays below.
from voice_routes import (  # noqa: E402
    bp as voice_bp, get_voice_engine, get_voice_languages,
)

from asr_routes import bp as asr_bp, asr_is_up  # noqa: E402
from webauthn_routes import (  # noqa: E402
    bp as webauthn_bp, _webauthn_enabled, start_login,
)

# ── Web search from the playground ───────────────────────────────────────────
# Moved to websearch_tools.py on 28/08 (see db.py for the shared core).
from websearch_tools import (  # noqa: E402
    _phase_outils, _recherche_pertinente, _texte_des_trouvailles, websearch_active,
)


# get_setting / set_setting / maintenance_active: see db.py (shared core).





# init_db (schema + migrations): see db.py

# ── LDAP ────────────────────────────────────────────────────────────────────




# ── Helpers ─────────────────────────────────────────────────────────────────






# VOICE_REPO_IDS: see sidecars.py (allowlist of launchable variants)









# ── OCR (baidu/Unlimited-OCR by default; chandra-ocr-2 also supported) ──────
# Internal container (dedicated ocr_net network, cf. README "Security"), never a
# published port.
#
# chandra-ocr-2 (datalab-to) has a completely different input/output contract
# from Unlimited-OCR: instead of a free prompt + "label [x,y,x,y]text" lines,
# it expects a fixed STRUCTURED prompt and replies in HTML with
# data-label/data-bbox attributes (bbox as spaced "x0 y0 x1 y1", always 0-1000). Text
# copied verbatim from chandra/prompts.py (OCR_LAYOUT_PROMPT on the model side) —
# rewording it would break the output format expected by the front-end parser.







# ── Routes ──────────────────────────────────────────────────────────────────

# ── Login brute-force protection (persisted in the DB) ──────────────────────
# Stored in SQLite and not in process memory: with gunicorn -w 2, an
# in-RAM counter is local to each worker (so 2× the allowed attempts,
# depending on which worker gets the request) and resets on each
# redeploy — two trivial ways to bypass the lockout.




@app.route('/api/config')
def api_config():
    return jsonify({'oidc_enabled': OIDC_ENABLED})






def _refus_si_bloque(username):
    """Refuse a blocked account, AFTER credential verification.

    Placed after, and not before: a valid password must be presented to learn
    that one is blocked, so a blocked account cannot be used to enumerate
    existing accounts. And the person concerned receives a truthful message,
    instead of an « identifiants incorrects » that would make them retry
    endlessly. The block is set by an admin (Users → Bloquer): it is the ONLY
    lever for an LDAP/SSO account, which has no row in local_users.
    """
    if not est_bloque(username):
        return None
    log_audit(username, 'login.refuse', 'compte bloqué par un administrateur')
    message = "Accès révoqué pour ce compte. Contacte un administrateur."
    flash(message, "danger")
    # JSON body in addition to the status: the login page displays the message
    # as-is, otherwise a blocked account would think it was a typo and
    # retry indefinitely.
    return (jsonify({'ok': False, 'error': message, 'blocked': True}), 403)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'username' in session:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        # Capped BEFORE building the lock keys: `key`/`ukey` are written to the
        # database (`login_attempts`) as soon as a failure occurs, and `username`
        # comes from an unauthenticated form. Without this filter, a 4 Mo (the
        # field cap) fresh identifier wrote ~8 Mo of rows per request, with a
        # different key every time — so never locked — and the table is only
        # purged at startup. The format is the one accepted everywhere else
        # (USERNAME_RE): at most 64 characters.
        if not USERNAME_RE.match(username):
            return ('', 401)
        ip  = _client_ip()
        key = f"{ip}|{username}"
        ukey = f"user:{username}"
        # Counter per username, independent of the IP: an attacker who changes
        # IP on every attempt stays under the per-IP threshold, but the account
        # counter cumulates all attempts → it eventually locks.
        # NB: NO global per-IP lock (L2 audit) — behind a shared NAT
        # (VPN/office), an IP lock blocked EVERYONE after 6 cumulated failures.
        # The per-account lock (ukey) suffices against targeted brute-force;
        # the ip|username key caps an account from a given IP.
        wait = _login_locked(key) or _login_locked(ukey)
        if wait:
            flash(f"Trop de tentatives. Réessaie dans {wait // 60 + 1} min.", "danger")
            return ('', 401)
        # Local accounts managed by the admin (local_users table, hashed) — checked
        # before LDAP so as not to depend on its availability.
        l_ok, l_admin, l_name = _local_user_auth(username, password)
        if l_ok:
            refus = _refus_si_bloque(username)
            if refus:
                return refus
            _login_reset(key); _login_reset(ukey)
            _record_user_source(username, 'local', l_name, l_admin)
            if _webauthn_enabled(username):
                # 2nd factor: valid password BUT no session yet.
                payload = start_login(username, l_name, l_admin, 'local')
                return jsonify({'webauthn_required': True,
                                'publicKey': payload['publicKey'],
                                'nonce': payload['nonce']})
            _apply_session(username, l_name, l_admin, via_sso=False)
            return redirect(_safe_next(request.args.get('next')))
        ok, is_admin, fullname = ldap_authenticate(username, password)
        if ok:
            refus = _refus_si_bloque(username)
            if refus:
                return refus
            _login_reset(key); _login_reset(ukey)
            _record_user_source(username, 'ldap', fullname, is_admin)
            if _webauthn_enabled(username):
                payload = start_login(username, fullname, is_admin, 'ldap')
                return jsonify({'webauthn_required': True,
                                'publicKey': payload['publicKey'],
                                'nonce': payload['nonce']})
            _apply_session(username, fullname, is_admin, via_sso=False)
            return redirect(_safe_next(request.args.get('next')))
        _login_fail(key); _login_fail(ukey)
        flash("Identifiants incorrects.", "danger")
        return ('', 401)
    # GET /login: the page itself is rendered by the Next.js frontend
    # (app/login/page.tsx) — this branch is no longer reached in normal use.
    return ('', 204)


def _safe_next(target):
    """Only allow redirects to a local relative path — blocks
    the open redirect (?next=https://evil.com, //evil.com, or /\\evil.com that
    browsers normalize to //evil.com).
    """
    if not target or '\\' in target or '\t' in target or '\n' in target:
        return url_for('index')
    parsed = urlparse(target)
    # target[:2] in ('//','/\\'): blocks protocol-relative and backslash after /
    if (parsed.scheme or parsed.netloc or not target.startswith('/')
            or target[:2] in ('//', '/\\')):
        return url_for('index')
    return target










@app.route('/login/sso')
def login_sso():
    if not OIDC_ENABLED:
        flash("Le SSO n'est pas configuré.", "danger")
        return redirect(url_for('login'))
    session['sso_next'] = _safe_next(request.args.get('next'))
    return oauth.authentik.authorize_redirect(OIDC_REDIRECT_URI or url_for('oauth_callback', _external=True))


@app.route('/api/oauth2-redirect')
def oauth_callback():
    if not OIDC_ENABLED:
        return redirect(url_for('login'))
    try:
        token = oauth.authentik.authorize_access_token()
    except Exception:
        flash("Échec de la connexion SSO. Réessaie.", "danger")
        return redirect(url_for('login'))

    userinfo = token.get('userinfo') or {}
    if not userinfo:
        try:
            userinfo = oauth.authentik.userinfo(token=token)
        except Exception:
            userinfo = {}

    # ── Identity binding (fail closed) ────────────────────────────────────────
    # `session['username']` is the ownership key of ALL the app's data (API keys,
    # MCP servers, skills, conversations, LiteLLM quotas). It must therefore come
    # from an AUTHENTIC source, not from user-editable OIDC claims:
    # `preferred_username`/`nickname`/`email` are modifiable by the user in many
    # IdPs, so trusting them would let an SSO account rename itself "someone-else" and
    # inherit that account. We resolve the canonical identity from the LDAP
    # directory, keyed on the immutable `sub` (Authentik → LDAP `uid`) with a
    # fallback to the verified unique `email` (→ LDAP `mail`). If the directory
    # does not recognise the caller, we refuse the login rather than trust the
    # token's own username.
    sub = (userinfo.get('sub') or '').strip()
    email = (userinfo.get('email') or '').strip().lower()
    # Falling back to the e-mail address is only allowed if the IdP does not
    # declare it NOT verified (audit of 2026-10-02). `ldap_resolve_sso_identity`
    # resolves by `sub` first and falls back to `(mail=…)`: on an IdP that lets a
    # user set a colleague's address, this fallback opened the colleague's
    # session — yet `session['username']` is the ownership key of EVERYTHING (API
    # keys, MCP, conversations, quota) and carries `is_admin` via `last_is_admin`.
    # An ABSENT claim leaves the behavior unchanged (many IdPs do not send it,
    # and refusing it would cut everyone's SSO login); only an EXPLICIT `false`
    # closes the fallback. So this is not a full hardening: requiring
    # `email_verified: true` for the fallback remains to be decided.
    if userinfo.get('email_verified') is False and email:
        log_audit('sso', 'sso.email_non_verifie',
                  f'repli sur l\'adresse refusé pour sub={sub[:64]} (email_verified=false)')
        email = ''
    username, is_admin, fullname = ldap_resolve_sso_identity(sub, email)
    if username is None:
        flash("SSO : identité non reconnue dans le répertoire.", "danger")
        return redirect(url_for('login'))
    username = username.lower()
    if not USERNAME_RE.match(username):
        flash("SSO : identifiant de profil invalide ou manquant.", "danger")
        return redirect(url_for('login'))
    fullname = fullname or (userinfo.get('name') or username)

    groups = userinfo.get('groups')
    if isinstance(groups, list):
        # Authentik returns group names ("adm_cronos"); _is_admin_group
        # also covers the case where it would be a full DN. The directory
        # lookup above already gave us is_admin from memberOf; the `groups`
        # claim is treated as a secondary signal only when present.
        is_admin = is_admin or any(g == OIDC_ADMIN_GROUP or _is_admin_group(g) for g in groups)

    if _refus_si_bloque(username):
        # The password does not come into play here: identity is proven by the
        # directory, but the portal's refusal stands. The URL flag carries the
        # message to the login page, which is rendered by Next.js
        # (a Flask flash would be displayed nowhere).
        return redirect(url_for('login', refus='bloque'))
    nxt = session.pop('sso_next', None)
    _record_user_source(username, 'sso', fullname, is_admin)
    _apply_session(username, fullname, is_admin, via_sso=True)
    return redirect(_safe_next(nxt))


app.register_blueprint(discord_bp)


# POST only: on GET, any third-party page could log the user out
# with a simple <img src="https://.../logout">, outside the CSRF
# guard (which only covers unsafe methods).
@app.route('/logout', methods=['POST'])
def logout():
    was_sso = session.get('sso')
    # Revokes the session server-side (registry) BEFORE emptying the cookie:
    # even a stolen/replayed cookie can no longer be used after logout.
    _revoke_current_session()
    session.clear()
    # RP-initiated logout: if the user logged in via SSO, we also
    # send them to Authentik's end-session to close the IdP session.
    if was_sso and OIDC_LOGOUT_URL:
        return redirect(OIDC_LOGOUT_URL)
    return redirect(url_for('login'))

_SIDECAR_METRICS_CACHE = {}
_SIDECAR_METRICS_TTL = 3.0

def _sidecar_metrics(kind):
    """Home-page metrics for a media backend (OCR/video/voice): today's
    generations, total, and average/last generation time measured over the last 20
    jobs that carry a duration (jobs prior to the measure have duration_ms
    NULL and are therefore ignored). Global (platform activity), not scoped per
    user: these are counters and timings, nothing confidential.
    """
    # Global platform counters (not per-user) that update slowly. Each kind is
    # ~3-5 read-only queries on the media tables, and /api/home polls this on
    # every refresh — a short per-process cache spares that recurring scan.
    now = time.time()
    hit = _SIDECAR_METRICS_CACHE.get(kind)
    if hit and now - hit[0] < _SIDECAR_METRICS_TTL:
        return hit[1]
    tbl = {'ocr': 'ocr_jobs', 'video': 'video_jobs', 'voice': 'voice_jobs'}.get(kind)
    if not tbl:
        return None
    db = get_db()
    today = datetime.now().strftime('%Y-%m-%d')
    count_today = db.execute(f"SELECT COUNT(*) FROM {tbl} WHERE created_at >= ?", (today,)).fetchone()[0]
    total = db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    # 20 most recent jobs that carry a duration: basis for the averages (time, throughputs).
    recent = db.execute(
        f"SELECT * FROM {tbl} WHERE duration_ms IS NOT NULL ORDER BY id DESC LIMIT 20").fetchall()
    durs = [r['duration_ms'] for r in recent if r['duration_ms']]
    m = {'count_today': count_today, 'total': total,
         'avg_ms': round(sum(durs) / len(durs)) if durs else None,
         'last_ms': durs[0] if durs else None}

    def _avg(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    if kind in ('ocr', 'voice'):
        cps = _avg([len(r['text']) * 1000.0 / r['duration_ms']
                    for r in recent if r['duration_ms'] and r['text']])
        m['chars_per_s'] = round(cps) if cps else None
    if kind == 'ocr':
        m['chars_avg'] = round(sum(len(r['text']) for r in recent) / len(recent)) if recent else None
    if kind == 'voice':
        # Real-time factor: seconds of audio produced / seconds of compute.
        rtf = _avg([r['audio_ms'] * 1.0 / r['duration_ms']
                    for r in recent if r['audio_ms'] and r['duration_ms']])
        m['rtf'] = round(rtf, 1) if rtf else None
    if kind == 'video':
        fin = {r['status']: r['c'] for r in db.execute(
            f"SELECT status, COUNT(*) c FROM {tbl} WHERE status IN ('done','error') GROUP BY status")}
        finished = fin.get('done', 0) + fin.get('error', 0)
        m['success_rate'] = round(100 * fin.get('done', 0) / finished) if finished else None
        m['video_secs_today'] = db.execute(
            f"SELECT SUM(req_duration_s) FROM {tbl} WHERE created_at >= ? AND req_duration_s IS NOT NULL",
            (today,)).fetchone()[0]
        # Seconds of compute per second of video produced (real-time factor).
        gpv = _avg([(r['duration_ms'] / 1000.0) / r['req_duration_s']
                    for r in recent if r['req_duration_s'] and r['duration_ms']])
        m['gen_per_vsec'] = round(gpv, 1) if gpv else None
    _SIDECAR_METRICS_CACHE[kind] = (time.time(), m)
    return m


def _budget_period_days(duration):
    """Days covered by the budget window (« 1d », « 7d », « 30d »,
    « 3 mois »…). Sensible default: 1 day if unparseable."""
    s = str(duration or "").lower()
    if "mois" in s or "month" in s:
        return 30
    digits = re.sub(r"\D", "", s)
    try:
        return max(1, int(digits)) if digits else 1
    except ValueError:
        return 1


# Short cache: /api/home is polled often, we do not re-slice SpendLogs on
# every refresh (same TTL logic as user_hourly in stats.py).
_BUDGET_CACHE = {}
_BUDGET_TTL = 60


def _budget_remaining(username, default_budget, duration):
    """(used, remaining) real tokens of the user over the budget window."""
    now = time.time()
    hit = _BUDGET_CACHE.get(username)
    if hit and now - hit[0] < _BUDGET_TTL:
        return hit[1]
    since = datetime.utcnow() - timedelta(days=_budget_period_days(duration))
    used = int(_real_tokens_by_user(since).get(username, 0) or 0)
    remaining = max(0, int(default_budget - used))
    _BUDGET_CACHE[username] = (now, (used, remaining))
    return used, remaining


def _index_data():
    running = [{'name': m, 'kind': 'chat', 'exposed': True} for m in get_running_models()]
    metrics = {}
    ocr_model = get_ocr_model()
    if ocr_model:
        running.append({'name': ocr_model, 'kind': 'ocr', 'exposed': False})
        metrics['ocr'] = _sidecar_metrics('ocr')
    if comfyui_is_up():
        running.append({'name': 'MiniMax-H3', 'kind': 'video', 'exposed': False})
        metrics['video'] = _sidecar_metrics('video')
    if image_ready():
        running.append({'name': get_image_model() or 'Image', 'kind': 'image', 'exposed': False})
    if music_ready():
        running.append({'name': get_music_model() or 'Musique', 'kind': 'music', 'exposed': False})
    voice_model = get_voice_model()
    if voice_model:
        _vlabel = 'Qwen3-TTS' if get_voice_engine() == 'qwen3-tts' else 'Chatterbox'
        running.append({'name': f'{_vlabel} ({voice_model})', 'kind': 'voice', 'exposed': False})
        metrics['voice'] = _sidecar_metrics('voice')
    db = get_db()
    my_requests = db.execute(
        "SELECT * FROM model_requests WHERE username=? ORDER BY created_at DESC LIMIT 5",
        (session['username'],)
    ).fetchall()
    default_budget = float(get_setting('default_key_budget', KEY_BUDGET))
    budget_duration = get_setting('default_key_duration', KEY_DURATION)
    # The envelope that ACTUALLY blocks lives at LiteLLM (it carries the admin
    # grants that only touch this account). It takes precedence over the portal
    # calculation (local override → group → default), used as fallback if the
    # account does not exist yet on the LiteLLM side. Without this, a limit
    # granted by the admin was never reflected on the usage card.
    _litellm_ui = _litellm_user_info(session['username'])
    effective = _litellm_ui.get('max_budget')
    if effective is None:
        mu = db.execute("SELECT * FROM local_users WHERE username=?",
                        (session['username'],)).fetchone()
        effective = _local_user_effective_budget(mu) if mu else default_budget
    budget_used, budget_remaining = _budget_remaining(session['username'], effective, budget_duration)
    # Date of the envelope's next reset: shown to the user so they know WHEN
    # they get quota back (otherwise « dépassé » seems eternal).
    budget_reset_at = (_litellm_ui.get('budget_reset_at') or '')[:16].replace('T', ' ')
    return dict(running_models=running, my_requests=my_requests,
                public_api_url=PUBLIC_API_URL, auto_model=AUTO_MODEL_NAME,
                usage=user_hourly(session['username']),
                usage_by_model=_tokens_by_model(),
                sysmetrics=runner_metrics(),
                sidecar_metrics=metrics,
                modelhealth=vllm_health(),
                active_users=_active_users() if session.get('is_admin') else None,
                budget_tokens=f"{effective:,.0f}".replace(',', ' '),
                budget_duration=budget_duration,
                budget_used=budget_used,
                budget_remaining=budget_remaining,
                budget_reset_at=budget_reset_at,
                # The home card disables the « Demander plus de budget » button
                # while a request is pending (anti double-post).
                budget_request_pending=bool(db.execute(
                    "SELECT 1 FROM budget_requests WHERE username=? AND status='pending'",
                    (session['username'],)).fetchone()))


@app.route('/')
@login_required
def index():
    # The page itself is rendered by the Next.js frontend (data via /api/home)
    # — this endpoint only stays registered because url_for('index') is used
    # throughout as a redirect target (login, request_model, admin_required).
    return ('', 204)


@app.route('/healthz')
def healthz():
    """Minimal, public liveness endpoint — for healthchecks / availability
    probes. Reveals nothing internal."""
    return jsonify({'ok': True, 'time': int(time.time())})


@app.route('/metrics')
def prom_metrics():
    """Prometheus exposition (text) — for scraping with Grafana.

    Public by choice on the LAN/netbird network: this is the standard for a
    metrics pull. However it must NOT reach the internet, and yet it was
    reachable there: the frontend's catch-all rewrite relays `/:path*` to
    Flask, so /metrics was served at `dgx.cronos.website`
    (CPU/RAM/GPU/temperature/online model to whoever asked).

    Guard kept: a request that crosses the Cloudflare edge always carries the
    `Cf-Connecting-Ip` header (set by Cloudflare, normalized by the Traefik
    plugin) → we refuse. A LAN/netbird scrape, on the other hand, arrives
    without this header. Reuses the runner's payload without duplicating
    collection (already cached on its side).

    Known limit: a client reaching the origin directly (outside Cloudflare)
    does not send this header; this is the known edge bypass, handled at the
    router level, not here.
    """
    if request.headers.get('Cf-Connecting-Ip'):
        return Response("metrics: accès réservé au réseau local\n", status=403,
                        mimetype='text/plain')
    m = runner_metrics() or {}
    ram = m.get('ram') or {}
    gpu = m.get('gpu') or {}
    online = 1 if m.get('model_status') == 'running' else 0

    def _g(name, doc, value):
        return f"# HELP cronos_{name} {doc}\n# TYPE cronos_{name} gauge\ncronos_{name} {value}"

    parts = [
        _g('cpu_pct', 'CPU usage (%)', m.get('cpu_pct') if m.get('cpu_pct') is not None else 'NaN'),
        _g('ram_used_gb', 'Host RAM used (GB)', ram.get('used_gb', 'NaN')),
        _g('ram_total_gb', 'Host RAM total (GB)', ram.get('total_gb', 'NaN')),
        _g('gpu_util_pct', 'GPU utilisation (%)', gpu.get('util', 'NaN')),
        _g('gpu_power_w', 'GPU power draw (W)', gpu.get('power', 'NaN')),
        _g('gpu_temp_c', 'GPU temperature (C)', gpu.get('temp', 'NaN')),
        _g('model_online', 'Served chat model online (1/0)', online),
    ]
    return Response('\n'.join(parts) + '\n', mimetype='text/plain')


@app.route('/docs')
@login_required
def docs():
    """Documentation of the OpenAI-compatible API, standalone HTML page (no
    CDN dependency: the box is LAN/netbird). Lists the current models + the
    /v1/* endpoints with curl/Python/JS examples."""
    import html as _h
    models = get_running_models() or []
    model_rows = "".join(f'<code>{_h.escape(m)}</code><br>' for m in models)
    if not model_rows:
        model_rows = '<span style="color:#9ca3af">aucun modèle en cours</span>'
    base = _h.escape(PUBLIC_API_URL)
    auto = _h.escape(AUTO_MODEL_NAME)
    curl_chat = f"""curl {base}/v1/chat/completions \\
  -H "Authorization: Bearer $CRONOS_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{{"model": "{auto}", "messages": [{{"role": "user", "content": "Bonjour"}}]}}'"""
    py_chat = ('from openai import OpenAI\n'
               'client = OpenAI(base_url="' + base + '/v1", api_key="<ta clé>")\n'
               'r = client.chat.completions.create(model="' + auto + '",\n'
               '    messages=[{"role": "user", "content": "Bonjour"}])\n'
               'print(r.choices[0].message.content)')
    js_chat = ('import OpenAI from "openai";\n'
               'const client = new OpenAI({ baseURL: "' + base + '/v1", apiKey: "<ta clé>" });\n'
               'const r = await client.chat.completions.create({\n'
               '  model: "' + auto + '",\n'
               '  messages: [{ role: "user", content: "Bonjour" }]\n'
               '});\n'
               'console.log(r.choices[0].message.content);')
    html_doc = f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Docs API — Cronos</title></head>
<body style="margin:0;background:#f9fafb;color:#111827;font:15px/1.6 system-ui">
<div style="max-width:860px;margin:0 auto;padding:32px 20px">
<h1 style="font:700 26px system-ui;margin:0 0 4px">API Cronos</h1>
<p style="margin:0 0 24px;color:#6b7280">Compatible OpenAI — crée une clé puis appelle comme un provider classique. URL de base : <code>{base}</code></p>
<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;padding:20px 24px;margin:0 0 24px">
<h2 style="font:700 16px system-ui;margin:0 0 8px">Modèles actuellement servis</h2>
{model_rows}
<p style="margin:8px 0 0;color:#374151">L'alias <code>{auto}</code> pointe toujours vers le modèle de chat en cours : branche-le une fois, plus besoin de renommer.</p>
</div>
<h2 style="font:700 18px system-ui">GET /v1/models</h2>
<p style="color:#6b7280">Liste les modèles disponibles.</p>
<pre style="background:#0f172a;color:#e2e8f0;padding:12px 16px;border-radius:8px;overflow:auto">{_h.escape('curl ' + base + '/v1/models -H "Authorization: Bearer $CRONOS_KEY"')}</pre>
<h2 style="font:700 18px system-ui">POST /v1/chat/completions</h2>
<p style="color:#6b7280">Génération (streaming via <code>"stream": true</code>).</p>
<pre style="background:#0f172a;color:#e2e8f0;padding:12px 16px;border-radius:8px;overflow:auto">{_h.escape(curl_chat)}</pre>
<h3 style="font:700 15px system-ui">Python</h3>
<pre style="background:#0f172a;color:#e2e8f0;padding:12px 16px;border-radius:8px;overflow:auto">{_h.escape(py_chat)}</pre>
<h3 style="font:700 15px system-ui">JavaScript</h3>
<pre style="background:#0f172a;color:#e2e8f0;padding:12px 16px;border-radius:8px;overflow:auto">{_h.escape(js_chat)}</pre>
<p style="margin:32px 0 0;color:#9ca3af;font:12px system-ui">Gère tes clés depuis les réglages. Les tokens comptent sur ton budget de compte.</p>
</div></body></html>"""
    return Response(html_doc, mimetype='text/html')


@app.route('/api/notifications')
@login_required
def api_notifications():
    """User in-app notifications (bell, seen/unseen)."""
    rows = get_db().execute(
        "SELECT id, kind, title, seen, created_at FROM notifications "
        "WHERE username=? ORDER BY id DESC LIMIT 30", (session['username'],)).fetchall()
    items = [{'id': r['id'], 'kind': r['kind'], 'title': r['title'],
              'seen': bool(r['seen']), 'created_at': r['created_at']} for r in rows]
    return jsonify({'items': items, 'unread': notification_unread(session['username'])})


@app.route('/api/notifications/seen', methods=['POST'])
@login_required
def api_notifications_seen():
    db = get_db()
    db.execute("UPDATE notifications SET seen=1 WHERE username=? AND seen=0",
               (session['username'],))
    db.commit()
    return jsonify({'ok': True})


def _service_reachable(url, expect=(200, 401), timeout=3):
    """True if the service answers (status in `expect`), False otherwise."""
    try:
        r = requests.get(url, timeout=timeout)
        return r.status_code in expect
    except requests.RequestException:
        return False


@app.route('/api/health')
@login_required
def api_health():
    """Aggregated state of the services (diagnostic). Media services are
    on-demand: their absence does not invalidate overall health."""
    chat = get_running_models()
    runner_up = _service_reachable(f"{RUNNER_URL}/status")
    # `/health/liveliness` and not `/health`: since LiteLLM 1.102 (upgrade of
    # 2026-09-24) `/health` requires an API key and answers 401 without it — so
    # the probe reported « LiteLLM injoignable » while the proxy was running.
    # `liveliness` is public, costs nothing and does not probe the upstream
    # models, which matches exactly what this flag means here: « joignable ».
    litellm_up = _service_reachable(f"{LITELLM_URL}/health/liveliness", expect=(200,))
    return jsonify({
        'ok': bool(runner_up and litellm_up),
        'services': {
            'runner': {'reachable': runner_up},
            'litellm': {'reachable': litellm_up},
            'chat': {'running': chat, 'ready': bool(chat)},
            'video': {'ready': bool(comfyui_is_up()), 'on_demand': True},
            'ocr': {'ready': bool(get_ocr_model()), 'on_demand': True},
            'voice': {'ready': bool(get_voice_model()), 'on_demand': True},
            'image': {'ready': bool(image_ready()), 'on_demand': True},
            'music': {'ready': bool(music_ready()), 'on_demand': True},
        },
        'time': int(time.time()),
    })


@app.route('/api/whoami')
@login_required
def api_whoami():
    pref = get_db().execute(
        "SELECT avatar_id, theme_id, lang, onboarded FROM user_prefs WHERE username=?",
        (session.get('username'),)).fetchone()
    # Who holds this account's password: the portal, the LDAP directory, the
    # SSO provider, or nobody as far as we know. The UI uses it to show the
    # right form — or to say where the password is changed.
    gestion = gestion_mot_de_passe(session.get('username'))
    return jsonify({'username': session.get('username'), 'fullname': session.get('fullname'),
                     'is_admin': bool(session.get('is_admin')),
                     'avatar_id': pref['avatar_id'] if pref else None,
                     'theme_id': (pref['theme_id'] if pref else None) or 'neutral',
                     'lang': (pref['lang'] if pref else None) or 'fr',
                     # No user_prefs row = account that has never set
                     # anything, hence never seen the onboarding.
                     'onboarded': bool(pref['onboarded']) if pref else False,
                     # `local_account` remains for existing callers;
                     # `password_managed_by` says the same thing more precisely
                     # (an account can be local AND have logged in via SSO).
                     'local_account': gestion['local'],
                     'password_managed_by': gestion['gestion'],
                     'auth_sources': gestion['sources'],
                     'passkey_possible': gestion['passkey'],
                     'maintenance_mode': maintenance_active()})


@app.route('/api/onboarding/done', methods=['POST'])
@login_required
def api_onboarding_done():
    """Marks the onboarding as seen, once and for all, for this account."""
    db = get_db()
    db.execute("INSERT INTO user_prefs (username, onboarded) VALUES (?,1) "
               "ON CONFLICT(username) DO UPDATE SET onboarded=1",
               (session['username'],))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/home')
@login_required
def api_home():
    data = _index_data()
    data['my_requests'] = [dict(r) for r in data['my_requests']]
    return jsonify(data)


@app.route('/api/modelhealth')
@login_required
def api_modelhealth():
    """Health of the model ALONE, probed every second by the dashboard.

    /api/home is much heavier (spend aggregate, user requests, sidecar sensors)
    and stays at 5 s; only real-time throughput needed a fast pace, hence this
    minimal endpoint.
    """
    return jsonify(vllm_health())


@app.route('/api/pending-count')
@login_required
def api_pending_count():
    """Sidebar badges: MODEL and BUDGET requests, kept separate.

    A single mixed total stuck the same number on « Demander un modèle »
    and on « Admin »: a mere budget request showed « 1 » on the model
    request (bug reported on 2026-09-08)."""
    db = get_db()
    if session.get('is_admin'):
        model = db.execute(
            "SELECT COUNT(*) FROM model_requests WHERE status='pending'").fetchone()[0]
        budget = db.execute(
            "SELECT COUNT(*) FROM budget_requests WHERE status='pending'").fetchone()[0]
    else:
        model = db.execute(
            "SELECT COUNT(*) FROM model_requests WHERE status='pending' AND username=?",
            (session['username'],)).fetchone()[0]
        budget = db.execute(
            "SELECT COUNT(*) FROM budget_requests WHERE status='pending' AND username=?",
            (session['username'],)).fetchone()[0]
    return jsonify({'model': int(model or 0), 'budget': int(budget or 0)})


# Media categories for which a user can request a model launch.
# A "request" only makes sense if NO model of the category is loaded:
# otherwise the button is pointless (the page already allows generating).
_MEDIA_CATEGORIES = {
    'image', 'music', 'video', 'ocr', 'voice',
}


def _media_category_running(category):
    """True if a model of the category is already loaded (same sensors as
    _index_data, to avoid duplicating the notion of « disponible »)."""
    if category == 'image':
        return image_ready()
    if category == 'music':
        return music_ready()
    if category == 'video':
        return comfyui_is_up()
    if category == 'ocr':
        return bool(get_ocr_model())
    if category == 'voice':
        return bool(get_voice_model())
    return False


@app.route('/api/model/request', methods=['POST'])
@login_required
def api_model_request():
    """Reports to the admin that a user wants a model of the given category.
    Refuses if a model of this category is already loaded (defense in
    depth: the frontend already hides the button in that case)."""
    data = request.get_json(silent=True) or {}
    category = (data.get('category') or '').strip().lower()
    user = session['username']
    if category not in _MEDIA_CATEGORIES:
        return jsonify({'error': {'message': 'Catégorie inconnue.'}}), 400
    if _media_category_running(category):
        return jsonify({'error': {'message':
                       f"Un modèle « {category} » est déjà chargé."}}), 409
    # Anti-spam: only one request per (user, category) within the
    # MEDIA_REQUEST_COOLDOWN_S window, even after navigation/refresh (the
    # frontend-side lock resets, this one does not).
    now = time.time()
    db = get_db()
    row = db.execute(
        "SELECT created_at FROM media_request_cooldown "
        "WHERE username=? AND category=?", (user, category)).fetchone()
    if row and (now - row['created_at']) < MEDIA_REQUEST_COOLDOWN_S:
        retry_after = int(MEDIA_REQUEST_COOLDOWN_S - (now - row['created_at']))
        remaining = retry_after // 60 + 1
        return jsonify({'error': {'message':
                       f"Déjà signalé. Nouvelle demande possible dans ≈ {remaining} min."},
                        'cooldown': True, 'retry_after': retry_after}), 429
    db.execute(
        "INSERT INTO media_request_cooldown (username, category, created_at) "
        "VALUES (?,?,?) ON CONFLICT(username, category) "
        "DO UPDATE SET created_at=excluded.created_at",
        (user, category, now))
    db.commit()
    email_sent = notify_media_request_email(
        category, user, session.get('fullname', ''))
    return jsonify({'ok': True, 'category': category, 'email_sent': bool(email_sent)})

@app.route('/keys', methods=['GET', 'POST'])
@login_required
def keys():
    # GET /keys: the page itself is rendered by the Next.js frontend
    # (data via /api/keys) — only the POST actions below remain
    # used (postForm("/keys", ...) from app/(app)/keys/page.tsx).
    # POST: actions of the « Clés API » tab. They answer JSON with an HONEST
    # status, like the admin actions (see CLAUDE.md): this route answered
    # `('', 204)` after a `flash(...)`, yet NO template renders flashes — so the
    # UI announced « Clé créée ! » / « Clé révoquée. » whatever happened. The
    # serious case was revocation: LiteLLM unreachable, the key stayed VALID
    # while the user believed it dead.
    if request.method != 'POST':
        return ('', 204)
    action = request.form.get('action')
    if action == 'create':
        raw_name = request.form.get('key_name', '').strip()
        if raw_name:
            alias = re.sub(r'[^a-zA-Z0-9_-]', '-', raw_name)[:40]
        else:
            alias = f"{session['username']}-{int(time.time())}"
        new_key = create_litellm_key(alias, session['username'], is_admin=session.get('is_admin', False))
        if not new_key:
            return jsonify({'ok': False,
                            'error': "La clé n'a pas pu être créée : le service de clés "
                                     "(LiteLLM) n'a pas répondu. Réessaie dans un instant."}), 503
        db = get_db()
        db.execute(
            "INSERT OR REPLACE INTO api_keys (username, key_alias, key_value, created_at) VALUES (?,?,?,?)",
            (session['username'], alias, new_key, datetime.now().isoformat())
        )
        db.commit()
        # The value goes back to the UI: it is the only moment it can be seen
        # without clicking « Afficher ».
        return jsonify({'ok': True, 'key_alias': alias, 'key': new_key})
    if action == 'revoke':
        k = request.form.get('key')
        db = get_db()
        # Verifies the key really belongs to the logged-in user BEFORE
        # revoking it on the LiteLLM side (anti-IDOR: otherwise any user could
        # revoke another's key by submitting its value).
        owns = db.execute(
            "SELECT 1 FROM api_keys WHERE key_value=? AND username=?",
            (k, session['username'])
        ).fetchone()
        if not owns:
            return jsonify({'ok': False, 'error': "Clé introuvable sur ce compte."}), 404
        if not revoke_litellm_key(k):
            return jsonify({'ok': False,
                            'error': "La clé n'a PAS pu être révoquée : LiteLLM n'a pas "
                                     "répondu. Elle est encore valide, réessaie."}), 503
        db.execute("DELETE FROM api_keys WHERE key_value=? AND username=?",
                   (k, session['username']))
        db.commit()
        return jsonify({'ok': True})
    if action == 'rename':
        k = request.form.get('key')
        raw_name = request.form.get('key_name', '').strip()
        db = get_db()
        # Same ownership check as revocation: without it, one could rename
        # someone else's key by submitting its value.
        owns = db.execute(
            "SELECT key_alias FROM api_keys WHERE key_value=? AND username=?",
            (k, session['username'])
        ).fetchone()
        if not owns:
            return jsonify({'ok': False, 'error': "Clé introuvable sur ce compte."}), 404
        # Same rules as creation, otherwise a 4000-character alias or one full
        # of line breaks would break the list display.
        alias = re.sub(r'[^a-zA-Z0-9_-]', '-', raw_name)[:40]
        if not alias:
            return jsonify({'ok': False, 'error': "Le nom doit contenir au moins un "
                                                  "caractère (lettres, chiffres, - ou _)."}), 400
        # An alias already carried by ANOTHER of your keys would make the list
        # ambiguous: that is exactly what the alias exists to avoid.
        doublon = db.execute(
            "SELECT 1 FROM api_keys WHERE username=? AND key_alias=? AND key_value<>?",
            (session['username'], alias, k)
        ).fetchone()
        if doublon:
            return jsonify({'ok': False, 'code': 'alias_deja_utilise',
                            'error': "Tu as déjà une clé nommée ainsi."}), 409
        # LiteLLM first: if we only renamed on our side, its console would keep
        # showing the old name, and the admin could no longer tell who owns a
        # given key.
        if not renommer_cle_litellm(k, alias):
            return jsonify({'ok': False,
                            'error': "Le nom n'a PAS pu être changé : LiteLLM n'a pas "
                                     "répondu. La clé est intacte, réessaie."}), 503
        db.execute("UPDATE api_keys SET key_alias=? WHERE key_value=? AND username=?",
                   (alias, k, session['username']))
        db.commit()
        return jsonify({'ok': True, 'key_alias': alias})
    if action == 'request_budget':
        # Capped at write time (audit of 2026-10-02): `/api/admin` re-reads the
        # last 200 FULL requests every 8 s, so a 4 Mio reason
        # (MAX_FORM_MEMORY_SIZE) made hundreds of MB serialize per response —
        # on unified memory where the OOM-killer targets the served model.
        reason  = request.form.get('reason', '').strip()[:500]
        current = _litellm_user_info(session['username']).get('max_budget')
        db = get_db()
        existing = db.execute(
            "SELECT id FROM budget_requests WHERE username=? AND status='pending'",
            (session['username'],)
        ).fetchone()
        if existing:
            # `code`: stable and frequent refusal, which the UI translates
            # (see CLAUDE.md § i18n) instead of displaying the French sentence.
            return jsonify({'ok': False, 'code': 'deja_en_attente',
                            'error': "Tu as déjà une demande en attente."}), 409
        db.execute(
            "INSERT INTO budget_requests (username, fullname, key_alias, current_budget, reason, status, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (session['username'], session['fullname'], '(compte)', current, reason, 'pending',
             datetime.now().isoformat())
        )
        db.commit()
        notify_budget_discord(session['username'], session['fullname'], '(compte)', current, reason)
        notify_budget_email(session['username'], session['fullname'], '(compte)', current, reason)
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': "Action inconnue."}), 400


@app.route('/api/keys')
@login_required
def api_keys():
    default_budget = float(get_setting('default_key_budget', KEY_BUDGET))
    acct = _litellm_user_info(session['username'])
    account = {
        'spend': acct['spend'],
        'max_budget': acct['max_budget'] if acct['exists'] else default_budget,
        'budget_reset_at': acct['budget_reset_at'],
        'unlimited': session.get('is_admin', False),
        'has_pending': bool(get_db().execute(
            "SELECT 1 FROM budget_requests WHERE username=? AND status='pending'",
            (session['username'],)).fetchone()),
    }
    model_limits = {}
    for row in get_db().execute("SELECT name, vllm_args, engine FROM model_configs"):
        # Same source as LiteLLM (ctx_split): on llama.cpp/ds4 the slot is
        # shared, so the context advertised to clients is the real input
        # (slot − output margin), not the raw slot. Avoids a snippet that
        # promises 256k/128k when the real budget is 192k/64k.
        max_in, max_out = ctx_split(row['vllm_args'], row['engine'] or 'vllm')
        if max_in:
            model_limits[row['name']] = {'context': max_in, 'output': max_out}
    # `auto-model` inherits the limits of the model actually running (cautious default
    # if nothing is launched), so the integration snippets are accurate.
    running = get_running_models()
    model_limits[AUTO_MODEL_NAME] = (model_limits.get(running[0]) if running else None) \
        or {'context': 262144, 'output': 131072}
    # A single read of the setting and ONE call to `_budget_remaining`: the same
    # response repeated them three and two times.
    duree = get_setting('default_key_duration', KEY_DURATION)
    budget_used, budget_remaining = _budget_remaining(session['username'], default_budget, duree)
    return jsonify({
        'user_keys': get_user_keys(session['username']),
        'budget_tokens': f"{default_budget:,.0f}".replace(',', ' '),
        'budget_duration': duree,
        'budget_used': budget_used,
        'budget_remaining': budget_remaining,
        'account': account,
        'model_limits': model_limits,
        'running_models': running,
        'auto_model': AUTO_MODEL_NAME,
        'public_api_url': PUBLIC_API_URL,
    })


app.register_blueprint(settings_bp)

app.register_blueprint(conversations_bp)
# ── Rate limit for chat endpoints ───────────────────────────────────────────
# The LiteLLM budget caps tokens, not the NUMBER of calls: a client that
# loops can monopolize the gunicorn threads (each SSE stream occupies one)
# and saturate the GPU without ever exceeding its quota. Simple sliding window,
# in the DB to be shared across workers, like the login lock.


@app.route('/api/csrf')
def api_csrf():
    # No login_required: the login page (unauthenticated) itself also
    # needs its own CSRF token, exactly like the server <meta>.
    return jsonify({'token': _ensure_csrf()})



app.register_blueprint(memory_bp)
app.register_blueprint(preview_bp)

app.register_blueprint(chat_bp)


@app.route('/api/search')
@login_required
def api_search():
    """Model search on Hugging Face.

    The contract is the same as the admin actions: the response says what
    really happened. An HF outage returns 503 with `ok: false` — never an
    empty list, which reads « ton modèle n'existe pas ».
    """
    query = request.args.get('q', '').strip()
    task  = request.args.get('task', '').strip()
    # The GB10 filter is now OPT-IN (2026-09-14). It was applied unconditionally,
    # and since the `gb10` tag marks only a handful of models tested on DGX
    # Spark, the whole rest of Hugging Face was invisible — the operator
    # concluded that search did not work (they were looking for Ornith-1.5,
    # which is not tagged). A filter nobody asked for must not decide the results.
    # `all=1`, the old parameter that disabled the filter, is still accepted so
    # an existing link or client does not break — it now does nothing.
    gb10  = request.args.get('gb10') == '1'
    try:
        skip = max(0, int(request.args.get('skip', 0)))
    except ValueError:
        skip = 0
    # An unknown filter makes HF answer 400: this is a call error, not an
    # outage — we say so, rather than disguising it as « aucun résultat ».
    if task and task not in HF_TASKS:
        return jsonify({'ok': False, 'results': [], 'error': f"Tâche inconnue : {task}."}), 400
    try:
        results, has_more = search_hf_models_page(query, task, gb10_only=gb10, skip=skip)
    except HfIndisponible as e:
        # `code` in addition to `error`: the portal is FR-first, but the UI can
        # translate that case (English only makes sense if it is complete), and
        # the technical detail stays attached for whoever must diagnose.
        return jsonify({'ok': False, 'results': [], 'code': 'hf_indisponible',
                        'error': f"Hugging Face n'a pas répondu ({e}). Réessaie dans un instant."}), 503
    # GB10 filter + no result: the user's next question is « est-ce que ça
    # existe ailleurs ? ». We answer it (True/False/None = unknown, and then the
    # UI says nothing).
    hors_gb10 = None
    if gb10 and query and not results:
        hors_gb10 = hf_modele_hors_gb10(query, task)
    return jsonify({'ok': True, 'results': results, 'query': query, 'task': task,
                    'gb10_only': gb10, 'skip': skip, 'page_size': _SEARCH_PAGE_SIZE,
                    'has_more': has_more, 'hors_gb10': hors_gb10,
                    # Presence of the token, NEVER its value: it tells the UI
                    # whether gated repositories can appear.
                    'hf_token': hf_jeton_present()})


RANKING_LABELS = {'day': "Aujourd'hui", 'week': '7 derniers jours', 'month': '30 derniers jours',
                  'year': '12 derniers mois', 'all': 'Depuis le début'}
RANKING_PREV_LABELS = {'day': 'hier', 'week': 'la semaine précédente', 'month': 'les 30 jours précédents',
                       'year': 'les 12 mois précédents', 'all': ''}
RANKING_PERIODS = tuple(RANKING_LABELS)



@app.route('/api/ranking')
@login_required
def api_ranking():
    period = request.args.get('period', 'day')
    if period not in RANKING_PERIODS:
        period = 'day'
    # The metric is validated here like the period: a ranking on anything other
    # than input, generation or their sum does not exist, and falling back to
    # the total beats answering an error to a page that opens.
    metric = request.args.get('metric', 'total')
    if metric not in RANKING_METRICS:
        metric = 'total'
    data = ranking_full(period, me=session['username'], metric=metric)
    # The avatar of each row. The ranking only named accounts; an avatar is
    # recognized faster than a nickname, and the admin list already showed it.
    # ONE read for the whole page (not one per row): `user_prefs` is
    # small, but the page refreshes often. A row absent from the table
    # keeps `None`, i.e. the avatar generated from the nickname — the default of
    # every account, see /api/whoami.
    avatars = {nom: av for nom, av in get_db().execute(
        "SELECT username, avatar_id FROM user_prefs").fetchall()}
    for ligne in data['rows']:
        ligne['avatar_id'] = avatars.get(ligne.get('username'))
    return jsonify({'rows': data['rows'], 'active_count': data['active_count'],
                    'period': period, 'metric': metric,
                    'total': data['total'], 'avg': data['avg'], 'has_prev': data['has_prev'],
                    'period_label': RANKING_LABELS[period], 'prev_label': RANKING_PREV_LABELS[period]})

@app.route('/request', methods=['GET', 'POST'])
@login_required
def request_model():
    # GET /request: the page is rendered by the Next.js frontend — only
    # the POST action below remains used (postForm from request/page.tsx).
    if request.method != 'POST':
        return ('', 204)
    # JSON first (sendJSON), form as fallback: the route was long called
    # form-encoded and nothing justifies breaking that path.
    donnees = request.get_json(silent=True) or request.form
    # Same bounds as the neighbouring routes (`admin_routes` :1203, `settings_routes`
    # :218): the type is checked along the way, a non-string JSON would otherwise
    # raise an AttributeError in 500 (audit of 2026-10-02).
    brut_id  = donnees.get('model_id')
    brut_motif = donnees.get('reason')
    model_id = (brut_id.strip() if isinstance(brut_id, str) else '')[:200]
    reason   = (brut_motif.strip() if isinstance(brut_motif, str) else '')[:500]
    if not model_id:
        return jsonify({'ok': False, 'code': 'identifiant_requis',
                        'error': "L'identifiant du modèle est requis."}), 400
    db = get_db()
    existing = db.execute(
        "SELECT id FROM model_requests WHERE username=? AND model_id=? AND status='pending'",
        (session['username'], model_id)
    ).fetchone()
    if existing:
        # Before: flash() + 204. The flash is rendered by no template, so the
        # page announced « Demande envoyée ! » while no row existed.
        return jsonify({'ok': False, 'code': 'deja_en_attente',
                        'error': f"Tu as déjà une demande en attente pour « {model_id} »."}), 409
    db.execute(
        "INSERT INTO model_requests (username, fullname, model_id, reason, status, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (session['username'], session['fullname'], model_id, reason, 'pending',
         datetime.now().isoformat())
    )
    db.commit()
    discord_ok = notify_discord(model_id, session['username'], session['fullname'], reason)
    email_ok = notify_email(model_id, session['username'], session['fullname'], reason)
    # The request IS recorded: a notification failure is a warning,
    # not an error — but it must show (the admin is warned via these channels).
    reponse = {'ok': True, 'message': f"Demande envoyée pour « {model_id} » !",
               'discord_sent': bool(discord_ok), 'email_sent': bool(email_ok)}
    if not (discord_ok or email_ok):
        reponse['warning'] = ("Demande enregistrée, mais l'admin n'a pu être prévenu "
                              "(ni Discord ni email).")
    return jsonify(reponse)

# Per-sidecar usage (administration): see stats.py



app.register_blueprint(admin_bp)
app.register_blueprint(video_bp)
app.register_blueprint(image_bp)
app.register_blueprint(music_bp)
app.register_blueprint(ocr_bp)
app.register_blueprint(voice_bp)
app.register_blueprint(asr_bp)
app.register_blueprint(webauthn_bp)

with app.app_context():
    init_db()
    # The avatar set went from generic shapes ("avatar-01"…) to AI
    # logos: we clear preferences pointing to a vanished id,
    # otherwise the <img> would hit a 404 for those accounts.
    _db = get_db()
    _db.execute(
        "UPDATE user_prefs SET avatar_id=NULL WHERE avatar_id IS NOT NULL "
        f"AND avatar_id NOT IN ({','.join('?' * len(AVATAR_IDS))})", AVATAR_IDS)
    # Purge of stale brute-force counters (window elapsed and no longer
    # locked) — otherwise the table grows indefinitely.
    _db.execute("DELETE FROM login_attempts WHERE locked_until < ? AND first_at < ?",
                (time.time(), time.time() - LOGIN_WINDOW))
    _db.commit()

def _start_grant_reaper():
    """Daemon thread: brings expired caps back to their base (every 60 s).

    Temporary grants (budget_grants) live on the LiteLLM side: without
    this sweep, an extra granted « pour 3 jours » would remain the limit
    forever as soon as nobody loads the portal. The first run at startup
    catches up on deadlines missed during an outage.

    CRONOS_NO_REAPER=1 keeps the thread from starting — the test-suite seam
    (same family as ASR_ONDEMAND_DIR). Measured 2026-10-02: the thread calls
    LiteLLM through `requests.post`, which several tests mock GLOBALLY, so a
    60-second wakeup landing inside one of those tests made it fail
    intermittently (« appel interdit » style assertions) — the flake that
    haunted the gate. A test must not depend on nobody else in the process
    making an HTTP call, and the suite has no real reaper work to do.
    """
    if os.environ.get('CRONOS_NO_REAPER') == '1':
        return
    import threading

    def _loop():
        # Local import: the thread starts at app.py import time, BEFORE the
        # admin routes module (mounted below) is loaded.
        from admin_routes import revert_expired_grants
        while True:
            try:
                with app.app_context():
                    ramenes = revert_expired_grants()
                    if ramenes:
                        app.logger.info("Subventions budgétaires expirées ramenées à la base : %s", ramenes)
            except Exception:
                app.logger.exception("reaper budget_grants")
            time.sleep(60)

    threading.Thread(target=_loop, daemon=True, name='budget-grant-reaper').start()


_start_grant_reaper()

# ASR reaper at STARTUP (2026-10-02), not only at the first dictation: after a
# reboot, `restart: unless-stopped` brings the `asr` container back and nothing
# stopped it before the first dictation + 10 min idle — 2.1 GiB of GPU held for
# nothing on a box that runs on fumes. Same test seam as the budget reaper:
# CRONOS_NO_REAPER=1 keeps the suite free of background threads. Idempotent —
# each gunicorn worker gets its own thread, exactly like the lazy start.
if os.environ.get('CRONOS_NO_REAPER') != '1':
    try:
        from sidecars import asr_demarrer_veilleur
        asr_demarrer_veilleur()
    except Exception:                                        # noqa: BLE001
        pass

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)


