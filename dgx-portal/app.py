import os, re, time, requests
from flask import Flask, request, session, redirect, url_for, flash, g, jsonify, Response
from datetime import datetime, timedelta
from urllib.parse import urlparse

from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.secret_key = os.environ['SECRET_KEY']

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
# Secure: cookie sent only over HTTPS. Enabled via env (=1) when a
#   TLS reverse proxy sits in front (dgx.cronos.website via Traefik).
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', '0') == '1',
    # Werkzeug parses the multipart BEFORE our application guards (the CSRF guard reads
    # request.form on each POST). Without a cap, an unauthenticated multi-GB POST
    # writes to disk before any check. 16 MB covers the
    # largest legitimate uploads (OCR/video image 15 MB); beyond that Werkzeug
    # returns 413 without parsing anything.
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
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
    # L'aperçu HTML pose SA PROPRE politique (bac à sable) : la remplacer par
    # celle du portail rendrait de nouveau ses scripts inertes.
    if resp.headers.get('Content-Security-Policy', '').startswith('sandbox'):
        return resp
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'same-origin')
    resp.headers.setdefault('Content-Security-Policy', _CSP)
    # HSTS: ignored over HTTP, applied behind Traefik's TLS.
    resp.headers.setdefault('Strict-Transport-Security', 'max-age=63072000; includeSubDomains')
    return resp


# ── CSRF protection (per-session token) ──────────────────────────────────────
# Each session carries a token; every unsafe request (POST/PUT/PATCH/DELETE)
# must send it back via the hidden `csrf_token` field (forms) or the
# X-CSRFToken header (fetch/JSON calls). Defense in depth on top of SameSite=Lax.

# Socle d'authentification (CSRF, secours, LDAP, anti-force-brute, session) :
# cf. auth.py. Les deux crochets ci-dessous y ont perdu leur decorateur, qui
# aurait exige l'objet `app` — on les enregistre donc ici, explicitement.
from auth import (  # noqa: E402
    LOGIN_LOCK, LOGIN_MAX_FAILS, LOGIN_WINDOW, USERNAME_RE, _admin_username_cache,
    _apply_session, _client_ip, _csrf_protect, _ensure_csrf,
    _inject_csrf, _is_admin_group, _login_fail, _login_locked,
    _login_reset, _revoke_current_session, _revoke_user_sessions,
    is_admin_username,
    ldap_authenticate, ldap_lookup_admin, ldap_lookup_email,
)
app.before_request(_csrf_protect)
app.context_processor(_inject_csrf)
# Configuration : cf. config.py (2e piece du noyau partage, avec db.py).
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



from db import (  # noqa: E402  (cf. commentaire plus bas)
    DB_PATH, _spend_conn, close_db, get_db, get_setting, init_db,
    maintenance_active, set_setting,
)

# ── SSO / OIDC (Authentik) ───────────────────────────────────────────────────

oauth = None
if OIDC_ENABLED:
    from authlib.integrations.flask_client import OAuth
    oauth = OAuth(app)
    oauth.register(
        name='authentik',
        server_metadata_url=OIDC_METADATA_URL,
        client_id=OIDC_CLIENT_ID,
        client_secret=OIDC_CLIENT_SECRET,
        client_kwargs={'scope': 'openid profile email'},
    )

# ── DB ─────────────────────────────────────────────────────────────────────

# get_db / close_db / DB_PATH vivent dans db.py depuis le 28/08 : ils sont le
# noyau partage par les modules extraits du monolithe, et un module importe par
# app.py ne peut pas reimporter app.py.
app.teardown_appcontext(close_db)
# _sse_msg / maintenance_block_sse : cf. guards.py
from guards import _sse_msg, maintenance_block_sse  # noqa: E402

# maintenance_block_json / media_rate_block / _chat_rate_limited : cf. guards.py
from guards import (  # noqa: E402
    CHAT_RATE_MAX, CHAT_RATE_WINDOW, _chat_rate_limited,
    maintenance_block_json, media_rate_block,
)

# Client LiteLLM (cles, budgets, comptes) : cf. litellm_client.py
from litellm_client import (  # noqa: E402
    _ensure_litellm_user, _infos_cles, _litellm_user_info, create_litellm_key,
    get_user_keys, litellm_headers, litellm_update_user_budget, revoke_litellm_key,
)

# get_running_models / _rm_cache : cf. vllm_health.py
from vllm_health import get_running_models  # noqa: E402

