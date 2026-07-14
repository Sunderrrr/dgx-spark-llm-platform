import os, sqlite3, smtplib, requests, time, re, threading, json, secrets, hmac
from flask import Flask, render_template, request, session, redirect, url_for, flash, g, jsonify, Response, stream_with_context, abort
from ldap3 import Server, Connection, ALL, SUBTREE, SIMPLE
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import escape_rdn
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from functools import wraps
from urllib.parse import urlparse
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.secret_key = os.environ['SECRET_KEY']

# Derrière Traefik (TLS terminé au proxy, forward en HTTP au conteneur) :
# fait confiance aux en-têtes X-Forwarded-* pour que Flask connaisse le vrai
# schéma (https) et l'hôte externe (dgx.cronos.website).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ── i18n ─────────────────────────────────────────────────────────────────────
# Les templates sont en français. PORTAL_LANG=fr → servis tels quels (instance de
# prod, aucune transformation). Sinon (défaut 'en') → le HTML rendu est traduit à
# la volée vers l'anglais via un catalogue FR→EN, avec des limites de mots pour ne
# jamais corrompre le HTML/JS (pas de remplacement à l'intérieur d'un mot).
import translations as _tr
PORTAL_LANG = os.environ.get('PORTAL_LANG', 'en').lower()
if PORTAL_LANG != 'fr' and _tr.FR_TO_EN:
    _TR_RE = re.compile('|'.join(
        '(?<!\\w)' + re.escape(k) + '(?!\\w)'
        for k in sorted(_tr.FR_TO_EN, key=len, reverse=True)))

    @app.after_request
    def _translate_html(resp):
        try:
            if (resp.content_type or '').startswith('text/html') and not resp.direct_passthrough:
                html = _TR_RE.sub(lambda m: _tr.FR_TO_EN[m.group(0)], resp.get_data(as_text=True))
                resp.set_data(html)
        except Exception:
            pass
        return resp

# ── Durcissement des sessions ────────────────────────────────────────────────
# HttpOnly : le cookie de session n'est pas lisible en JS (anti-vol via XSS).
# SameSite=Lax : le cookie n'est pas envoyé sur les requêtes cross-site de type
#   POST/sous-ressource (→ protège du CSRF sur les routes POST), MAIS il l'est
#   sur une navigation top-level GET — ce qui est nécessaire pour que le retour
#   OIDC (Authentik → /api/oauth2-redirect) retrouve l'état OAuth en session.
# Secure : cookie transmis uniquement en HTTPS. Activé via env (=1) quand un
#   reverse proxy TLS est devant (dgx.cronos.website via Traefik).
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', '0') == '1',
)

# Regex de validation des identifiants LDAP (défense en profondeur contre
# l'injection de filtre/DN, en plus de l'échappement).
USERNAME_RE = re.compile(r'^[a-zA-Z0-9._-]{1,64}$')


_CSP = ("default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "font-src 'self' https://cdn.jsdelivr.net; "
        "img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")

@app.after_request
def _security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'same-origin')
    resp.headers.setdefault('Content-Security-Policy', _CSP)
    # HSTS : ignoré en HTTP, appliqué derrière le TLS de Traefik.
    resp.headers.setdefault('Strict-Transport-Security', 'max-age=63072000; includeSubDomains')
    return resp


# ── Protection CSRF (jeton par session) ──────────────────────────────────────
# Chaque session porte un jeton ; toute requête non sûre (POST/PUT/PATCH/DELETE)
# doit le renvoyer via le champ caché `csrf_token` (formulaires) ou l'en-tête
# X-CSRFToken (appels fetch/JSON). Défense en profondeur en plus de SameSite=Lax.
@app.before_request
def _csrf_protect():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(32)
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        sent = request.form.get('csrf_token') or request.headers.get('X-CSRFToken', '')
        if not hmac.compare_digest(str(session['csrf']), str(sent)):
            abort(400, description='CSRF token manquant ou invalide.')


@app.context_processor
def _inject_csrf():
    return {'csrf_token': lambda: session.get('csrf', '')}

LDAP_URI      = os.environ.get('LDAP_URI', 'ldap://lldap.cronos.lan:3890')
LDAP_BASE     = os.environ.get('LDAP_BASE', 'dc=cronos,dc=website')
LDAP_BIND_DN  = os.environ.get('LDAP_BIND_DN', '')
LDAP_BIND_PW  = os.environ.get('LDAP_BIND_PW', '')
LITELLM_URL   = os.environ.get('LITELLM_URL', 'http://litellm:4000')
LITELLM_KEY   = os.environ.get('LITELLM_MASTER_KEY', '')
VLLM_API      = os.environ.get('VLLM_API_URL', 'http://host.docker.internal:8000/v1')
RUNNER_URL    = os.environ.get('VLLM_RUNNER_URL', 'http://host.docker.internal:8001')
RUNNER_TOKEN  = os.environ.get('RUNNER_TOKEN', '')
DISCORD_WH    = os.environ.get('DISCORD_WEBHOOK_URL', '')
SMTP_HOST     = os.environ.get('SMTP_HOST', '')
SMTP_PORT     = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER     = os.environ.get('SMTP_USER', '')
SMTP_PASS     = os.environ.get('SMTP_PASSWORD', '')
SMTP_FROM     = os.environ.get('SMTP_FROM', '')
ADMIN_EMAIL   = os.environ.get('ADMIN_EMAIL', '')
KEY_BUDGET    = float(os.environ.get('KEY_MAX_BUDGET', '0.002'))
KEY_DURATION  = os.environ.get('KEY_BUDGET_DURATION', '1d')
DB_PATH       = '/app/data/portal.db'
# URL publique de l'API compatible OpenAI, affichée aux utilisateurs.
PUBLIC_API_URL = os.environ.get('PUBLIC_API_URL', 'https://api.cronos.website/v1')
# Base LiteLLM (Postgres) pour les statistiques de consommation horodatées.
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

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db:
        db.close()

def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row['value'] if row else default

def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value))
    )
    db.commit()

