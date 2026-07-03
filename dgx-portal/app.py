import os, sqlite3, smtplib, requests, time, re
from flask import Flask, render_template, request, session, redirect, url_for, flash, g, jsonify, Response, stream_with_context
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


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'same-origin')
    return resp

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
            key_alias  TEXT NOT NULL UNIQUE,
            key_value  TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS model_configs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            hf_model_id TEXT NOT NULL,
            vllm_args   TEXT DEFAULT '',
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
    ''')
    db.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)",
        ('default_key_budget', str(KEY_BUDGET))
    )
    db.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)",
        ('default_key_duration', KEY_DURATION)
    )
    ORNITH_ARGS = "--enable-auto-tool-choice --tool-call-parser qwen3_coder --dtype bfloat16 --max-model-len 262144 --gpu-memory-utilization 0.7 --max-num-seqs 8"
    QWEN_ARGS   = "--enable-auto-tool-choice --tool-call-parser qwen3_coder --dtype bfloat16 --max-model-len 32768 --gpu-memory-utilization 0.8 --max-num-seqs 8"
    now = datetime.now().isoformat()
    db.executemany(
        "INSERT OR IGNORE INTO model_configs (name, hf_model_id, vllm_args, added_at) VALUES (?,?,?,?)",
        [
            ("ornith-35b-fp8",  "deepreinforce-ai/Ornith-1.0-35B-FP8",  ORNITH_ARGS, now),
            ("qwen3-coder-30b", "Qwen/Qwen3-Coder-30B-A3B-Instruct",     QWEN_ARGS,   now),
        ]
    )
    # Toujours mettre à jour les args des modèles pré-configurés
    db.execute("UPDATE model_configs SET hf_model_id=?, vllm_args=? WHERE name=?",
               ("deepreinforce-ai/Ornith-1.0-35B-FP8", ORNITH_ARGS, "ornith-35b-fp8"))
    db.execute("UPDATE model_configs SET hf_model_id=?, vllm_args=? WHERE name=?",
               ("Qwen/Qwen3-Coder-30B-A3B-Instruct", QWEN_ARGS, "qwen3-coder-30b"))
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

def get_running_models():
    try:
        r = requests.get(f"{VLLM_API}/models", timeout=3)
        if r.ok:
            return [m['id'] for m in r.json().get('data', [])]
    except Exception:
        pass
    return []

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

def create_litellm_key(alias, username, is_admin=False):
    payload = {
        "key_alias": alias,
        "metadata": {"user": username, "created_by": "dgx-portal"},
    }
    if not is_admin:
        payload["max_budget"] = float(get_setting('default_key_budget', KEY_BUDGET))
        payload["budget_duration"] = get_setting('default_key_duration', KEY_DURATION)
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
            return r.json()
    except Exception:
        pass
    return {'status': 'unreachable', 'model': None, 'pid': None}

def runner_launch(hf_model_id, model_name, vllm_args=''):
    try:
        r = requests.post(f"{RUNNER_URL}/launch",
                          headers=_runner_headers(),
                          json={'hf_model_id': hf_model_id, 'model_name': model_name, 'vllm_args': vllm_args},
                          timeout=5)
        return r.ok
    except Exception:
        return False

def runner_stop():
    try:
        r = requests.post(f"{RUNNER_URL}/stop", headers=_runner_headers(), timeout=5)
        return r.ok
    except Exception:
        return False

def runner_logs(n=150):
    try:
        r = requests.get(f"{RUNNER_URL}/logs", headers=_runner_headers(), params={'n': n}, timeout=3)
        if r.ok:
            return r.json().get('logs', [])
    except Exception:
        pass
    return []

def search_hf_models(query, task='text-generation'):
    try:
        r = requests.get(
            'https://huggingface.co/api/models',
            params={'search': query, 'filter': task, 'limit': 24,
                    'sort': 'downloads', 'direction': -1},
            timeout=8
        )
        if r.ok:
            return r.json()
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

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'username' in session:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        ok, is_admin, fullname = ldap_authenticate(username, password)
        if ok:
            _apply_session(username, fullname, is_admin, via_sso=False)
            return redirect(_safe_next(request.args.get('next')))
        flash("Identifiants incorrects.", "danger")
    return render_template('login.html', oidc_enabled=OIDC_ENABLED)


def _safe_next(target):
    """N'autorise que les redirections vers un chemin local relatif — bloque
    l'open redirect (?next=https://evil.com ou //evil.com)."""
    if not target:
        return url_for('index')
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith('/') or target.startswith('//'):
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
                           public_api_url=PUBLIC_API_URL, usage=user_usage(session['username']),
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
            key_alias = request.form.get('key_alias', '').strip()
            reason    = request.form.get('reason', '').strip()
            current   = next((k['max_budget'] for k in user_keys if k['key_alias'] == key_alias), None)
            if not key_alias:
                flash("Clé invalide.", "warning")
                return redirect(url_for('keys'))
            db = get_db()
            existing = db.execute(
                "SELECT id FROM budget_requests WHERE username=? AND key_alias=? AND status='pending'",
                (session['username'], key_alias)
            ).fetchone()
            if existing:
                flash("Tu as déjà une demande en attente pour cette clé.", "warning")
                return redirect(url_for('keys'))
            db.execute(
                "INSERT INTO budget_requests (username, fullname, key_alias, current_budget, reason, status, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (session['username'], session['fullname'], key_alias, current, reason, 'pending',
                 datetime.now().isoformat())
            )
            db.commit()
            notify_budget_discord(session['username'], session['fullname'], key_alias, current, reason)
            notify_budget_email(session['username'], session['fullname'], key_alias, current, reason)
            flash("Demande de tokens envoyée !", "success")
    running = get_running_models()
    default_budget = float(get_setting('default_key_budget', KEY_BUDGET))
    # Limites de contexte par modèle (déduites de --max-model-len), pour les snippets
    # d'intégration qui déclarent la fenêtre côté client (OpenCode/OpenChamber).
    model_limits = {}
    for row in get_db().execute("SELECT name, vllm_args FROM model_configs"):
        mm = re.search(r'--max-model-len\s+(\d+)', row['vllm_args'] or '')
        if mm:
            ctx = int(mm.group(1))
            model_limits[row['name']] = {'context': ctx, 'output': min(ctx // 4, 65536)}
    return render_template('keys.html', user_keys=user_keys, new_key_alias=new_key_alias,
                           budget_tokens=f"{default_budget:,.0f}".replace(',', ' '),
                           budget_duration=get_setting('default_key_duration', KEY_DURATION),
                           model_limits=model_limits,
                           running_models=running, public_api_url=PUBLIC_API_URL)

@app.route('/search')
@login_required
def search():
    query = request.args.get('q', '').strip()
    task  = request.args.get('task', 'text-generation')
    results = search_hf_models(query, task) if query else []
    return render_template('search.html', results=results, query=query, task=task)

@app.route('/ranking')
@login_required
def ranking():
    period = request.args.get('period', 'day')
    if period not in ('day', 'week', 'month'):
        period = 'day'
    raw = ranking_data(period)
    series = _series_for([u for u, _ in raw])
    top = raw[0][1] if raw else 0
    rows = [{
        'rank': i + 1,
        'username': u,
        'spend': s,
        'pct': (s / top * 100) if top else 0,
        'series': series[u],
        'is_me': u == session['username'],
    } for i, (u, s) in enumerate(raw)]
    labels = {'day': "Aujourd'hui", 'week': '7 derniers jours', 'month': '30 derniers jours'}
    return render_template('ranking.html', rows=rows, period=period, period_label=labels[period])

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

def admin_get_all_keys_spend():
    """Récupère toutes les clés (DB locale) + info spend/budget depuis LiteLLM."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        local_keys = conn.execute(
            "SELECT username, key_alias, key_value FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
    except Exception:
        return []
    result = []
    for k in local_keys:
        info = {'username': k['username'], 'key_alias': k['key_alias'],
                'spend': 0, 'max_budget': None, 'budget_duration': None, 'budget_reset_at': None}
        try:
            r = requests.get(f"{LITELLM_URL}/key/info",
                             headers=litellm_headers(),
                             params={"key": k['key_value']}, timeout=3)
            if r.ok:
                d = r.json().get('info', {})
                info['spend']          = d.get('spend', 0)
                info['max_budget']     = d.get('max_budget')
                info['budget_duration'] = d.get('budget_duration')
                info['budget_reset_at'] = d.get('budget_reset_at', '')
        except Exception:
            pass
        result.append(info)
    return result

def admin_get_user_consumption():
    """Agrège spend/budget par utilisateur (somme sur toutes ses clés)."""
    per_key = admin_get_all_keys_spend()
    users = {}
    for k in per_key:
        u = users.setdefault(k['username'], {'username': k['username'], 'spend': 0,
                                               'max_budget': 0, 'unlimited': False, 'key_count': 0})
        u['spend'] += k['spend'] or 0
        u['key_count'] += 1
        if k['max_budget'] is None:
            u['unlimited'] = True
        else:
            u['max_budget'] += k['max_budget']
    return sorted(users.values(), key=lambda u: u['spend'], reverse=True)


# ── Statistiques de consommation (base LiteLLM Postgres) ─────────────────────
# La colonne SpendLogs.spend applique déjà la pondération input×0.1 + output×1
# (= "coût pondéré" / budget). startTime est en UTC → converti en LOCAL_TZ.

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

def ranking_data(period='day'):
    """[(username, spend)] trié décroissant sur la fenêtre choisie."""
    conn = _spend_conn()
    if not conn:
        return []
    try:
        now_local = datetime.now(ZoneInfo(LOCAL_TZ))
        if period == 'week':
            cutoff = (now_local - timedelta(days=7))
        elif period == 'month':
            cutoff = (now_local - timedelta(days=30))
        else:  # day = depuis minuit local aujourd'hui
            cutoff = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff_utc = cutoff.astimezone(ZoneInfo('UTC')).replace(tzinfo=None)
        umap = _key_user_map(conn)
        cur = conn.cursor()
        cur.execute('SELECT api_key, SUM(spend) FROM "LiteLLM_SpendLogs" '
                    'WHERE "startTime" >= %s GROUP BY api_key', (cutoff_utc,))
        totals = {}
        for api_key, spend in cur.fetchall():
            if api_key in _NON_USER_KEYS:
                continue
            user = umap.get(api_key, 'inconnu')
            totals[user] = totals.get(user, 0) + (spend or 0)
        return sorted([t for t in totals.items() if t[1] > 0], key=lambda x: x[1], reverse=True)
    except Exception:
        return []
    finally:
        conn.close()

def hourly_data():
    """Consommation d'aujourd'hui par heure locale, empilée par utilisateur."""
    conn = _spend_conn()
    if not conn:
        return None
    try:
        umap = _key_user_map(conn)
        cur = conn.cursor()
        cur.execute(
            'SELECT EXTRACT(HOUR FROM (("startTime" AT TIME ZONE \'UTC\') AT TIME ZONE %s))::int AS h, '
            'api_key, SUM(spend) FROM "LiteLLM_SpendLogs" '
            'WHERE (("startTime" AT TIME ZONE \'UTC\') AT TIME ZONE %s)::date '
            '    = (now() AT TIME ZONE %s)::date '
            'GROUP BY h, api_key', (LOCAL_TZ, LOCAL_TZ, LOCAL_TZ))
        by_hour = {h: {} for h in range(24)}
        users = set()
        for h, api_key, spend in cur.fetchall():
            if api_key in _NON_USER_KEYS or not spend:
                continue
            user = umap.get(api_key, 'inconnu')
            by_hour[h][user] = by_hour[h].get(user, 0) + spend
            users.add(user)
        series = _series_for(users)
        max_total = max((sum(v.values()) for v in by_hour.values()), default=0) or 1
        hours = []
        peak_hour, peak_val = 0, 0
        for h in range(24):
            total = sum(by_hour[h].values())
            if total > peak_val:
                peak_val, peak_hour = total, h
            segs = [{'user': u, 'spend': s, 'h_pct': s / max_total * 100, 'series': series[u]}
                    for u, s in sorted(by_hour[h].items(), key=lambda x: x[0])]
            hours.append({'hour': h, 'total': total, 'segments': segs})
        legend = [{'user': u, 'series': series[u]} for u in sorted(users)]
        return {'hours': hours, 'legend': legend, 'max_total': max_total,
                'peak_hour': peak_hour, 'has_data': peak_val > 0}
    except Exception:
        return None
    finally:
        conn.close()

def user_usage(username, days=14):
    """Consommation personnelle : totaux jour/semaine/mois + série quotidienne
    (coût pondéré) sur les `days` derniers jours, pour l'utilisateur connecté."""
    conn = _spend_conn()
    if not conn:
        return None
    try:
        umap = _key_user_map(conn)
        # tokens(hash) appartenant à cet utilisateur
        my_keys = {tok for tok, u in umap.items() if u == username}
        if not my_keys:
            return {'has_data': False, 'today': 0, 'week': 0, 'month': 0, 'daily': []}
        cur = conn.cursor()
        cur.execute(
            'SELECT (("startTime" AT TIME ZONE \'UTC\') AT TIME ZONE %s)::date AS d, '
            'api_key, SUM(spend) FROM "LiteLLM_SpendLogs" '
            'WHERE api_key = ANY(%s) '
            '  AND (("startTime" AT TIME ZONE \'UTC\') AT TIME ZONE %s)::date '
            '      >= (now() AT TIME ZONE %s)::date - INTERVAL \'29 days\' '
            'GROUP BY d, api_key', (LOCAL_TZ, list(my_keys), LOCAL_TZ, LOCAL_TZ))
        by_date = {}
        for d, api_key, spend in cur.fetchall():
            by_date[d] = by_date.get(d, 0) + (spend or 0)
        today = datetime.now(ZoneInfo(LOCAL_TZ)).date()
        totals = {'today': 0, 'week': 0, 'month': 0}
        for d, s in by_date.items():
            delta = (today - d).days
            totals['month'] += s
            if delta < 7:
                totals['week'] += s
            if delta == 0:
                totals['today'] += s
        chart_days = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
        vals = [by_date.get(d, 0) for d in chart_days]
        mx = max(vals) or 1
        daily = [{'label': d.strftime('%d/%m'), 'dow': d.strftime('%a'),
                  'spend': v, 'h_pct': v / mx * 100}
                 for d, v in zip(chart_days, vals)]
        return {'has_data': totals['month'] > 0, 'today': totals['today'],
                'week': totals['week'], 'month': totals['month'], 'daily': daily}
    except Exception:
        return None
    finally:
        conn.close()

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
    init_logs   = runner_logs(300)
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
                           hourly=hourly_data(),
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
    ok = runner_launch(cfg['hf_model_id'], cfg['name'], cfg['vllm_args'] or '')
    flash(f"Lancement de {name} en cours…" if ok else "Runner vLLM inaccessible.", "success" if ok else "danger")
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
    name  = re.sub(r'[^a-zA-Z0-9_-]', '-', request.form.get('name', '').strip())[:40]
    hf_id = request.form.get('hf_model_id', '').strip()
    args  = request.form.get('vllm_args', '').strip()
    if not name or not hf_id:
        flash("Nom et HF model ID requis.", "warning")
        return redirect(url_for('admin'))
    db = get_db()
    try:
        db.execute("INSERT INTO model_configs (name, hf_model_id, vllm_args, added_at) VALUES (?,?,?,?)",
                   (name, hf_id, args, datetime.now().isoformat()))
        db.commit()
        flash(f"Modèle {name} ajouté.", "success")
    except sqlite3.IntegrityError:
        flash("Un modèle avec ce nom existe déjà.", "danger")
    return redirect(url_for('admin'))

@app.route('/admin/model/delete/<int:mid>', methods=['POST'])
@admin_required
def delete_model_cfg(mid):
    db = get_db()
    db.execute("DELETE FROM model_configs WHERE id=?", (mid,))
    db.commit()
    flash("Modèle supprimé.", "success")
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
    key_row = db.execute(
        "SELECT key_value FROM api_keys WHERE username=? AND key_alias=?",
        (breq['username'], breq['key_alias'])
    ).fetchone()
    if not key_row:
        flash("Clé introuvable (peut-être révoquée depuis).", "danger")
        return redirect(url_for('admin'))
    info = litellm_key_info(key_row['key_value'])
    current_budget = info.get('max_budget') or 0
    new_budget = current_budget + amount_val
    if not litellm_update_key_budget(key_row['key_value'], new_budget):
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
        try:
            for chunk in upstream.iter_content(chunk_size=None):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return Response(stream_with_context(generate()), mimetype="text/event-stream", headers=headers)

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
    db.commit()
    flash("Statut mis à jour.", "success")
    return redirect(url_for('admin'))

with app.app_context():
    init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