# Annonces (enregistrement + diffusion) : cf. announcements.py
from announcements import _announce_launch, add_announcement  # noqa: E402

# Runner et sidecars (etat, lancement, journaux, sondes) : cf. sidecars.py
from sidecars import (  # noqa: E402
    _drop_log_noise, _image_launch, _mem_guard, _music_launch, _ocr_launch,
    _runner_headers, _sidecar_action, _sidecar_proc_status, _sidecar_start_json,
    VOICE_REPO_IDS, _sidecar_status, _voice_launch, asr_is_up, get_image_model,
    get_music_model,
    get_ocr_model, get_voice_model, image_ready, music_ready, runner_launch,
    runner_logs, runner_metrics, runner_status, runner_stop,
    IMAGE_MODEL_IDS, _HF_ID_RE, _LOG_NOISE_RE,
)

# ── ComfyUI (generation video MiniMax H3) ────────────────────────────────────
# Deplace dans comfyui_client.py le 28/08 : cette section n'avait qu'une seule
# dependance (COMFYUI_URL, issue de l'environnement), c'etait donc la coupure la
# moins risquee du monolithe. Les routes /api/video/* restent ici.
from comfyui_client import (  # noqa: E402
    comfyui_is_up,
    comfyui_generate, comfyui_status, comfyui_fetch_video,
    _comfyui_output_file, _local_video_path, _cache_video_local,
    VIDEO_FILES_DIR, COMFYUI_OUTPUT_DIR,
)

# Sonde vLLM (sante, debit, contexte) + recherche HF : cf. vllm_health.py
from vllm_health import (  # noqa: E402
    GB10_TAG, _CTX_FLAG, _SEARCH_PAGE_SIZE, _SEQS_FLAG, _prom_sum, ctx_of, ctx_split,
    effective_ctx,
    guess_engine, max_seqs_of, search_hf_models, vllm_health,
)

# Notifications (mail admin, webhook Discord) : cf. notify.py
from notify import (  # noqa: E402
    notify_budget_discord, notify_budget_email, notify_discord, notify_email,
    notify_media_request_email, send_user_email,
)

# ── Notifications Discord ────────────────────────────────────────────────────
# Deplacees dans discord_notify.py le 28/08 (cf. db.py et config.py).
from discord_notify import _discord_announce, discord_broadcast  # noqa: E402

# ── Gardes d'authentification ────────────────────────────────────────────────
# Deplacees dans auth.py le 28/08 : un blueprint doit pouvoir les importer sans
# reimporter app.py. Cf. la docstring d'auth.py.
from auth import (  # noqa: E402
    SESSION_MAX_AGE, _API_FETCH_PATHS, _is_api_request, _session_expired,
    admin_required, login_required,
)

# ── Local users managed by the admin (local_users table) ─────────────────────
# Comptes locaux (authentification, budget) : cf. local_users.py
from local_users import (  # noqa: E402
    _local_group, _local_user_auth, _local_user_effective_budget,
    _local_user_is_admin, _parse_budget, _record_user_source,
    _sync_local_user_budget,
)

# ── Discord account linking (OAuth2 "identify") ──────────────────────────────
# ── Liaison de compte Discord ────────────────────────────────────────────────
# Blueprint : cf. discord_routes.py (28/08).
from discord_routes import bp as discord_bp  # noqa: E402

# ── Reglages utilisateur ─────────────────────────────────────────────────────
# Blueprint : cf. settings_routes.py (28/08).
from settings_routes import bp as settings_bp  # noqa: E402

# ── Historique des conversations du playground ───────────────────────────────
# Blueprint : cf. conversation_routes.py (28/08).
from conversation_routes import (  # noqa: E402
    CONVERSATIONS_MAX, CONV_MAX_CHARS, MSG_MAX_CHARS, bp as conversations_bp,
)

# ── Support (assistant IA) ───────────────────────────────────────────────────
# Assistant Support (outils, execution, contexte) : cf. support.py
from support import (  # noqa: E402
    _clean_reply, _exec_support_tool, _mask_key, _sse_tool_event,
    _support_context, _support_tools, _user_extra_tools,
    GUARDED_TOOLS, SUPPORT_SYSTEM, TOOL_LABELS, _exec_mcp_tool, _exec_skill,
    _support_tool_target,
)

# ── Memoire : graphe de connaissances par utilisateur ────────────────────────
# Premier blueprint sorti du monolithe (28/08) : cf. memory_routes.py.
from memory_routes import bp as memory_bp  # noqa: E402