def init_db():
    os.makedirs('/app/data', exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS model_requests (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT NOT NULL,
            fullname   TEXT NOT NULL,
            model_id   TEXT NOT NULL,
            reason     TEXT,
            status     TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS api_keys (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT NOT NULL,
            key_alias  TEXT NOT NULL,
            key_value  TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(username, key_alias)
        );
        CREATE TABLE IF NOT EXISTS model_configs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            hf_model_id TEXT NOT NULL,
            vllm_args   TEXT DEFAULT '',
            engine      TEXT NOT NULL DEFAULT 'vllm',   -- 'vllm' | 'llamacpp'
            added_at    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS budget_requests (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            username        TEXT NOT NULL,
            fullname        TEXT NOT NULL,
            key_alias       TEXT NOT NULL,
            current_budget  REAL,
            reason          TEXT,
            status          TEXT DEFAULT 'pending',
            granted_amount  REAL,
            created_at      TEXT NOT NULL,
            updated_at      TEXT
        );
        CREATE TABLE IF NOT EXISTS announcements (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            kind       TEXT NOT NULL,
            a          TEXT DEFAULT '',
            b          TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS announcement_state (
            username     TEXT PRIMARY KEY,
            last_seen_id INTEGER NOT NULL DEFAULT 0
        );
    ''')
    # Migration : api_keys de key_alias unique GLOBAL → unique par (username, alias)
    # (évite qu'un utilisateur écrase la ligne d'un autre via un alias identique).
    sql = (db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='api_keys'")
             .fetchone() or [''])[0] or ''
    if 'UNIQUE(username' not in sql.replace(' ', ''):
        db.executescript('''
            CREATE TABLE api_keys_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL,
                key_alias TEXT NOT NULL, key_value TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(username, key_alias)
            );
            INSERT INTO api_keys_new (id, username, key_alias, key_value, created_at)
                SELECT id, username, key_alias, key_value, created_at FROM api_keys;
            DROP TABLE api_keys;
            ALTER TABLE api_keys_new RENAME TO api_keys;
        ''')
    # Migration : ajout du moteur d'inférence (vLLM historique, llama.cpp pour les GGUF)
    cols = {r[1] for r in db.execute("PRAGMA table_info(model_configs)")}
    if 'engine' not in cols:
        db.execute("ALTER TABLE model_configs ADD COLUMN engine TEXT NOT NULL DEFAULT 'vllm'")
    db.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)",
        ('default_key_budget', str(KEY_BUDGET))
    )
    db.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)",
        ('default_key_duration', KEY_DURATION)
    )
    ORNITH_ARGS = "--enable-auto-tool-choice --tool-call-parser qwen3_coder --dtype bfloat16 --max-model-len 262144 --gpu-memory-utilization 0.7 --max-num-seqs 8"
    now = datetime.now().isoformat()
    db.execute(
        "INSERT OR IGNORE INTO model_configs (name, hf_model_id, vllm_args, added_at) VALUES (?,?,?,?)",
        ("ornith-35b-fp8", "deepreinforce-ai/Ornith-1.0-35B-FP8", ORNITH_ARGS, now)
    )
    # Toujours mettre à jour les args du modèle pré-configuré
    db.execute("UPDATE model_configs SET hf_model_id=?, vllm_args=? WHERE name=?",
               ("deepreinforce-ai/Ornith-1.0-35B-FP8", ORNITH_ARGS, "ornith-35b-fp8"))
    db.commit()
    db.close()

# ── LDAP ────────────────────────────────────────────────────────────────────

def _is_admin_group(dn):
    """Vrai si un des composants RDN du DN est exactement cn=adm_cronos.
    Évite le faux positif d'un simple `'adm_cronos' in dn` (qui matcherait
    cn=adm_cronos_readonly, cn=notadm_cronos, etc.)."""
    for part in dn.split(','):
        attr, _, val = part.strip().partition('=')
        if attr.strip().lower() == 'cn' and val.strip().lower() == 'adm_cronos':
            return True
    return False


def ldap_authenticate(username, password):
    """Retourne (ok, is_admin, display_name)."""
    # Rejet strict : un mot de passe vide déclenche un "unauthenticated bind"
    # LDAP qui réussit sur certains annuaires → bypass d'authentification.
    # Un identifiant hors charset autorisé est refusé avant tout accès LDAP.
    if not password or not USERNAME_RE.match(username):
        return False, False, username
    try:
        server = Server(LDAP_URI, get_info=ALL)
        # Échappement anti-injection : RDN pour le DN de bind, filtre pour la recherche.
        user_dn = f"uid={escape_rdn(username)},ou=people,{LDAP_BASE}"
        conn = Connection(server, user=user_dn, password=password,
                          authentication=SIMPLE, auto_bind=True)
        conn.search(
            search_base=f"ou=people,{LDAP_BASE}",
            search_filter=f"(uid={escape_filter_chars(username)})",
            attributes=['cn', 'memberOf']
        )
        if not conn.entries:
            conn.unbind()
            return False, False, username
        entry = conn.entries[0]
        fullname = str(entry.cn) if hasattr(entry, 'cn') else username
        groups = [str(g) for g in entry.memberOf] if hasattr(entry, 'memberOf') else []
        is_admin = any(_is_admin_group(g) for g in groups)
        conn.unbind()
        return True, is_admin, fullname
    except Exception:
        return False, False, username

# ── Helpers ─────────────────────────────────────────────────────────────────

def litellm_headers():
    return {'Authorization': f'Bearer {LITELLM_KEY}', 'Content-Type': 'application/json'}

_rm_cache = {'t': 0.0, 'v': []}

def get_running_models():
    """Modèle(s) servi(s) par vLLM. Mis en cache ~5 s pour éviter de marteler
    /v1/models à chaque rendu de page et à chaque poll (logs vLLM lisibles)."""
    now = time.time()
    if now - _rm_cache['t'] < 5:
        return _rm_cache['v']
    v = []
    try:
        r = requests.get(f"{VLLM_API}/models", timeout=3)
        if r.ok:
            v = [m['id'] for m in r.json().get('data', [])]
    except Exception:
        pass
    _rm_cache.update(t=now, v=v)
    return v

def add_announcement(kind, a='', b=''):
    """Publie une annonce (carré affiché à l'ouverture du site). kind ∈
    {'site', 'model_add', 'model_launch'}. `a`/`b` sont des champs libres
    (ex. nom du modèle / ancien modèle) rendus côté client."""
    try:
        db = get_db()
        db.execute(
            "INSERT INTO announcements (kind, a, b, created_at) VALUES (?,?,?,?)",
            (kind, a or '', b or '', datetime.now().isoformat()))
        db.commit()
    except Exception:
        pass

def _announce_launch(new_name):
    """Annonce le passage à un nouveau modèle actif. Ne publie rien si ce modèle
    est déjà le dernier annoncé (relance / même modèle) → pas de doublon. Le
    « remplace X » vient de la dernière annonce, plus fiable que get_running_models()
    au moment du lancement (l'ancien est en train d'être tué, le nouveau pas encore up)."""
    last = get_db().execute(
        "SELECT a FROM announcements WHERE kind='model_launch' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    prev = last['a'] if last else ''
    if prev == new_name:
        return
    add_announcement('model_launch', new_name, prev)

def get_user_keys(username):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        local_keys = conn.execute(
            "SELECT key_alias, key_value, created_at FROM api_keys WHERE username=? ORDER BY created_at DESC",
            (username,)
        ).fetchall()
        conn.close()
    except Exception:
        return []
    result = []
    for k in local_keys:
        info = {
            'key_alias': k['key_alias'],
            'key': k['key_value'],
            'created_at': k['created_at'],
            'spend': 0,
            'max_budget': None,
            'budget_reset_at': None,
        }
        try:
            r = requests.get(
                f"{LITELLM_URL}/key/info",
                headers=litellm_headers(),
                params={"key": k['key_value']},
                timeout=3
            )
            if r.ok:
                data = r.json().get('info', {})
                info['spend'] = data.get('spend', 0)
                info['max_budget'] = data.get('max_budget')
                info['budget_reset_at'] = data.get('budget_reset_at', '')
        except Exception:
            pass
        result.append(info)
    return result

def _ensure_litellm_user(username, max_budget, budget_duration):
    """Crée/maj l'utilisateur LiteLLM avec un budget de COMPTE, partagé par toutes
    ses clés (user_id). Ne réécrase pas le budget si l'utilisateur existe déjà —
    seul le montant peut avoir été ajusté par un admin."""
    body = {"user_id": username, "metadata": {"created_by": "dgx-portal"}}
    try:
        # /user/info existe déjà ? sinon on le crée avec le budget par défaut.
        info = _litellm_user_info(username)
        if info.get('exists'):
            return True
        body["max_budget"] = float(max_budget)
        body["budget_duration"] = budget_duration
        r = requests.post(f"{LITELLM_URL}/user/new", headers=litellm_headers(),
                          json=body, timeout=8)
        return r.status_code < 300
    except Exception:
        return False


def _litellm_user_info(username):
    """Budget/spend au niveau COMPTE (objet user LiteLLM)."""
    out = {'spend': 0, 'max_budget': None, 'budget_reset_at': '', 'exists': False}
    try:
        r = requests.get(f"{LITELLM_URL}/user/info", headers=litellm_headers(),
                         params={'user_id': username}, timeout=5)
        if r.ok:
            d = r.json()
            ui = d.get('user_info') or d
            if ui:
                out['exists'] = True
                out['spend'] = ui.get('spend', 0) or 0
                out['max_budget'] = ui.get('max_budget')
                out['budget_reset_at'] = ui.get('budget_reset_at', '') or ''
    except Exception:
        pass
    return out


def litellm_update_user_budget(username, new_max_budget):
    try:
        r = requests.post(f"{LITELLM_URL}/user/update", headers=litellm_headers(),
                          json={'user_id': username, 'max_budget': float(new_max_budget)},
                          timeout=5)
        return r.ok
    except Exception:
        return False


def create_litellm_key(alias, username, is_admin=False):
    payload = {
        "key_alias": alias,
        "metadata": {"user": username, "created_by": "dgx-portal"},
    }
    if not is_admin:
        # Budget au niveau COMPTE (partagé par toutes les clés du compte), pas au
        # niveau clé : la clé porte user_id et LiteLLM plafonne la somme des dépenses
        # de l'utilisateur sur l'ensemble de ses clés.
        _ensure_litellm_user(username,
                             float(get_setting('default_key_budget', KEY_BUDGET)),
                             get_setting('default_key_duration', KEY_DURATION))
        payload["user_id"] = username
    r = requests.post(f"{LITELLM_URL}/key/generate",
                      headers=litellm_headers(), json=payload, timeout=10)
    if r.ok:
        return r.json().get('key')
    return None

def litellm_key_info(key_value):
    try:
        r = requests.get(f"{LITELLM_URL}/key/info", headers=litellm_headers(),
                         params={'key': key_value}, timeout=5)
        if r.ok:
            return r.json().get('info', {})
    except Exception:
        pass
    return {}

def litellm_update_key_budget(key_value, new_max_budget):
    try:
        r = requests.post(f"{LITELLM_URL}/key/update", headers=litellm_headers(),
                          json={'key': key_value, 'max_budget': new_max_budget}, timeout=5)
        return r.ok
    except Exception:
        return False

def revoke_litellm_key(key_value):
    r = requests.post(f"{LITELLM_URL}/key/delete",
                      headers=litellm_headers(),
                      json={"keys": [key_value]}, timeout=5)
    return r.ok

def _runner_headers():
    return {'Authorization': f'Bearer {RUNNER_TOKEN}'}

def runner_status():
    try:
        r = requests.get(f"{RUNNER_URL}/status", headers=_runner_headers(), timeout=3)
        if r.ok:
            st = r.json()
            # Le runner ne bascule en "running" que sur la ligne de log
            # « Application startup complete », masquée par --uvicorn-log-level
            # warning. On fiabilise l'état en vérifiant que vLLM sert réellement
            # le modèle → plus de « Démarrage… » qui reste collé.
            if st.get('status') == 'starting' and st.get('model') in get_running_models():
                st['status'] = 'running'
            return st
    except Exception:
        pass
    return {'status': 'unreachable', 'model': None, 'pid': None}

def runner_launch(hf_model_id, model_name, vllm_args='', engine='vllm'):
    # Timeout long : quand un modèle tourne déjà, le runner attend que le driver
    # rende la mémoire unifiée avant de spawner le nouveau (anti-OOM). /launch peut
    # donc mettre ~10-60 s à répondre — un timeout court ferait croire à un échec
    # alors que le lancement est bien parti.
    try:
        r = requests.post(f"{RUNNER_URL}/launch",
                          headers=_runner_headers(),
                          json={'hf_model_id': hf_model_id, 'model_name': model_name,
                                'vllm_args': vllm_args, 'engine': engine or 'vllm'},
                          timeout=90)
        return r.ok
    except Exception:
        return False

def runner_stop():
    try:
        r = requests.post(f"{RUNNER_URL}/stop", headers=_runner_headers(), timeout=5)
        return r.ok
    except Exception:
        return False

# Lignes d'accès de routine (polls santé/statut) → bruit qui noie les logs utiles.
_LOG_NOISE_RE = re.compile(r'"GET /(?:v1/models|metrics|health\S*|version|ping)\b')

def _drop_log_noise(lines):
    return [l for l in lines if not _LOG_NOISE_RE.search(l)]

def runner_logs(n=150):
    try:
        # on demande large puis on filtre le bruit pour renvoyer n lignes utiles.
        r = requests.get(f"{RUNNER_URL}/logs", headers=_runner_headers(),
                         params={'n': min(n * 5, 2000)}, timeout=3)
        if r.ok:
            return _drop_log_noise(r.json().get('logs', []))[-n:]
    except Exception:
        pass
    return []

def runner_metrics():
    try:
        r = requests.get(f"{RUNNER_URL}/metrics", headers=_runner_headers(), timeout=5)
        if r.ok:
            return r.json()
    except Exception:
        pass
    return None

_VLLM_METRICS_URL = VLLM_API.rsplit('/v1', 1)[0] + '/metrics'
_vllm_tps = {'t': 0.0, 'gen': 0.0}

def _prom_sum(text, metric):
    """Somme des échantillons d'une métrique Prometheus (nom exact, labels ignorés)."""
    tot, found = 0.0, False
    for line in text.splitlines():
        if line.startswith(metric) and len(line) > len(metric) and line[len(metric)] in ' {':
            try:
                tot += float(line.rsplit(' ', 1)[1]); found = True
            except (ValueError, IndexError):
                pass
    return tot if found else None

_vllm_health_cache = {'t': 0.0, 'v': None}

def vllm_health():
    """Santé du modèle actif (débit tok/s, requêtes en cours/file, TTFT moyen).
    Mis en cache ~4 s → un seul scrape /metrics même avec plusieurs polls."""
    now = time.time()
    if _vllm_health_cache['v'] is not None and now - _vllm_health_cache['t'] < 4:
        return _vllm_health_cache['v']
    out = _vllm_health_uncached()
    _vllm_health_cache.update(t=now, v=out)
    return out

# Les deux moteurs exposent /metrics au format Prometheus, mais avec des noms
# différents. On mappe les deux vers le même dictionnaire de santé.
_METRIC_NAMES = {
    'vllm': {
        'gen':      'vllm:generation_tokens_total',
        'running':  'vllm:num_requests_running',
        'waiting':  'vllm:num_requests_waiting',
        'requests': 'vllm:request_success_total',
        'ttft_sum': 'vllm:time_to_first_token_seconds_sum',
        'ttft_cnt': 'vllm:time_to_first_token_seconds_count',
    },
    'llamacpp': {
        'gen':      'llamacpp:tokens_predicted_total',
        'running':  'llamacpp:requests_processing',
        'waiting':  'llamacpp:requests_deferred',
        'requests': 'llamacpp:n_decode_total',
        'ttft_sum': 'llamacpp:prompt_seconds_total',
        'ttft_cnt': 'llamacpp:n_prompt_tokens_processed_total',
    },
}

def _vllm_health_uncached():
    running = get_running_models()
    if not running:
        return {'up': False, 'model': None}
    engine = 'vllm'
    try:
        row = get_db().execute("SELECT engine FROM model_configs WHERE name=?",
                               (running[0],)).fetchone()
        if row and row['engine']:
            engine = row['engine']
    except Exception:
        pass
    try:
        text = requests.get(_VLLM_METRICS_URL, timeout=4).text
    except Exception:
        return {'up': True, 'model': running[0], 'engine': engine, 'metrics': False}
    M = _METRIC_NAMES.get(engine, _METRIC_NAMES['vllm'])
    gen = _prom_sum(text, M['gen']) or 0.0
    now = time.time()
    tps = None
    if _vllm_tps['t'] and now > _vllm_tps['t'] and gen >= _vllm_tps['gen']:
        tps = (gen - _vllm_tps['gen']) / (now - _vllm_tps['t'])
    _vllm_tps.update(t=now, gen=gen)
    ttft_sum = _prom_sum(text, M['ttft_sum']) or 0.0
    ttft_cnt = _prom_sum(text, M['ttft_cnt']) or 0.0
    # Slots de génération concurrents du modèle actif (--max-num-seqs / --parallel)
    # → « X / N sessions occupées » sur l'accueil.
    max_seqs = None
    try:
        row = get_db().execute("SELECT vllm_args FROM model_configs WHERE name=?",
                               (running[0],)).fetchone()
        if row:
            max_seqs = max_seqs_of(row['vllm_args'], engine)
    except Exception:
        pass
    return {
        'up': True,
        'model': running[0],
        'engine': engine,
        'metrics': True,
        'running': int(_prom_sum(text, M['running']) or 0),
        'waiting': int(_prom_sum(text, M['waiting']) or 0),
        'max_seqs': max_seqs,
        'tps': round(tps, 1) if tps is not None else None,
        'ttft': round(ttft_sum / ttft_cnt, 2) if ttft_cnt else None,
        'requests': int(_prom_sum(text, M['requests']) or 0),
    }

# Tag HF porté par les modèles réellement testés sur DGX Spark / GB10.
GB10_TAG = 'gb10'

def guess_engine(model):
    """Moteur nécessaire pour servir ce modèle, déduit de ses tags HF.
    GGUF → llama.cpp ; poids safetensors (NVFP4/FP8/BF16) → vLLM."""
    tags = {t.lower() for t in (model.get('tags') or [])}
    if 'gguf' in tags:
        return 'llamacpp'
    return 'vllm'

# Les deux moteurs expriment contexte et concurrence avec des flags différents.
_CTX_FLAG  = {'vllm': 'max-model-len', 'llamacpp': 'ctx-size', 'ds4': 'ctx'}
_SEQS_FLAG = {'vllm': 'max-num-seqs',  'llamacpp': 'parallel'}

def _arg_int(args, flag, default=None):
    m = re.search(r'--' + re.escape(flag) + r'\s+(\d+)', args or '')
    return int(m.group(1)) if m else default

def ctx_of(args, engine='vllm'):
    """Fenêtre de contexte configurée (--max-model-len ou --ctx-size)."""
    return _arg_int(args, _CTX_FLAG.get(engine or 'vllm', 'max-model-len'))

def max_seqs_of(args, engine='vllm'):
    """Sessions concurrentes configurées (--max-num-seqs ou --parallel).
    ds4 n'a aucun réglage de parallélisme : il alloue un seul KV cache géant (1M)
    et sérialise les requêtes → 1 session, mesuré (2 requêtes = 2× la latence solo)."""
    if engine == 'ds4':
        return 1
    return _arg_int(args, _SEQS_FLAG.get(engine or 'vllm', 'max-num-seqs'))

def search_hf_models(query, task='text-generation', gb10_only=True):
    """Recherche HF. Par défaut, restreinte aux modèles tagués `gb10` — c'est-à-dire
    ceux réellement testés sur DGX Spark. Plusieurs `filter` = ET côté API HF."""
    filters = [task] if task else []
    if gb10_only:
        filters.append(GB10_TAG)
    try:
        r = requests.get(
            'https://huggingface.co/api/models',
            params={'search': query, 'filter': filters, 'limit': 24,
                    'sort': 'downloads', 'direction': -1},
            timeout=8
        )
        if r.ok:
            out = r.json()
            for m in out:
                m['engine'] = guess_engine(m)
            return out
    except Exception:
        pass
    return []

def notify_discord(model_id, username, fullname, reason):
    if not DISCORD_WH:
        return
    payload = {"embeds": [{
        "title": "🤖 Nouvelle demande de modèle — DGX Spark",
        "color": 0x76B900,
        "fields": [
            {"name": "Utilisateur", "value": f"{fullname} (`{username}`)", "inline": True},
            {"name": "Modèle", "value": f"`{model_id}`", "inline": True},
            {"name": "Raison", "value": reason or "—"},
        ],
        "footer": {"text": "DGX Portal"},
        "timestamp": datetime.utcnow().isoformat()
    }]}
    try:
        requests.post(DISCORD_WH, json=payload, timeout=5)
    except Exception:
        pass

def notify_email(model_id, username, fullname, reason):
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASS, ADMIN_EMAIL]):
        return
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"[DGX] Demande modèle : {model_id}"
    msg['From'] = SMTP_FROM or SMTP_USER
    msg['To'] = ADMIN_EMAIL
    body = (
        f"Nouvelle demande de modèle\n\n"
        f"Utilisateur : {fullname} ({username})\n"
        f"Modèle      : {model_id}\n"
        f"Raison      : {reason or '—'}\n"
        f"Date        : {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
        f"Dashboard admin : http://dgx.cronos.lan:5000/admin\n"
    )
    msg.attach(MIMEText(body, 'plain'))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(msg['From'], [ADMIN_EMAIL], msg.as_string())
    except Exception as e:
        print(f"[email] erreur : {e}")

def notify_budget_discord(username, fullname, key_alias, current_budget, reason):
    if not DISCORD_WH:
        return
    payload = {"embeds": [{
        "title": "🔋 Demande de tokens supplémentaires — DGX Spark",
        "color": 0xF0A500,
        "fields": [
            {"name": "Utilisateur", "value": f"{fullname} (`{username}`)", "inline": True},
            {"name": "Clé", "value": f"`{key_alias}`", "inline": True},
            {"name": "Budget actuel", "value": f"{current_budget:,.0f} tokens" if current_budget is not None else "—", "inline": True},
            {"name": "Raison", "value": reason or "—"},
        ],
        "footer": {"text": "DGX Portal"},
        "timestamp": datetime.utcnow().isoformat()
    }]}
    try:
        requests.post(DISCORD_WH, json=payload, timeout=5)
    except Exception:
        pass

def notify_budget_email(username, fullname, key_alias, current_budget, reason):
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASS, ADMIN_EMAIL]):
        return
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"[DGX] Demande de tokens : {username}"
    msg['From'] = SMTP_FROM or SMTP_USER
    msg['To'] = ADMIN_EMAIL
    budget_str = f"{current_budget:,.0f} tokens" if current_budget is not None else "—"
    body = (
        f"Nouvelle demande de tokens supplémentaires\n\n"
        f"Utilisateur   : {fullname} ({username})\n"
        f"Clé           : {key_alias}\n"
        f"Budget actuel : {budget_str}\n"
        f"Raison        : {reason or '—'}\n"
        f"Date          : {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
        f"Dashboard admin : http://dgx.cronos.lan:5000/admin\n"
    )
    msg.attach(MIMEText(body, 'plain'))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(msg['From'], [ADMIN_EMAIL], msg.as_string())
    except Exception as e:
        print(f"[email] erreur : {e}")

# ── Décorateurs ─────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'))
        if not session.get('is_admin'):
            flash("Accès réservé aux administrateurs.", "danger")
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated

# ── Routes ──────────────────────────────────────────────────────────────────

# ── Anti-brute-force du login (in-memory, best-effort) ──────────────────────
_login_lock = threading.Lock()
_login_attempts = {}          # clé -> {'fails': int, 'first': ts, 'until': ts}
LOGIN_MAX_FAILS = 6           # tentatives avant verrouillage
LOGIN_WINDOW    = 900         # fenêtre glissante (15 min)
LOGIN_LOCK      = 900         # durée du verrouillage (15 min)

def _login_locked(key):
    """Retourne le nb de secondes de verrouillage restant, ou 0."""
    now = time.time()
    with _login_lock:
        e = _login_attempts.get(key)
        if e and e['until'] > now:
            return int(e['until'] - now)
    return 0

def _login_fail(key):
    now = time.time()
    with _login_lock:
        e = _login_attempts.get(key)
        if not e or now - e['first'] > LOGIN_WINDOW:
            e = {'fails': 0, 'first': now, 'until': 0}
        e['fails'] += 1
        if e['fails'] >= LOGIN_MAX_FAILS:
            e['until'] = now + LOGIN_LOCK
        _login_attempts[key] = e

def _login_reset(key):
    with _login_lock:
        _login_attempts.pop(key, None)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'username' in session:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        ip  = request.remote_addr or 'unknown'          # vraie IP via ProxyFix
        key = f"{ip}|{username}"
        wait = _login_locked(key) or _login_locked(ip)
        if wait:
            flash(f"Trop de tentatives. Réessaie dans {wait // 60 + 1} min.", "danger")
            return render_template('login.html', oidc_enabled=OIDC_ENABLED)
        ok, is_admin, fullname = ldap_authenticate(username, password)
        if ok:
            _login_reset(key); _login_reset(ip)
            _apply_session(username, fullname, is_admin, via_sso=False)
            return redirect(_safe_next(request.args.get('next')))
        _login_fail(key); _login_fail(ip)
        flash("Identifiants incorrects.", "danger")
    return render_template('login.html', oidc_enabled=OIDC_ENABLED)


def _safe_next(target):
    """N'autorise que les redirections vers un chemin local relatif — bloque
    l'open redirect (?next=https://evil.com, //evil.com, ou /\\evil.com que les
    navigateurs normalisent en //evil.com)."""
    if not target or '\\' in target or '\t' in target or '\n' in target:
        return url_for('index')
    parsed = urlparse(target)
    # target[:2] in ('//','/\\') : bloque protocole-relatif et backslash après /
    if (parsed.scheme or parsed.netloc or not target.startswith('/')
            or target[:2] in ('//', '/\\')):
        return url_for('index')
    return target


def _apply_session(username, fullname, is_admin, via_sso=False):
    session.clear()
    session['username'] = username
    session['fullname'] = fullname
    session['is_admin'] = is_admin
    session['sso'] = via_sso


def ldap_lookup_admin(username):
    """Détermine is_admin via un lookup LDAP par uid (compte de service).
    Utilisé pour le SSO quand le claim OIDC 'groups' est absent."""
    if not (LDAP_BIND_DN and LDAP_BIND_PW) or not USERNAME_RE.match(username or ''):
        return False
    try:
        server = Server(LDAP_URI, get_info=ALL)
        conn = Connection(server, user=LDAP_BIND_DN, password=LDAP_BIND_PW,
                          authentication=SIMPLE, auto_bind=True)
        conn.search(search_base=f"ou=people,{LDAP_BASE}",
                    search_filter=f"(uid={escape_filter_chars(username)})",
                    attributes=['memberOf'])
        is_admin = False
        if conn.entries and hasattr(conn.entries[0], 'memberOf'):
            groups = [str(g) for g in conn.entries[0].memberOf]
            is_admin = any(_is_admin_group(g) for g in groups)
        conn.unbind()
        return is_admin
    except Exception:
        return False


def ldap_lookup_email(username):
    """Email de l'utilisateur via le compte de service LDAP (pour le notifier)."""
    if not (LDAP_BIND_DN and LDAP_BIND_PW) or not USERNAME_RE.match(username or ''):
        return None
    try:
        conn = Connection(Server(LDAP_URI, get_info=ALL), user=LDAP_BIND_DN,
                          password=LDAP_BIND_PW, authentication=SIMPLE, auto_bind=True)
        conn.search(search_base=f"ou=people,{LDAP_BASE}",
                    search_filter=f"(uid={escape_filter_chars(username)})", attributes=['mail'])
        email = None
        if conn.entries and hasattr(conn.entries[0], 'mail') and conn.entries[0].mail:
            email = str(conn.entries[0].mail)
        conn.unbind()
        return email or None
    except Exception:
        return None


def send_user_email(to_email, subject, body):
    """Envoie un email simple à un utilisateur (notifications)."""
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASS]) or not to_email:
        return False
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = SMTP_FROM or SMTP_USER
    msg['To'] = to_email
    msg.attach(MIMEText(body, 'plain'))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(msg['From'], [to_email], msg.as_string())
        return True
    except Exception as e:
        print(f"[email user] erreur : {e}")
        return False


@app.context_processor
def inject_budget_alert():
    """Bannière in-app quand le budget du compte dépasse 85 % (non-admins)."""
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
    if not username:
        flash("SSO : profil incomplet (identifiant manquant).", "danger")
        return redirect(url_for('login'))
    fullname = userinfo.get('name') or username

    groups = userinfo.get('groups')
    if isinstance(groups, list):
        # Authentik renvoie des noms de groupes ("adm_cronos") ; _is_admin_group
        # couvre aussi le cas où ce serait un DN complet.
        is_admin = any(g == OIDC_ADMIN_GROUP or _is_admin_group(g) for g in groups)
    else:
        # Claim 'groups' absent → on retombe sur un lookup LDAP par uid.
        is_admin = ldap_lookup_admin(username)

    nxt = session.pop('sso_next', None)
    _apply_session(username, fullname, is_admin, via_sso=True)
    return redirect(_safe_next(nxt))


@app.route('/logout')
def logout():
    was_sso = session.get('sso')
    session.clear()
    # Déconnexion RP-initiated : si l'utilisateur s'est connecté en SSO, on le
    # renvoie aussi vers l'end-session Authentik pour fermer la session IdP.
    if was_sso and OIDC_LOGOUT_URL:
        return redirect(OIDC_LOGOUT_URL)
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    running = get_running_models()
    db = get_db()
    my_requests = db.execute(
        "SELECT * FROM model_requests WHERE username=? ORDER BY created_at DESC LIMIT 5",
        (session['username'],)
    ).fetchall()
    default_budget = float(get_setting('default_key_budget', KEY_BUDGET))
    return render_template('index.html', running_models=running, my_requests=my_requests,
                           public_api_url=PUBLIC_API_URL, usage=user_hourly(session['username']),
                           sysmetrics=runner_metrics(),
                           modelhealth=vllm_health(),
                           active_users=_active_users() if session.get('is_admin') else None,
                           budget_tokens=f"{default_budget:,.0f}".replace(',', ' '),
                           budget_duration=get_setting('default_key_duration', KEY_DURATION))

@app.route('/keys', methods=['GET', 'POST'])
@login_required
def keys():
    user_keys = get_user_keys(session['username'])
    new_key_alias = None
    if request.method == 'POST':
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
                new_key_alias = alias
                flash("Clé créée !", "success")
                user_keys = get_user_keys(session['username'])
            else:
                flash("Erreur lors de la création de la clé.", "danger")
        elif action == 'revoke':
            k = request.form.get('key')
            db = get_db()
            # Vérifie que la clé appartient bien à l'utilisateur connecté AVANT de
            # la révoquer côté LiteLLM (anti-IDOR : sinon n'importe quel user pourrait
            # révoquer la clé d'un autre en soumettant sa valeur).
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
                user_keys = get_user_keys(session['username'])
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
                return redirect(url_for('keys'))
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
    running = get_running_models()
    default_budget = float(get_setting('default_key_budget', KEY_BUDGET))
    # Budget au niveau COMPTE (partagé par toutes les clés). Admin = illimité.
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
    # Limites de contexte par modèle (déduites de --max-model-len), pour les snippets
    # d'intégration qui déclarent la fenêtre côté client (OpenCode/OpenChamber).
    model_limits = {}
    for row in get_db().execute("SELECT name, vllm_args FROM model_configs"):
        mm = re.search(r'--max-model-len\s+(\d+)', row['vllm_args'] or '')
        if mm:
            ctx = int(mm.group(1))
            model_limits[row['name']] = {'context': ctx, 'output': min(ctx // 2, 262144)}
    return render_template('keys.html', user_keys=user_keys, new_key_alias=new_key_alias,
                           budget_tokens=f"{default_budget:,.0f}".replace(',', ' '),
                           budget_duration=get_setting('default_key_duration', KEY_DURATION),
                           account=account,
                           model_limits=model_limits,
                           running_models=running, public_api_url=PUBLIC_API_URL)


# ── Support (assistant IA) ───────────────────────────────────────────────────
SUPPORT_FAQ = (
    "FAQ plateforme Cronos :\n"
    "- Plateforme IA interne et GRATUITE (pas de facturation, pas de plan payant).\n"
    "- API compatible OpenAI. Endpoint public : configuré dans « Mes clés API ».\n"
    "- Budget PAR COMPTE, partagé par toutes les clés d'un même utilisateur, "
    "réinitialisé chaque jour. Le quota compte les vrais tokens : 1 token de prompt = 1, 1 token généré = 1.\n"
    "- Obtenir plus de budget : demande envoyée à un admin (bouton « Demander plus de "
    "tokens » ou via toi, Cronos). Un admin valide.\n"
    "- Demander un nouveau modèle : via la page « Demander un modèle » (identifiant "
    "Hugging Face) ou via toi ; un admin le valide puis le lance.\n"
    "- Intégrations : OpenCode, Hermes Agent, Codex, Aider, Cursor, Continue, "
    "Python/cURL — snippets prêts sur « Mes clés API ».\n"
    "- Un seul modèle tourne à la fois sur le GPU (mémoire unifiée du DGX Spark)."
)

SUPPORT_SYSTEM = (
    "Tu es Cronos, l'assistant IA de la plateforme Cronos (NVIDIA DGX Spark, "
    "auto-hébergée). Tu aides les utilisateurs en français, de façon concise et "
    "concrète, sur les clés API, le budget/quota, les intégrations, l'accès aux "
    "modèles et le dépannage.\n"
    "Tu peux AGIR pour l'utilisateur via des outils (tools) — toujours au nom du "
    "compte connecté, jamais pour quelqu'un d'autre :\n"
    "- create_api_key : créer une clé API.\n"
    "- revoke_api_key : supprimer une de ses clés (DESTRUCTIF).\n"
    "- request_budget : déposer une demande d'augmentation de budget.\n"
    "- request_model : demander l'ajout d'un modèle (identifiant Hugging Face).\n"
    "- launch_model / stop_model : (admin uniquement) piloter le modèle du GPU.\n"
    "Règles d'usage des outils :\n"
    "- N'appelle un outil QUE pour une action explicitement demandée (créer/"
    "révoquer une clé, demander du budget/un modèle, lancer/arrêter). Pour toute "
    "question de dépannage, d'information ou d'explication, réponds DIRECTEMENT en "
    "texte, SANS appeler d'outil (tu as déjà les logs et l'état dans le contexte).\n"
    "- Confirme TOUJOURS avec l'utilisateur avant une action destructive ou "
    "impactante (revoke_api_key, stop_model, launch_model qui coupe le modèle "
    "actif) : demande « tu confirmes ? » et n'appelle l'outil qu'après un oui.\n"
    "- create_api_key et request_* peuvent être faits directement si la demande est "
    "claire.\n"
    "- Quand tu crées une clé, AFFICHE la clé complète une seule fois à l'utilisateur "
    "(c'est sa nouvelle clé) et rappelle-lui de la copier.\n"
    "Règles générales :\n"
    "- Appuie-toi sur le CONTEXTE et la FAQ fournis. N'invente rien (ni plan payant, "
    "ni page de facturation, ni fonctionnalité inexistante).\n"
    "- Les clés du CONTEXTE sont MASQUÉES : ne tente jamais d'en reconstituer une.\n"
    "- IMPORTANT : réponds DIRECTEMENT, en français, sans montrer ton raisonnement "
    "ni de préambule interne. Va droit au but."
)

_THINK_RE = re.compile(r'<think>.*?</think>|<reasoning>.*?</reasoning>', re.S | re.I)


def _clean_reply(text):
    """Retire les blocs de raisonnement éventuels laissés dans la réponse."""
    text = _THINK_RE.sub('', text or '')
    # Certains modèles émettent un CoT en clair puis la réponse finale : si on
    # détecte un marqueur de réponse finale, on garde ce qui suit.
    for marker in ('### Réponse', 'Réponse finale :', 'Final answer:', 'Voici ma réponse'):
        idx = text.rfind(marker)
        if idx != -1:
            text = text[idx + len(marker):]
    return text.strip().lstrip(':').strip()


def _mask_key(k):
    return (k[:6] + '…' + k[-4:]) if k and len(k) > 12 else '—'


_LOG_HINT_RE = re.compile(
    r'log|erreur|error|marche pas|répond|repond|crash|plante|lent|500|502|503|bug|'
    r'démarr|demarr|charge|timeout|down|hs|ko', re.I)

def _support_context(username, is_admin, user_msg=''):
    """Contexte injecté au bot, STRICTEMENT limité à l'utilisateur connecté.
    Les logs serveur (gros) ne sont inclus que si la question porte sur un souci
    technique → prompt bien plus léger pour les questions courantes."""
    db = get_db()
    lines = [f"Utilisateur connecté : {username}" + (" (admin)" if is_admin else "")]

    # ── Budget + clés du compte ──
    acct = _litellm_user_info(username)
    if is_admin:
        lines.append("Budget du compte : illimité (admin).")
    elif acct['exists'] and acct['max_budget'] is not None:
        s, b = acct['spend'] or 0, acct['max_budget']
        lines.append(("Budget du compte : {:,.0f} / {:,.0f} tokens utilisés"
                      .format(s, b)).replace(',', ' ')
                     + (f" (reset {acct['budget_reset_at'][:10]})" if acct['budget_reset_at'] else ""))
    keys = get_user_keys(username)
    if keys:
        lines.append("Clés de l'utilisateur (masquées, alias = identifiant pour les actions) :")
        for k in keys:
            lines.append("  - {} : {}, dépensé {:,.0f}".format(
                k.get('key_alias', '—'), _mask_key(k.get('key', '')),
                k.get('spend', 0) or 0).replace(',', ' '))
    else:
        lines.append("L'utilisateur n'a aucune clé API pour l'instant.")

    # ── Conso du jour ──
    try:
        u = user_hourly(username)
        if u and u.get('has_data'):
            lines.append("Conso aujourd'hui : {:,.0f} tokens réels (pic vers {}h)."
                         .format(u['total'], u['peak_hour']).replace(',', ' '))
    except Exception:
        pass

    # ── Catalogue des modèles lançables ──
    running = set(get_running_models())
    cat = []
    for row in db.execute("SELECT name, vllm_args FROM model_configs ORDER BY name"):
        mm = re.search(r'--max-model-len\s+(\d+)', row['vllm_args'] or '')
        ctx = int(mm.group(1)) if mm else None
        has_tools = '--tool-call-parser' in (row['vllm_args'] or '')
        flag = " [ACTIF]" if row['name'] in running else ""
        cat.append("  - {}{} : contexte {}, tool-calling {}".format(
            row['name'], flag,
            f"{ctx:,}".replace(',', ' ') if ctx else "?",
            "oui" if has_tools else "non"))
    if cat:
        lines.append("Catalogue des modèles (le [ACTIF] est celui chargé sur le GPU) :\n"
                     + "\n".join(cat))
    st = runner_status()
    lines.append("Runner vLLM : " + st.get('status', '?')
                 + (" — aucun modèle chargé" if not running else ""))

    # ── Demandes en cours de l'utilisateur ──
    mreqs = db.execute("SELECT model_id, status FROM model_requests WHERE username=? "
                       "ORDER BY created_at DESC LIMIT 5", (username,)).fetchall()
    if mreqs:
        lines.append("Demandes de modèle de l'utilisateur : "
                     + ", ".join(f"{r['model_id']} ({r['status']})" for r in mreqs))
    breqs = db.execute("SELECT status FROM budget_requests WHERE username=? "
                       "ORDER BY created_at DESC LIMIT 3", (username,)).fetchall()
    if breqs:
        lines.append("Demandes de budget de l'utilisateur : "
                     + ", ".join(r['status'] for r in breqs))

    # ── Logs serveur (uniquement pour les questions de dépannage) ──
    if _LOG_HINT_RE.search(user_msg or ''):
        logs = runner_logs(n=20)
        if logs:
            tail = [l[:200] for l in logs[-12:]]
            lines.append("Derniers logs du serveur de modèle :\n" + "\n".join(tail))

    return SUPPORT_FAQ + "\n\n" + "\n".join(lines)


def _support_tools(is_admin):
    """Schémas des outils self-service exposés au modèle (format function-calling)."""
    t = [
        {"type": "function", "function": {
            "name": "create_api_key",
            "description": "Crée une nouvelle clé API pour l'utilisateur connecté et la retourne.",
            "parameters": {"type": "object", "properties": {
                "alias": {"type": "string", "description": "Nom court de la clé (ex: mon-laptop). Optionnel."}}}}},
        {"type": "function", "function": {
            "name": "revoke_api_key",
            "description": "Révoque (supprime) une clé de l'utilisateur, par son alias. Destructif : confirmer avant.",
            "parameters": {"type": "object", "properties": {
                "alias": {"type": "string", "description": "Alias exact de la clé à révoquer."}},
                "required": ["alias"]}}},
        {"type": "function", "function": {
            "name": "request_budget",
            "description": "Dépose une demande d'augmentation de budget pour l'utilisateur (envoyée à un admin).",
            "parameters": {"type": "object", "properties": {
                "reason": {"type": "string", "description": "Raison (optionnel)."}}}}},
        {"type": "function", "function": {
            "name": "request_model",
            "description": "Demande l'ajout d'un modèle par son identifiant Hugging Face (envoyée à un admin).",
            "parameters": {"type": "object", "properties": {
                "hf_model_id": {"type": "string", "description": "Ex: Qwen/Qwen3-Coder-30B-A3B-Instruct."},
                "reason": {"type": "string", "description": "Pourquoi ce modèle (optionnel)."}},
                "required": ["hf_model_id"]}}},
    ]
    if is_admin:
        t += [
            {"type": "function", "function": {
                "name": "launch_model",
                "description": "(Admin) Lance un modèle du catalogue par son nom. Remplace le modèle actif — confirmer avant.",
                "parameters": {"type": "object", "properties": {
                    "name": {"type": "string", "description": "Nom du modèle dans le catalogue."}},
                    "required": ["name"]}}},
            {"type": "function", "function": {
                "name": "stop_model",
                "description": "(Admin) Arrête le modèle actuellement chargé. Confirmer avant.",
                "parameters": {"type": "object", "properties": {}}}},
        ]
    return t


def _exec_support_tool(name, args, username, fullname, is_admin):
    """Exécute une action self-service, TOUJOURS au nom de l'utilisateur de session
    (le modèle ne choisit jamais « pour qui »). Retourne un texte de résultat."""
    db = get_db()
    try:
        if name == 'create_api_key':
            raw = (args.get('alias') or '').strip()
            alias = re.sub(r'[^a-zA-Z0-9_-]', '-', raw)[:40] if raw else f"{username}-{int(time.time())}"
            newkey = create_litellm_key(alias, username, is_admin=is_admin)
            if not newkey:
                return "Échec de la création (alias déjà pris ou LiteLLM injoignable)."
            db.execute("INSERT OR REPLACE INTO api_keys (username, key_alias, key_value, created_at) "
                       "VALUES (?,?,?,?)", (username, alias, newkey, datetime.now().isoformat()))
            db.commit()
            return f"Clé créée (alias={alias}). CLÉ COMPLÈTE à montrer une fois : {newkey}"

        if name == 'revoke_api_key':
            alias = (args.get('alias') or '').strip()
            row = db.execute("SELECT key_value FROM api_keys WHERE username=? AND key_alias=?",
                             (username, alias)).fetchone()
            if not row:
                return f"Aucune clé « {alias} » pour cet utilisateur."
            if revoke_litellm_key(row['key_value']):
                db.execute("DELETE FROM api_keys WHERE username=? AND key_alias=?", (username, alias))
                db.commit()
                return f"Clé « {alias} » révoquée."
            return "Échec de la révocation côté LiteLLM."

        if name == 'request_budget':
            reason = (args.get('reason') or '').strip()
            if db.execute("SELECT 1 FROM budget_requests WHERE username=? AND status='pending'",
                          (username,)).fetchone():
                return "Une demande de budget est déjà en attente."
            current = _litellm_user_info(username).get('max_budget')
            db.execute("INSERT INTO budget_requests (username, fullname, key_alias, current_budget, "
                       "reason, status, created_at) VALUES (?,?,?,?,?,?,?)",
                       (username, fullname, '(compte)', current, reason, 'pending',
                        datetime.now().isoformat()))
            db.commit()
            notify_budget_discord(username, fullname, '(compte)', current, reason)
            notify_budget_email(username, fullname, '(compte)', current, reason)
            return "Demande de budget envoyée à un admin."

        if name == 'request_model':
            hf = (args.get('hf_model_id') or '').strip()
            if not hf:
                return "Identifiant de modèle manquant."
            reason = (args.get('reason') or '').strip()
            if db.execute("SELECT 1 FROM model_requests WHERE username=? AND model_id=? AND status='pending'",
                          (username, hf)).fetchone():
                return f"Une demande pour « {hf} » est déjà en attente."
            db.execute("INSERT INTO model_requests (username, fullname, model_id, reason, status, created_at) "
                       "VALUES (?,?,?,?,?,?)",
                       (username, fullname, hf, reason, 'pending', datetime.now().isoformat()))
            db.commit()
            notify_discord(hf, username, fullname, reason)
            notify_email(hf, username, fullname, reason)
            return f"Demande d'ajout du modèle « {hf} » envoyée à un admin."

        if name == 'launch_model':
            if not is_admin:
                return "Action réservée aux admins."
            mname = (args.get('name') or '').strip()
            cfg = db.execute("SELECT hf_model_id, name, vllm_args, engine FROM model_configs WHERE name=?",
                             (mname,)).fetchone()
            if not cfg:
                return f"Modèle « {mname} » introuvable dans le catalogue."
            ok = runner_launch(cfg['hf_model_id'], cfg['name'], cfg['vllm_args'] or '',
                               cfg['engine'] or 'vllm')
            if ok:
                _announce_launch(cfg['name'])
            return f"Lancement de « {mname} » demandé (démarrage en cours)." if ok else "Runner injoignable."

        if name == 'stop_model':
            if not is_admin:
                return "Action réservée aux admins."
            return "Modèle arrêté." if runner_stop() else "Runner injoignable."

        return f"Outil inconnu : {name}"
    except Exception as e:
        return f"Erreur lors de l'exécution de l'action ({type(e).__name__})."


@app.route('/support')
@login_required
def support():
    return render_template('support.html',
                           running_models=get_running_models(),
                           public_api_url=PUBLIC_API_URL)


@app.route('/support/chat', methods=['POST'])
@login_required
def support_chat():
    data = request.get_json(silent=True) or {}
    history = data.get('messages', [])
    if not isinstance(history, list) or not history:
        return jsonify({'reply': "Message vide."}), 400
    history = [{'role': m.get('role'), 'content': str(m.get('content', ''))[:4000]}
               for m in history if m.get('role') in ('user', 'assistant')][-12:]
    running = get_running_models()
    if not running:
        return jsonify({'reply': "Aucun modèle n'est actif sur le serveur pour l'instant : "
                                 "je ne peux pas répondre. Préviens un admin ou réessaie une "
                                 "fois un modèle lancé."})
    model = running[0]
    username = session['username']
    fullname = session.get('fullname', username)
    is_admin = session.get('is_admin', False)
    last_user = next((m['content'] for m in reversed(history) if m['role'] == 'user'), '')
    ctx = _support_context(username, is_admin, user_msg=last_user)
    msgs = [{'role': 'system', 'content': SUPPORT_SYSTEM + "\n\n### CONTEXTE\n" + ctx}] + history
    tools = _support_tools(is_admin)

    def _chat(with_tools):
        body = {'model': model, 'messages': msgs, 'temperature': 0.3, 'max_tokens': 600,
                'chat_template_kwargs': {'enable_thinking': False}}
        if with_tools:
            body['tools'] = tools
            body['tool_choice'] = 'auto'
        return requests.post(f"{LITELLM_URL}/v1/chat/completions", headers=litellm_headers(),
                             json=body, timeout=120)

    try:
        use_tools = True
        for _ in range(4):  # boucle : le modèle peut enchaîner des appels d'outils
            r = _chat(use_tools)
            if not r.ok and use_tools:
                use_tools = False   # modèle sans support tools → réessai sans
                continue
            if not r.ok:
                return jsonify({'reply': f"Le modèle a renvoyé une erreur ({r.status_code}). Réessaie."})
            m = r.json()['choices'][0]['message']
            tcs = m.get('tool_calls')
            if not tcs:
                reply = _clean_reply(m.get('content') or '')
                return jsonify({'reply': reply or "(réponse vide)", 'model': model})
            # Le modèle appelle des outils → on les exécute côté serveur puis on reboucle.
            msgs.append({'role': 'assistant', 'content': m.get('content') or '', 'tool_calls': tcs})
            for tc in tcs:
                fn = tc.get('function', {})
                try:
                    a = json.loads(fn.get('arguments') or '{}')
                except Exception:
                    a = {}
                res = _exec_support_tool(fn.get('name', ''), a, username, fullname, is_admin)
                msgs.append({'role': 'tool', 'tool_call_id': tc.get('id'), 'content': res})
        # Trop d'allers-retours d'outils → on force une réponse finale SANS outils
        # (sinon le modèle peut boucler sur des appels et ne jamais conclure).
        rf = _chat(False)
        if rf.ok:
            reply = _clean_reply(rf.json()['choices'][0]['message'].get('content') or '')
            return jsonify({'reply': reply or "Peux-tu reformuler ta demande ?", 'model': model})
        return jsonify({'reply': "Le modèle est occupé, réessaie dans un instant.", 'model': model})
    except Exception:
        return jsonify({'reply': "Le modèle n'a pas répondu à temps. Réessaie dans un instant."})


# ── Playground : chat direct avec le modèle, en streaming ────────────────────
@app.route('/playground')
@login_required
def playground():
    # Fenêtre de contexte par modèle (déduite de --max-model-len) → anneau d'usage.
    model_limits = {}
    for row in get_db().execute("SELECT name, vllm_args FROM model_configs"):
        mm = re.search(r'--max-model-len\s+(\d+)', row['vllm_args'] or '')
        if mm:
            model_limits[row['name']] = int(mm.group(1))
    return render_template('playground.html', running_models=get_running_models(),
                           model_limits=model_limits)


def _sse_msg(text):
    """Un message SSE 'content' + fin de flux (échappement JSON sûr)."""
    payload = json.dumps({'choices': [{'delta': {'content': text}}]})
    return f"data: {payload}\n\ndata: [DONE]\n\n"


@app.route('/playground/chat', methods=['POST'])
@login_required
def playground_chat():
    data = request.get_json(silent=True) or {}
    history = [{'role': m.get('role'), 'content': str(m.get('content', ''))[:8000]}
               for m in data.get('messages', []) if m.get('role') in ('user', 'assistant')][-20:]
    if not history:
        return Response(_sse_msg("Message vide."), mimetype='text/event-stream')
    running = get_running_models()
    if not running:
        return Response(_sse_msg("Aucun modèle actif."), mimetype='text/event-stream')
    model = data.get('model') if data.get('model') in running else running[0]

    # Réglages (bornés).
    system = str(data.get('system', '')).strip()[:4000]
    def _num(v, lo, hi, default, cast):
        try:
            return min(max(cast(v), lo), hi)
        except (TypeError, ValueError):
            return default
    temperature = _num(data.get('temperature'), 0.0, 2.0, 0.7, float)
    max_tokens  = _num(data.get('max_tokens'), 1, 8192, 1024, int)
    top_p       = _num(data.get('top_p'), 0.0, 1.0, 1.0, float)
    reasoning   = bool(data.get('reasoning'))     # afficher le raisonnement du modèle

    # Le playground consomme le BUDGET de l'utilisateur → on utilise SA clé
    # (partagée par le compte). LiteLLM applique donc le quota (429 si dépassé).
    keys = get_user_keys(session['username'])
    if not keys:
        return Response(_sse_msg("Crée d'abord une clé API (page « Mes clés API ») — "
                                 "le playground utilise ton budget de compte."),
                        mimetype='text/event-stream')
    user_key = keys[0]['key']
    msgs = ([{'role': 'system', 'content': system}] if system else []) + history

    def gen():
        try:
            with requests.post(f"{LITELLM_URL}/v1/chat/completions",
                               headers={'Authorization': f'Bearer {user_key}'},
                               json={'model': model, 'messages': msgs, 'stream': True,
                                     'temperature': temperature, 'max_tokens': max_tokens, 'top_p': top_p,
                                     'stream_options': {'include_usage': True},
                                     'chat_template_kwargs': {'enable_thinking': reasoning}},
                               stream=True, timeout=(10, None)) as r:
                if not r.ok:
                    msg = ("Budget de compte dépassé — attends le reset quotidien ou demande plus de tokens."
                           if r.status_code == 429 else f"Erreur modèle ({r.status_code}).")
                    yield _sse_msg(msg)
                    return
                for line in r.iter_lines():
                    if line:
                        yield line.decode('utf-8', 'replace') + "\n\n"
        except Exception:
            yield _sse_msg("⚠ flux interrompu.")

    return Response(stream_with_context(gen()), mimetype='text/event-stream',
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route('/search')
@login_required
def search():
    query = request.args.get('q', '').strip()
    task  = request.args.get('task', 'text-generation')
    # Filtre GB10 actif par défaut (décoché = on élargit à tout HF).
    gb10 = request.args.get('all') != '1'
    # Avec le filtre GB10 le vivier est petit : on affiche le top même sans requête.
    results = search_hf_models(query, task, gb10_only=gb10) if (query or gb10) else []
    return render_template('search.html', results=results, query=query, task=task,
                           gb10_only=gb10)

@app.route('/ranking')
@login_required
def ranking():
    period = request.args.get('period', 'day')
    if period not in ('day', 'week', 'month'):
        period = 'day'
    data = ranking_full(period, me=session['username'])
    labels = {'day': "Aujourd'hui", 'week': '7 derniers jours', 'month': '30 derniers jours'}
    prev_labels = {'day': 'hier', 'week': 'la semaine précédente', 'month': 'les 30 jours précédents'}
    return render_template('ranking.html', rows=data['rows'], active_count=data['active_count'],
                           period=period, period_label=labels[period], prev_label=prev_labels[period])

@app.route('/request', methods=['GET', 'POST'])
@login_required
def request_model():
    prefill = request.args.get('model', '')
    if request.method == 'POST':
        model_id = request.form['model_id'].strip()
        reason   = request.form.get('reason', '').strip()
        if not model_id:
            flash("L'identifiant du modèle est requis.", "warning")
            return render_template('request_form.html', prefill=prefill)
        db = get_db()
        existing = db.execute(
            "SELECT id FROM model_requests WHERE username=? AND model_id=? AND status='pending'",
            (session['username'], model_id)
        ).fetchone()
        if existing:
            flash("Tu as déjà une demande en attente pour ce modèle.", "warning")
            return redirect(url_for('index'))
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
        return redirect(url_for('index'))
    return render_template('request_form.html', prefill=prefill)

def admin_get_user_consumption():
    """Conso par COMPTE : nb de clés (DB locale) + spend/budget au niveau user
    LiteLLM, récupérés en UN seul appel /user/list (au lieu d'un appel par clé et
    par user — ce qui bloquait le rendu de la page admin)."""
    counts = {}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        for r in conn.execute("SELECT username, COUNT(*) c FROM api_keys GROUP BY username"):
            counts[r['username']] = r['c']
        conn.close()
    except Exception:
        pass
    users = {}
    try:
        r = requests.get(f"{LITELLM_URL}/user/list", headers=litellm_headers(),
                         params={"page_size": 100}, timeout=6)
        if r.ok:
            for u in r.json().get('users', []):
                uid = u.get('user_id')
                if uid not in counts:
                    continue  # on n'affiche que les comptes ayant des clés ici
                mb = u.get('max_budget')
                users[uid] = {'username': uid, 'spend': u.get('spend') or 0,
                              'max_budget': mb if mb is not None else 0,
                              'unlimited': mb is None, 'key_count': counts[uid]}
    except Exception:
        pass
    # Comptes avec des clés mais sans objet user LiteLLM → affichés quand même.
    for uname, c in counts.items():
        users.setdefault(uname, {'username': uname, 'spend': 0, 'max_budget': 0,
                                 'unlimited': False, 'key_count': c})
    # Vrais tokens consommés (prompt + généré) sur la période du budget en cours.
    # Le budget est journalier et se réinitialise à 00:00 UTC → on ne compte que
    # depuis le début de la journée UTC, pour que « consommé » soit comparable au
    # « budget / jour » (sinon on affichait le cumul depuis toujours > budget).
    day_start = (datetime.now(ZoneInfo('UTC'))
                 .replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None))
    toks = _real_tokens_by_user(day_start)
    for uid, u in users.items():
        u['tokens'] = toks.get(uid, 0)
    return sorted(users.values(), key=lambda u: u['tokens'], reverse=True)


# ── Statistiques de consommation (base LiteLLM Postgres) ─────────────────────
# Le tarif est désormais 1:1 (input=1, output=1) → SpendLogs.spend ≈ vrais tokens
# pour les requêtes récentes. On somme malgré tout prompt_tokens+completion_tokens
# directement : exact même pour l'historique tarifé à input×0,1. startTime UTC → LOCAL_TZ.

# Pseudo-clés qui ne correspondent pas à un utilisateur (appels admin/health).
_NON_USER_KEYS = {'litellm_proxy_master_key', 'None', ''}

def _spend_conn():
    if not LITELLM_DB_URL:
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(LITELLM_DB_URL, connect_timeout=4)
        conn.autocommit = True   # lecture seule : évite qu'une requête ratée avorte la transaction
        return conn
    except Exception:
        return None

def _real_tokens_by_user(since_utc=None):
    """Vrais tokens (prompt + généré) par utilisateur, depuis SpendLogs. Si
    `since_utc` (datetime UTC naïf) est fourni, ne compte que depuis cet instant —
    utilisé pour aligner la conso affichée sur la période du budget (journalier)."""
    conn = _spend_conn()
    if not conn:
        return {}
    try:
        umap = _key_user_map(conn)
        cur = conn.cursor()
        q = ('SELECT api_key, SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0)) '
             'FROM "LiteLLM_SpendLogs"')
        params = []
        if since_utc is not None:
            q += ' WHERE "startTime" >= %s'
            params.append(since_utc)
        q += ' GROUP BY api_key'
        cur.execute(q, params)
        out = {}
        for api_key, toks in cur.fetchall():
            if api_key in _NON_USER_KEYS:
                continue
            u = umap.get(api_key)
            if u:
                out[u] = out.get(u, 0) + int(toks or 0)
        return out
    except Exception:
        return {}
    finally:
        conn.close()

def _active_users(window_s=120):
    """Utilisateurs ayant sollicité le modèle dans les `window_s` dernières secondes
    (depuis SpendLogs). Sert le panneau admin « qui utilise le modèle » sur l'accueil.
    NB : SpendLogs n'écrit qu'à la fin d'une requête → c'est l'activité récente, pas
    strictement les requêtes en vol."""
    conn = _spend_conn()
    if not conn:
        return []
    try:
        umap = _key_user_map(conn)
        cur = conn.cursor()
        since = datetime.now(ZoneInfo('UTC')).replace(tzinfo=None) - timedelta(seconds=window_s)
        cur.execute('SELECT api_key, COUNT(*), '
                    'SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0)) '
                    'FROM "LiteLLM_SpendLogs" WHERE "startTime" >= %s GROUP BY api_key', (since,))
        agg = {}
        for api_key, cnt, toks in cur.fetchall():
            if api_key in _NON_USER_KEYS:
                continue
            u = umap.get(api_key)
            if not u:
                continue
            a = agg.setdefault(u, {'username': u, 'requests': 0, 'tokens': 0})
            a['requests'] += int(cnt or 0)
            a['tokens'] += int(toks or 0)
        return sorted(agg.values(), key=lambda x: x['requests'], reverse=True)
    except Exception:
        return []
    finally:
        conn.close()

def _key_user_map(conn):
    """token(hash) -> username, depuis les métadonnées des clés (actives + supprimées)."""
    mapping = {}
    cur = conn.cursor()
    for table in ('LiteLLM_VerificationToken', 'LiteLLM_DeletedVerificationToken',
                  'LiteLLM_DeprecatedVerificationToken'):
        try:
            cur.execute(f"SELECT token, metadata->>'user' FROM \"{table}\"")
            for token, user in cur.fetchall():
                if token and user and token not in mapping:
                    mapping[token] = user
        except Exception:
            pass
    return mapping

def _series_for(usernames):
    """username -> classe de couleur stable (ordre alphabétique, 8 slots + 'other')."""
    out = {}
    for i, u in enumerate(sorted(usernames)):
        out[u] = f"s{i+1}" if i < 8 else "other"
    return out

def _spark_points(spark, w=88, h=24):
    """Points d'une polyline SVG (normalisée sur son propre max)."""
    n = len(spark)
    if n < 2:
        return ''
    mx = max(spark) or 1
    return ' '.join(
        f"{(j/(n-1)*w):.1f},{(h - 1 - (v/mx)*(h-2)):.1f}" for j, v in enumerate(spark))

def ranking_full(period='day', me=None):
    """Classement enrichi : vrais tokens consommés (prompt + généré), delta vs
    période précédente, répartition prompt/généré, et sparkline de tendance, par
    utilisateur."""
    conn = _spend_conn()
    empty = {'period': period, 'rows': [], 'active_count': 0}
    if not conn:
        return empty
    UTC = ZoneInfo('UTC')
    try:
        now_local = datetime.now(ZoneInfo(LOCAL_TZ))
        today = now_local.date()
        if period == 'week':
            cur_start = now_local - timedelta(days=7)
            prev_start = now_local - timedelta(days=14)
            buckets = [today - timedelta(days=i) for i in range(6, -1, -1)]
            bucket_kind = 'day'
        elif period == 'month':
            cur_start = now_local - timedelta(days=30)
            prev_start = now_local - timedelta(days=60)
            buckets = [today - timedelta(days=i) for i in range(29, -1, -1)]
            bucket_kind = 'day'
        else:  # day
            cur_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            prev_start = cur_start - timedelta(days=1)
            buckets = list(range(24))
            bucket_kind = 'hour'
        cur_start_utc = cur_start.astimezone(UTC).replace(tzinfo=None)
        prev_start_utc = prev_start.astimezone(UTC).replace(tzinfo=None)
        umap = _key_user_map(conn)
        cur = conn.cursor()
        bexpr = ("EXTRACT(HOUR FROM ((\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE %s))::int"
                 if bucket_kind == 'hour'
                 else "((\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE %s)::date")
        # Période courante : par bucket + clé (vrais tokens + répartition prompt/généré)
        cur.execute(
            f'SELECT {bexpr} AS b, api_key, SUM(prompt_tokens), SUM(completion_tokens) '
            'FROM "LiteLLM_SpendLogs" WHERE "startTime" >= %s GROUP BY b, api_key',
            (LOCAL_TZ, cur_start_utc))
        agg = {}
        for b, api_key, prompt, comp in cur.fetchall():
            if api_key in _NON_USER_KEYS:
                continue
            u = umap.get(api_key, 'inconnu')
            a = agg.setdefault(u, {'tokens': 0, 'prompt': 0, 'completion': 0, 'spark': {}})
            tok = (prompt or 0) + (comp or 0)
            a['tokens'] += tok; a['prompt'] += prompt or 0; a['completion'] += comp or 0
            if tok:
                a['spark'][b] = a['spark'].get(b, 0) + tok
        # Période précédente : total par clé (pour le delta) — vrais tokens
        cur.execute('SELECT api_key, SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0)) '
                    'FROM "LiteLLM_SpendLogs" '
                    'WHERE "startTime" >= %s AND "startTime" < %s GROUP BY api_key',
                    (prev_start_utc, cur_start_utc))
        prev = {}
        for api_key, toks in cur.fetchall():
            if api_key in _NON_USER_KEYS:
                continue
            u = umap.get(api_key, 'inconnu')
            prev[u] = prev.get(u, 0) + (toks or 0)
        items = sorted([(u, a) for u, a in agg.items() if a['tokens'] > 0],
                       key=lambda x: x[1]['tokens'], reverse=True)
        series = _series_for([u for u, _ in items])
        top = items[0][1]['tokens'] if items else 0
        rows = []
        for i, (u, a) in enumerate(items):
            pv = prev.get(u, 0)
            delta = ((a['tokens'] - pv) / pv * 100) if pv > 0 else None
            spark = [a['spark'].get(b, 0) for b in buckets]
            rows.append({
                'rank': i + 1, 'username': u, 'series': series[u], 'is_me': u == me,
                'tokens': a['tokens'], 'prompt': int(a['prompt']), 'completion': int(a['completion']),
                'delta': delta, 'bar_pct': (a['tokens'] / top * 100) if top else 0,
                'spark_pts': _spark_points(spark),
            })
        return {'period': period, 'rows': rows, 'active_count': len(rows)}
    except Exception:
        return empty
    finally:
        conn.close()

def user_hourly(username):
    """24 points horaires (vrais tokens consommés = prompt + généré) d'aujourd'hui
    pour l'utilisateur, + total, pic horaire et nombre de clés actives dans la
    journée. On affiche les tokens réels, pas le coût pondéré (input×0,1) qui
    sous-estime la conso d'un facteur ~10 sur les charges à gros prompt."""
    conn = _spend_conn()
    if not conn:
        return None
    empty = {'has_data': False, 'points': [{'hour': h, 'tokens': 0} for h in range(24)],
             'total': 0, 'peak_hour': 0, 'peak_val': 0, 'active_keys': 0}
    try:
        umap = _key_user_map(conn)
        my_keys = {tok for tok, u in umap.items() if u == username}
        if not my_keys:
            return empty
        cur = conn.cursor()
        cur.execute(
            'SELECT EXTRACT(HOUR FROM (("startTime" AT TIME ZONE \'UTC\') AT TIME ZONE %s))::int AS h, '
            'api_key, SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0)) '
            'FROM "LiteLLM_SpendLogs" '
            'WHERE api_key = ANY(%s) '
            '  AND (("startTime" AT TIME ZONE \'UTC\') AT TIME ZONE %s)::date '
            '      = (now() AT TIME ZONE %s)::date '
            'GROUP BY h, api_key', (LOCAL_TZ, list(my_keys), LOCAL_TZ, LOCAL_TZ))
        by_hour = {h: 0 for h in range(24)}
        active = set()
        for h, api_key, toks in cur.fetchall():
            by_hour[h] += (toks or 0)
            if toks:
                active.add(api_key)
        peak_hour = max(range(24), key=lambda h: by_hour[h])
        total = sum(by_hour.values())
        return {'has_data': total > 0,
                'points': [{'hour': h, 'tokens': round(by_hour[h])} for h in range(24)],
                'total': round(total), 'peak_hour': peak_hour,
                'peak_val': round(by_hour[peak_hour]), 'active_keys': len(active)}
    except Exception:
        return empty
    finally:
        conn.close()

@app.route('/usage/hourly')
@login_required
def usage_hourly():
    return jsonify(user_hourly(session['username']) or {'has_data': False})

@app.route('/system/stats')
@login_required
def system_stats():
    data = runner_metrics() or {}
    data['model'] = vllm_health()
    data['running'] = get_running_models()
    if session.get('is_admin'):
        data['runner'] = runner_status()
        data['active_users'] = _active_users()
    return jsonify(data)

@app.route('/admin/consumption')
@admin_required
def admin_consumption():
    return jsonify({'users': admin_get_user_consumption()})

@app.route('/admin')
@admin_required
def admin():
    db = get_db()
    all_reqs    = db.execute("SELECT * FROM model_requests ORDER BY created_at DESC").fetchall()
    model_cfgs  = db.execute("SELECT * FROM model_configs ORDER BY name").fetchall()
    budget_reqs = db.execute("SELECT * FROM budget_requests ORDER BY created_at DESC").fetchall()
    running     = get_running_models()
    v_status    = runner_status()
    init_logs   = runner_logs(120)
    spend_data  = admin_get_user_consumption()
    stats = {
        'pending':  sum(1 for r in all_reqs if r['status'] == 'pending'),
        'done':     sum(1 for r in all_reqs if r['status'] == 'done'),
        'rejected': sum(1 for r in all_reqs if r['status'] == 'rejected'),
        'budget_pending': sum(1 for r in budget_reqs if r['status'] == 'pending'),
    }
    return render_template('admin.html', requests=all_reqs, running_models=running,
                           stats=stats, spend_data=spend_data,
                           model_cfgs=model_cfgs, v_status=v_status,
                           init_logs=init_logs, budget_reqs=budget_reqs,
                           default_key_budget=get_setting('default_key_budget', KEY_BUDGET),
                           default_key_duration=get_setting('default_key_duration', KEY_DURATION))

@app.route('/admin/model/launch', methods=['POST'])
@admin_required
def launch_model():
    name = request.form.get('model_name', '').strip()
    db   = get_db()
    cfg  = db.execute("SELECT * FROM model_configs WHERE name=?", (name,)).fetchone()
    if not cfg:
        flash("Modèle introuvable.", "danger")
        return redirect(url_for('admin'))
    ok = runner_launch(cfg['hf_model_id'], cfg['name'], cfg['vllm_args'] or '',
                       cfg['engine'] or 'vllm')
    if ok:
        _announce_launch(cfg['name'])
    flash(f"Lancement de {name} en cours…" if ok else "Runner inaccessible (ou moteur indisponible).",
          "success" if ok else "danger")
    return redirect(url_for('admin'))

@app.route('/api/announcements')
@login_required
def api_announcements():
    db = get_db()
    row = db.execute("SELECT last_seen_id FROM announcement_state WHERE username=?",
                     (session['username'],)).fetchone()
    seen = row['last_seen_id'] if row else 0
    rows = db.execute(
        "SELECT id, kind, a, b, created_at FROM announcements WHERE id > ? "
        "ORDER BY id DESC LIMIT 6", (seen,)).fetchall()
    return {'items': [dict(r) for r in rows]}

@app.route('/api/announcements/seen', methods=['POST'])
@login_required
def api_announcements_seen():
    db = get_db()
    mx = db.execute("SELECT COALESCE(MAX(id), 0) AS m FROM announcements").fetchone()['m']
    db.execute(
        "INSERT INTO announcement_state (username, last_seen_id) VALUES (?, ?) "
        "ON CONFLICT(username) DO UPDATE SET last_seen_id=excluded.last_seen_id",
        (session['username'], mx))
    db.commit()
    return {'ok': True}

@app.route('/admin/announce', methods=['POST'])
@admin_required
def admin_announce():
    title = request.form.get('title', '').strip()[:120]
    body  = request.form.get('body', '').strip()[:600]
    if not title:
        flash("Titre requis pour l'annonce.", "warning")
        return redirect(url_for('admin'))
    add_announcement('site', title, body)
    flash("Annonce publiée — elle s'affichera à l'ouverture du site.", "success")
    return redirect(url_for('admin'))

@app.route('/admin/model/stop', methods=['POST'])
@admin_required
def stop_model():
    ok = runner_stop()
    flash("Modèle arrêté." if ok else "Runner vLLM inaccessible.", "success" if ok else "danger")
    return redirect(url_for('admin'))

@app.route('/admin/model/add', methods=['POST'])
@admin_required
def add_model_cfg():
    name   = re.sub(r'[^a-zA-Z0-9_-]', '-', request.form.get('name', '').strip())[:40]
    hf_id  = request.form.get('hf_model_id', '').strip()
    args   = request.form.get('vllm_args', '').strip()
    engine = request.form.get('engine', 'vllm').strip().lower()
    if engine not in ('vllm', 'llamacpp', 'ds4'):
        engine = 'vllm'
    if not name or not hf_id:
        flash("Nom et HF model ID requis.", "warning")
        return redirect(url_for('admin'))
    db = get_db()
    try:
        db.execute("INSERT INTO model_configs (name, hf_model_id, vllm_args, engine, added_at) "
                   "VALUES (?,?,?,?,?)",
                   (name, hf_id, args, engine, datetime.now().isoformat()))
        db.commit()
        add_announcement('model_add', name)
        ok = _register_litellm_model(name, args, engine)
        flash(f"Modèle {name} ajouté ({engine}) et routé par LiteLLM." if ok
              else f"Modèle {name} ajouté (⚠ enregistrement LiteLLM échoué).", "success" if ok else "warning")
    except sqlite3.IntegrityError:
        flash("Un modèle avec ce nom existe déjà.", "danger")
    return redirect(url_for('admin'))

@app.route('/admin/model/edit/<int:mid>', methods=['POST'])
@admin_required
def edit_model_cfg(mid):
    args = request.form.get('vllm_args', '').strip()
    db = get_db()
    db.execute("UPDATE model_configs SET vllm_args=? WHERE id=?", (args, mid))
    db.commit()
    row = db.execute("SELECT name, engine FROM model_configs WHERE id=?", (mid,)).fetchone()
    if row:
        _register_litellm_model(row['name'], args, row['engine'] or 'vllm')
    flash("Args du modèle mis à jour (routage LiteLLM rafraîchi).", "success")
    return redirect(url_for('admin'))

@app.route('/admin/model/delete/<int:mid>', methods=['POST'])
@admin_required
def delete_model_cfg(mid):
    db = get_db()
    row = db.execute("SELECT name FROM model_configs WHERE id=?", (mid,)).fetchone()
    db.execute("DELETE FROM model_configs WHERE id=?", (mid,))
    db.commit()
    if row:
        _unregister_litellm_model(row['name'])
    flash("Modèle supprimé (retiré de LiteLLM).", "success")
    return redirect(url_for('admin'))

@app.route('/admin/settings', methods=['POST'])
@admin_required
def update_settings():
    budget   = request.form.get('default_key_budget', '').strip()
    duration = request.form.get('default_key_duration', '').strip()
    try:
        budget_val = float(budget)
        if budget_val <= 0:
            raise ValueError
    except ValueError:
        flash("Le nombre de tokens par défaut doit être un nombre positif.", "warning")
        return redirect(url_for('admin'))
    if not re.match(r'^\d+[smhd]$', duration):
        flash("Durée invalide (ex: 1d, 7d, 30d, 12h).", "warning")
        return redirect(url_for('admin'))
    set_setting('default_key_budget', budget_val)
    set_setting('default_key_duration', duration)
    flash(f"Limite globale mise à jour : {budget_val:,.0f} tokens / {duration}.".replace(',', ' '), "success")
    return redirect(url_for('admin'))

@app.route('/admin/budget/approve/<int:req_id>', methods=['POST'])
@admin_required
def approve_budget(req_id):
    amount = request.form.get('amount', '').strip()
    db = get_db()
    breq = db.execute("SELECT * FROM budget_requests WHERE id=?", (req_id,)).fetchone()
    if not breq or breq['status'] != 'pending':
        flash("Demande introuvable ou déjà traitée.", "warning")
        return redirect(url_for('admin'))
    try:
        amount_val = float(amount)
        if amount_val <= 0:
            raise ValueError
    except ValueError:
        flash("Le montant à ajouter doit être un nombre positif.", "warning")
        return redirect(url_for('admin'))
    # Budget au niveau COMPTE : on incrémente l'enveloppe de l'utilisateur LiteLLM.
    info = _litellm_user_info(breq['username'])
    current_budget = info.get('max_budget') or 0
    new_budget = current_budget + amount_val
    if not litellm_update_user_budget(breq['username'], new_budget):
        flash("Erreur lors de la mise à jour du budget sur LiteLLM.", "danger")
        return redirect(url_for('admin'))
    db.execute(
        "UPDATE budget_requests SET status='approved', granted_amount=?, updated_at=? WHERE id=?",
        (amount_val, datetime.now().isoformat(), req_id)
    )
    db.commit()
    flash(f"+{amount_val:,.0f} tokens accordés à {breq['fullname']} (nouveau total : {new_budget:,.0f}).".replace(',', ' '), "success")
    return redirect(url_for('admin'))

@app.route('/admin/budget/reject/<int:req_id>', methods=['POST'])
@admin_required
def reject_budget(req_id):
    db = get_db()
    db.execute(
        "UPDATE budget_requests SET status='rejected', updated_at=? WHERE id=?",
        (datetime.now().isoformat(), req_id)
    )
    db.commit()
    flash("Demande rejetée.", "success")
    return redirect(url_for('admin'))

@app.route('/admin/runner/logs')
@admin_required
def admin_runner_logs():
    return jsonify({'logs': runner_logs(200)})

@app.route('/admin/runner/stream')
@admin_required
def admin_runner_stream():
    # Le navigateur ne peut pas parler directement à vllm-runner (port 8001) :
    # ce port est restreint au bridge Docker + localhost, et EventSource ne peut
    # pas poser de header Authorization. dgx-portal, lui, est sur le bridge et a
    # le token — on relaie donc le flux SSE ici, en interne, sans jamais exposer
    # RUNNER_TOKEN au navigateur.
    upstream = requests.get(f"{RUNNER_URL}/stream", headers=_runner_headers(),
                            stream=True, timeout=(5, None))

    def generate():
        buf = ''
        try:
            for chunk in upstream.iter_content(chunk_size=None, decode_unicode=True):
                if not chunk:
                    continue
                buf += chunk
                while '\n\n' in buf:
                    evt, buf = buf.split('\n\n', 1)
                    data_line = next((l for l in evt.split('\n') if l.startswith('data:')), '')
                    if _LOG_NOISE_RE.search(data_line):
                        continue                 # ligne d'accès de routine → on n'affiche pas
                    yield evt + '\n\n'
        finally:
            upstream.close()

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return Response(stream_with_context(generate()), mimetype="text/event-stream", headers=headers)

# Args vLLM prudents par défaut pour un modèle validé (à ajuster ensuite).
# max-model-len volontairement conservateur (mémoire unifiée GB10 → risque OOM
# si on laisse la fenêtre native du modèle).
# Tool-calling activé par défaut (parser qwen3_coder = flotte Qwen). Pour un modèle
# non-Qwen, ajuster --tool-call-parser (ex. hermes) depuis l'admin avant de lancer.
DEFAULT_VLLM_ARGS = "--enable-auto-tool-choice --tool-call-parser qwen3_coder --dtype bfloat16 --max-model-len 32768 --gpu-memory-utilization 0.7 --max-num-seqs 4"
# llama.cpp : -ngl 999 = tout le modèle sur le GPU ; --jinja active les templates
# de chat et le tool-calling ; --parallel = sessions concurrentes (équiv. max-num-seqs).
DEFAULT_LLAMA_ARGS = "--ctx-size 32768 --n-gpu-layers 999 --parallel 4 --flash-attn --jinja"

def _model_slug(hf_id):
    base = (hf_id or '').split('/')[-1]
    return (re.sub(r'[^a-zA-Z0-9_-]', '-', base).strip('-').lower()[:40]) or 'modele'

VLLM_API_BASE = os.environ.get('VLLM_API_BASE', 'http://host.docker.internal:8000/v1')

def _litellm_model_id(name):
    """Id LiteLLM du modèle portant ce model_name, ou None."""
    try:
        r = requests.get(f"{LITELLM_URL}/model/info", headers=litellm_headers(), timeout=5)
        for m in r.json().get('data', []):
            if m.get('model_name') == name:
                return m.get('model_info', {}).get('id')
    except Exception:
        pass
    return None

def _register_litellm_model(name, vllm_args, engine='vllm'):
    """Enregistre (ou rafraîchit) le modèle dans LiteLLM à chaud. Le contexte est
    déduit des args du moteur (--max-model-len pour vLLM, --ctx-size pour llama.cpp).
    Les deux servent une API OpenAI sur :8000 → mêmes litellm_params."""
    if not LITELLM_KEY:
        return False
    ctx = ctx_of(vllm_args, engine) or 32768
    # ds4 part en mode « thinking » par défaut : il IGNORE alors max_tokens
    # (« client sampling knobs are ignored like the official API ») et génère des
    # milliers de tokens à ~10 tok/s. Comme le moteur est mono-slot, une seule
    # requête bloque toute la plateforme. On route donc vers le nom réservé
    # `deepseek-chat`, qui sélectionne le mode NON-thinking (cf. --help de ds4).
    upstream = 'deepseek-chat' if engine == 'ds4' else name
    body = {
        "model_name": name,
        "litellm_params": {
            "model": f"openai/{upstream}",
            "api_base": VLLM_API_BASE,
            "api_key": "dummy",
            "input_cost_per_token": 1,
            "output_cost_per_token": 1,
        },
        "model_info": {
            "mode": "chat",
            "supports_function_calling": True,
            "max_input_tokens": ctx,
            "max_output_tokens": min(ctx // 2, 262144),
        },
    }
    try:
        existing = _litellm_model_id(name)
        if existing:
            requests.post(f"{LITELLM_URL}/model/delete", headers=litellm_headers(),
                          json={"id": existing}, timeout=5)
        r = requests.post(f"{LITELLM_URL}/model/new", headers=litellm_headers(),
                          json=body, timeout=8)
        return r.status_code < 300
    except Exception:
        return False

def _unregister_litellm_model(name):
    if not LITELLM_KEY:
        return
    mid = _litellm_model_id(name)
    if mid:
        try:
            requests.post(f"{LITELLM_URL}/model/delete", headers=litellm_headers(),
                          json={"id": mid}, timeout=5)
        except Exception:
            pass

def hf_engine_for(hf_id):
    """Interroge le Hub pour savoir si le modèle est en GGUF (→ llama.cpp) ou en
    safetensors (→ vLLM). En cas d'échec réseau, on retombe sur vLLM."""
    try:
        r = requests.get(f'https://huggingface.co/api/models/{hf_id}', timeout=6)
        if r.ok:
            return guess_engine(r.json())
    except Exception:
        pass
    return 'vllm'

def _add_model_to_catalog(db, hf_id):
    """Ajoute un modèle validé au catalogue lançable (nom unique). Retourne
    (nom, déjà_présent). Le moteur est déduit des tags HF."""
    row = db.execute("SELECT name FROM model_configs WHERE hf_model_id=?", (hf_id,)).fetchone()
    if row:
        return row['name'], True
    base = _model_slug(hf_id)
    name = base
    n = 2
    while db.execute("SELECT 1 FROM model_configs WHERE name=?", (name,)).fetchone():
        name = f"{base}-{n}"; n += 1
    engine = hf_engine_for(hf_id)
    args = DEFAULT_LLAMA_ARGS if engine == 'llamacpp' else DEFAULT_VLLM_ARGS
    db.execute("INSERT INTO model_configs (name, hf_model_id, vllm_args, engine, added_at) "
               "VALUES (?,?,?,?,?)",
               (name, hf_id, args, engine, datetime.now().isoformat()))
    return name, False

@app.route('/admin/update/<int:req_id>', methods=['POST'])
@admin_required
def update_request(req_id):
    status = request.form.get('status')
    if status not in ('pending', 'done', 'rejected'):
        flash("Statut invalide.", "danger")
        return redirect(url_for('admin'))
    db = get_db()
    db.execute("UPDATE model_requests SET status=?, updated_at=? WHERE id=?",
               (status, datetime.now().isoformat(), req_id))
    # Valider une demande = l'ajouter au catalogue lançable (comme les modèles seedés).
    if status == 'done':
        req = db.execute("SELECT username, model_id FROM model_requests WHERE id=?", (req_id,)).fetchone()
        if req and req['model_id']:
            # Prévient le demandeur par email que son modèle est dispo.
            email = ldap_lookup_email(req['username'])
            if email:
                send_user_email(email, "[Cronos] Ton modèle est disponible",
                                f"Bonne nouvelle — le modèle que tu as demandé est validé et "
                                f"disponible sur la plateforme Cronos :\n\n  {req['model_id']}\n\n"
                                f"Tu peux l'utiliser via l'API / le Playground une fois lancé.\n"
                                f"https://dgx.cronos.website/\n")
            name, existed = _add_model_to_catalog(db, req['model_id'])
            cfg = db.execute("SELECT vllm_args, engine FROM model_configs WHERE name=?", (name,)).fetchone()
            ok = _register_litellm_model(name, cfg['vllm_args'] if cfg else DEFAULT_VLLM_ARGS,
                                         (cfg['engine'] if cfg else 'vllm') or 'vllm')
            routed = "" if ok else " (⚠ enregistrement LiteLLM échoué — à vérifier)"
            if existed:
                flash(f"Modèle déjà dans le catalogue sous « {name} ».{routed}", "info")
            else:
                add_announcement('model_add', name)
                flash(f"Modèle « {name} » ajouté au catalogue et routé par LiteLLM — vérifie ses args vLLM puis lance-le.{routed}", "success")
    db.commit()
    return redirect(url_for('admin'))

with app.app_context():
    init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