# ── Apercu d'une page HTML generee ───────────────────────────────────────────
# Blueprint : cf. preview_routes.py (28/08).
from preview_routes import bp as preview_bp  # noqa: E402

# ── Chat (playground + support), en flux SSE ─────────────────────────────────
# Blueprint : cf. chat_routes.py (28/08).
from chat_routes import bp as chat_bp  # noqa: E402

# ── Statistiques de consommation (base LiteLLM Postgres) ─────────────────────
# Statistiques (agregats, classements, utilisateurs actifs) : cf. stats.py
from stats import (  # noqa: E402
    _account_activity, _active_users, _inflight_end, _inflight_start,
    _real_tokens_by_user,
    ranking_full, user_hourly,
)

# ── Administration (modeles, sidecars, comptes, annonces) ────────────────────
# Blueprint : cf. admin_routes.py (28/08). L'endpoint `admin` devient
# `admin.admin` ; les 34 url_for du projet, tous dans ce bloc, ont suivi.
from admin_routes import bp as admin_bp  # noqa: E402

# ── Video (MiniMax H3 via ComfyUI) ──
# Blueprint : cf. video_routes.py (28/08).
from video_routes import bp as video_bp  # noqa: E402

# ── Generation d'image (sidecar diffusers) ──
# Blueprint : cf. image_routes.py (28/08).
from image_routes import bp as image_bp  # noqa: E402  (get_image_model/image_ready deja importes de sidecars)

# ── Musique (sidecar diffusers) ──
# Blueprint : cf. music_routes.py (28/08).
from music_routes import bp as music_bp  # noqa: E402  (get_music_model/music_ready deja importes de sidecars)

# ── OCR ──────────────────────────────────────────────────────────────────────
# Blueprint : cf. ocr_routes.py (28/08). Frontiere redessinee — voir sa docstring.
from ocr_routes import bp as ocr_bp  # noqa: E402  (get_ocr_model deja importe de sidecars)

# ── Voix et dictee ───────────────────────────────────────────────────────────
# Frontiere redessinee le 28/08 : cette banniere couvrait voix + dictee +
# amorcage. La voix part dans voice_routes.py, la dictee dans asr_routes.py,
# l'amorcage reste ci-dessous.
from voice_routes import (  # noqa: E402
    bp as voice_bp, get_voice_engine, get_voice_languages,
)

from asr_routes import bp as asr_bp, asr_is_up  # noqa: E402
from webauthn_routes import (  # noqa: E402
    bp as webauthn_bp, _webauthn_enabled, start_login,
)

# ── Recherche web depuis le playground ───────────────────────────────────────
# Deplacee dans websearch_tools.py le 28/08 (cf. db.py pour le noyau partage).
from websearch_tools import (  # noqa: E402
    _phase_outils, _recherche_pertinente, _texte_des_trouvailles, websearch_active,
)


# get_setting / set_setting / maintenance_active : cf. db.py (noyau partage).





# init_db (schema + migrations) : cf. db.py

# ── LDAP ────────────────────────────────────────────────────────────────────




# ── Helpers ─────────────────────────────────────────────────────────────────






# VOICE_REPO_IDS : cf. sidecars.py (liste blanche des variantes lancables)









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






@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'username' in session:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        ip  = _client_ip()
        key = f"{ip}|{username}"
        ukey = f"user:{username}"
        # Compteur par username, indépendant de l'IP : un attaquant qui change
        # d'IP à chaque essai reste sous le seuil par IP, mais le compteur du
        # compte cumule toutes les tentatives → il finit par se verrouiller.
        wait = _login_locked(key) or _login_locked(ip) or _login_locked(ukey)
        if wait:
            flash(f"Trop de tentatives. Réessaie dans {wait // 60 + 1} min.", "danger")
            return ('', 401)
        # Local accounts managed by the admin (local_users table, hashed) — checked
        # before LDAP so as not to depend on its availability.
        l_ok, l_admin, l_name = _local_user_auth(username, password)
        if l_ok:
            _login_reset(key); _login_reset(ip); _login_reset(ukey)
            _record_user_source(username, 'local', l_name)
            if _webauthn_enabled(username):
                # 2e facteur : mot de passe valide MAIS pas encore de session.
                payload = start_login(username, l_name, l_admin, 'local')
                return jsonify({'webauthn_required': True,
                                'publicKey': payload['publicKey'],
                                'nonce': payload['nonce']})
            _apply_session(username, l_name, l_admin, via_sso=False)
            return redirect(_safe_next(request.args.get('next')))
        ok, is_admin, fullname = ldap_authenticate(username, password)
        if ok:
            _login_reset(key); _login_reset(ip); _login_reset(ukey)
            _record_user_source(username, 'ldap', fullname)
            if _webauthn_enabled(username):
                payload = start_login(username, fullname, is_admin, 'ldap')
                return jsonify({'webauthn_required': True,
                                'publicKey': payload['publicKey'],
                                'nonce': payload['nonce']})
            _apply_session(username, fullname, is_admin, via_sso=False)
            return redirect(_safe_next(request.args.get('next')))
        _login_fail(key); _login_fail(ip); _login_fail(ukey)
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










@app.context_processor
def inject_budget_alert():
    """In-app banner when the account budget exceeds 85% (non-admins)."""
    if 'username' not in session or session.get('is_admin'):
        return {}
    if hasattr(g, '_budget_alert'):
        return {'budget_alert': g._budget_alert}
    alert = None
    try:
        info = _litellm_user_info(session['username'])
        if info['exists'] and info['max_budget']:
            pct = (info['spend'] or 0) / info['max_budget'] * 100
            if pct >= 85:
                alert = {'pct': round(pct),
                         'remaining': max(info['max_budget'] - (info['spend'] or 0), 0)}
    except Exception:
        pass
    g._budget_alert = alert
    return {'budget_alert': alert}


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

    username = (userinfo.get('preferred_username') or userinfo.get('nickname')
                or (userinfo.get('email') or '').split('@')[0]
                or userinfo.get('sub') or '').strip().lower()
    # preferred_username / nickname / email are claims MODIFIABLE by
    # the user in many IdPs. This value becomes session['username'],
    # which is the ownership key of ALL the app's data (API keys,
    # MCP servers, skills, conversations, LiteLLM quotas): without the same
    # filter as the LDAP path, an SSO account that renames itself "mboitel" would
    # be assigned mboitel's data. So we apply USERNAME_RE.
    if not username or not USERNAME_RE.match(username):
        flash("SSO : identifiant de profil invalide ou manquant.", "danger")
        return redirect(url_for('login'))
    fullname = userinfo.get('name') or username

    groups = userinfo.get('groups')
    if isinstance(groups, list):
        # Authentik returns group names ("adm_cronos"); _is_admin_group
        # also covers the case where it would be a full DN.
        is_admin = any(g == OIDC_ADMIN_GROUP or _is_admin_group(g) for g in groups)
    else:
        # 'groups' claim absent → we fall back on an LDAP lookup by uid.
        is_admin = ldap_lookup_admin(username)

    nxt = session.pop('sso_next', None)
    _record_user_source(username, 'sso', fullname)
    _apply_session(username, fullname, is_admin, via_sso=True)
    return redirect(_safe_next(nxt))


app.register_blueprint(discord_bp)


# POST only: on GET, any third-party page could log the user out
# with a simple <img src="https://.../logout">, outside the CSRF
# guard (which only covers unsafe methods).
@app.route('/logout', methods=['POST'])
def logout():
    was_sso = session.get('sso')
    # Révoque la session côté serveur (registre) AVANT de vider le cookie :
    # même un cookie volé/rejoué ne pourra plus être utilisé après logout.
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
    """Jours couverts par la fenêtre budgétaire (« 1d », « 7d », « 30d »,
    « 3 mois »…). Défaut raisonnable : 1 jour si non parsable."""
    s = str(duration or "").lower()
    if "mois" in s or "month" in s:
        return 30
    digits = re.sub(r"\D", "", s)
    try:
        return max(1, int(digits)) if digits else 1
    except ValueError:
        return 1


# Cache court : /api/home est pollé souvent, on ne re-scinde pas SpendLogs à
# chaque refresh (même logique de TTL que user_hourly dans stats.py).
_BUDGET_CACHE = {}
_BUDGET_TTL = 60


def _budget_remaining(username, default_budget, duration):
    """(used, remaining) tokens réels de l'utilisateur sur la fenêtre budgétaire."""
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
    budget_used, budget_remaining = _budget_remaining(session['username'], default_budget, budget_duration)
    return dict(running_models=running, my_requests=my_requests,
                public_api_url=PUBLIC_API_URL, auto_model=AUTO_MODEL_NAME,
                usage=user_hourly(session['username']),
                sysmetrics=runner_metrics(),
                sidecar_metrics=metrics,
                modelhealth=vllm_health(),
                active_users=_active_users() if session.get('is_admin') else None,
                budget_tokens=f"{default_budget:,.0f}".replace(',', ' '),
                budget_duration=budget_duration,
                budget_used=budget_used,
                budget_remaining=budget_remaining)


@app.route('/')
@login_required
def index():
    # The page itself is rendered by the Next.js frontend (data via /api/home)
    # — this endpoint only stays registered because url_for('index') is used
    # throughout as a redirect target (login, request_model, admin_required).
    return ('', 204)


@app.route('/healthz')
def healthz():
    """Liveness minimale, publique — pour les healthchecks / sondes de
    disponibilité. Ne révèle rien d'interné."""
    return jsonify({'ok': True, 'time': int(time.time())})


@app.route('/metrics')
def prom_metrics():
    """Exposition Prometheus (texte) — pour scraper avec Grafana.

    Publique par choix : c'est le standard pour un pull de métriques, et on le
    branche sur le réseau LAN/netbird (jamais exposé sur l'internet). Reprend le
    payload du runner sans dupliquer la collecte (déjà mise en cache côté lui).
    """
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


def _service_reachable(url, expect=(200, 401), timeout=3):
    """True si le service répond (statut dans `expect`), sinon False."""
    try:
        r = requests.get(url, timeout=timeout)
        return r.status_code in expect
    except requests.RequestException:
        return False


@app.route('/api/health')
@login_required
def api_health():
    """État agrégé des services (diagnostic). Les services média sont
    on-demand : leur absence n'invalide pas la santé globale."""
    chat = get_running_models()
    runner_up = _service_reachable(f"{RUNNER_URL}/status")
    litellm_up = _service_reachable(f"{LITELLM_URL}/health", expect=(200,))
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
    return jsonify({'username': session.get('username'), 'fullname': session.get('fullname'),
                     'is_admin': bool(session.get('is_admin')),
                     'avatar_id': pref['avatar_id'] if pref else None,
                     'theme_id': (pref['theme_id'] if pref else None) or 'neutral',
                     'lang': (pref['lang'] if pref else None) or 'en',
                     # Absence de ligne user_prefs = compte qui n'a jamais rien
                     # réglé, donc jamais vu la prise en main.
                     'onboarded': bool(pref['onboarded']) if pref else False,
                     'maintenance_mode': maintenance_active()})


@app.route('/api/onboarding/done', methods=['POST'])
@login_required
def api_onboarding_done():
    """Marque la prise en main comme vue, une fois pour toutes, pour ce compte."""
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


@app.route('/api/pending-count')
@login_required
def api_pending_count():
    """Nombre de demandes (modèle + budget) en attente — badge de la sidebar."""
    db = get_db()
    if session.get('is_admin'):
        n = db.execute(
            "SELECT (SELECT COUNT(*) FROM model_requests WHERE status='pending')"
            " + (SELECT COUNT(*) FROM budget_requests WHERE status='pending')"
        ).fetchone()[0]
    else:
        n = db.execute(
            "SELECT (SELECT COUNT(*) FROM model_requests WHERE status='pending' AND username=?)"
            " + (SELECT COUNT(*) FROM budget_requests WHERE status='pending' AND username=?)",
            (session['username'], session['username']),
        ).fetchone()[0]
    return jsonify({'count': int(n or 0)})


# Catégories média sur lesquelles un utilisateur peut demander le lancement d'un
# modèle. Un "request" n'a de sens que si AUCUN modèle de la catégorie n'est
# chargé : sinon le bouton ne sert à rien (la page permet déjà de générer).
_MEDIA_CATEGORIES = {
    'image', 'music', 'video', 'ocr', 'voice',
}


def _media_category_running(category):
    """True si un modèle de la catégorie est déjà chargé (mêmes capteurs que
    _index_data, pour ne pas dupliquer la notion de « disponible »)."""
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
    """Signale à l'admin qu'un utilisateur veut un modèle de la catégorie donnée.
    Refuse si un modèle de cette catégorie est déjà chargé (défense en
    profondeur : le frontend cache déjà le bouton dans ce cas)."""
    data = request.get_json(silent=True) or {}
    category = (data.get('category') or '').strip().lower()
    user = session['username']
    if category not in _MEDIA_CATEGORIES:
        return jsonify({'error': {'message': 'Catégorie inconnue.'}}), 400
    if _media_category_running(category):
        return jsonify({'error': {'message':
                       f"Un modèle « {category} » est déjà chargé."}}), 409
    # Anti-spam : une seule demande par (utilisateur, catégorie) dans la
    # fenêtre MEDIA_REQUEST_COOLDOWN_S, même après navigation/refresh (le
    # verrou côté frontend se réinitialise, celui-ci non).
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
        if new_key:
            db = get_db()
            db.execute(
                "INSERT OR REPLACE INTO api_keys (username, key_alias, key_value, created_at) VALUES (?,?,?,?)",
                (session['username'], alias, new_key, datetime.now().isoformat())
            )
            db.commit()
            flash("Clé créée !", "success")
        else:
            flash("Erreur lors de la création de la clé.", "danger")
    elif action == 'revoke':
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
            flash("Clé introuvable.", "danger")
        elif revoke_litellm_key(k):
            db.execute("DELETE FROM api_keys WHERE key_value=? AND username=?",
                       (k, session['username']))
            db.commit()
            flash("Clé révoquée.", "success")
        else:
            flash("Erreur lors de la révocation.", "danger")
    elif action == 'request_budget':
        reason  = request.form.get('reason', '').strip()
        current = _litellm_user_info(session['username']).get('max_budget')
        db = get_db()
        existing = db.execute(
            "SELECT id FROM budget_requests WHERE username=? AND status='pending'",
            (session['username'],)
        ).fetchone()
        if existing:
            flash("Tu as déjà une demande en attente.", "warning")
            return ('', 204)
        db.execute(
            "INSERT INTO budget_requests (username, fullname, key_alias, current_budget, reason, status, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (session['username'], session['fullname'], '(compte)', current, reason, 'pending',
             datetime.now().isoformat())
        )
        db.commit()
        notify_budget_discord(session['username'], session['fullname'], '(compte)', current, reason)
        notify_budget_email(session['username'], session['fullname'], '(compte)', current, reason)
        flash("Demande de tokens envoyée !", "success")
    return ('', 204)


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
    return jsonify({
        'user_keys': get_user_keys(session['username']),
        'budget_tokens': f"{default_budget:,.0f}".replace(',', ' '),
        'budget_duration': get_setting('default_key_duration', KEY_DURATION),
        'budget_used': _budget_remaining(session['username'], default_budget, get_setting('default_key_duration', KEY_DURATION))[0],
        'budget_remaining': _budget_remaining(session['username'], default_budget, get_setting('default_key_duration', KEY_DURATION))[1],
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
    query = request.args.get('q', '').strip()
    task  = request.args.get('task', 'text-generation')
    gb10  = request.args.get('all') != '1'
    try:
        skip = max(0, int(request.args.get('skip', 0)))
    except ValueError:
        skip = 0
    results = search_hf_models(query, task, gb10_only=gb10, skip=skip) if (query or gb10) else []
    return jsonify({'results': results, 'query': query, 'task': task, 'gb10_only': gb10,
                    'skip': skip, 'page_size': _SEARCH_PAGE_SIZE})


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
    data = ranking_full(period, me=session['username'])
    return jsonify({'rows': data['rows'], 'active_count': data['active_count'], 'period': period,
                     'period_label': RANKING_LABELS[period], 'prev_label': RANKING_PREV_LABELS[period]})

@app.route('/request', methods=['GET', 'POST'])
@login_required
def request_model():
    # GET /request: the page is rendered by the Next.js frontend — only
    # the POST action below remains used (postForm from request/page.tsx).
    if request.method != 'POST':
        return ('', 204)
    model_id = request.form['model_id'].strip()
    reason   = request.form.get('reason', '').strip()
    if not model_id:
        flash("L'identifiant du modèle est requis.", "warning")
        return ('', 204)
    db = get_db()
    existing = db.execute(
        "SELECT id FROM model_requests WHERE username=? AND model_id=? AND status='pending'",
        (session['username'], model_id)
    ).fetchone()
    if existing:
        flash("Tu as déjà une demande en attente pour ce modèle.", "warning")
        return ('', 204)
    db.execute(
        "INSERT INTO model_requests (username, fullname, model_id, reason, status, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (session['username'], session['fullname'], model_id, reason, 'pending',
         datetime.now().isoformat())
    )
    db.commit()
    notify_discord(model_id, session['username'], session['fullname'], reason)
    notify_email(model_id, session['username'], session['fullname'], reason)
    flash(f"Demande envoyée pour « {model_id} » !", "success")
    return ('', 204)

# Usage par sidecar (administration) : cf. stats.py



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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)


