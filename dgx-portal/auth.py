"""Gardes d'authentification et duree de vie des sessions.

Extrait de app.py le 28/08 — TROISIEME piece du noyau, apres db.py et config.py,
et celle sans laquelle aucun blueprint n'etait possible : un module de routes doit
importer login_required/admin_required, or ces decorateurs vivaient dans app.py,
que ce meme module ne peut pas reimporter.

Ne depend que de flask et de l'environnement. url_for('login') et url_for('index')
sont resolus A L'APPEL, pas a l'import : les routes correspondantes restent
enregistrees sur l'application dans app.py, donc rien a passer ici.
"""
import os
import time
from functools import wraps

import hmac
import ipaddress
import re
import secrets
import sqlite3

from flask import abort, flash, g, redirect, request, session, url_for
from ldap3 import ALL, SIMPLE, Connection, Server
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import escape_rdn

from config import (LDAP_BASE, LDAP_BIND_DN,
                    LDAP_BIND_PW, LDAP_URI)
from db import DB_PATH, get_db

_API_FETCH_PATHS = ('/playground/chat', '/support/chat', '/admin/runner/stream')


def _is_api_request():
    # Distinguishes fetch/JSON calls (Next.js driver) from classic navigation:
    # fetch() follows 302 redirects automatically and would return /login's HTML
    # with a 200 code, hiding the session expiry from the frontend.
    return request.path.startswith('/api/') or request.path in _API_FETCH_PATHS


# Absolute session lifetime (not inactivity: we don't extend it on
# each request, it really is a cap from login time). 12 h = one
# workday, the user reconnects the next day. Incidentally,
# this bounds how long a stale is_admin remains valid.
SESSION_MAX_AGE = int(os.environ.get('SESSION_MAX_AGE', 12 * 3600))


def _session_expired():
    if 'username' not in session:
        return False
    # Sessions created before auth_at was introduced: treated as
    # expired rather than eternal.
    if time.time() - session.get('auth_at', 0) > SESSION_MAX_AGE:
        return True
    # Registre serveur : la session n'est plus seulement limitée par son âge,
    # elle peut être révoquée à volonté (logout, compte verrouillé, admin).
    sid = session.get('sid')
    if not sid:
        # Session antérieure au registre (ou session de test sans sid) : on
        # conserve l'expiration par l'âge seule — elle n'est pas révocable mais
        # expirera naturellement. Les nouvelles sessions portent un sid et le
        # sont. Pas de déconnexion en masse lors de la migration.
        return False
    row = get_db().execute(
        "SELECT revoked, expires_at FROM user_sessions WHERE sid=?", (sid,)).fetchone()
    if row is None or row['revoked'] or row['expires_at'] < time.time():
        return True
    return False


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if _session_expired():
            session.clear()
        if 'username' not in session:
            if _is_api_request():
                abort(401)
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    # Marqueur lu a l'execution par le test de garde des routes : @wraps efface
    # toute trace du decorateur, donc sans lui il faudrait analyser le source —
    # fragile, et aveugle a une route enregistree autrement qu'en litteral.
    decorated._garde = 'login'
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if _session_expired():
            session.clear()
        if 'username' not in session:
            if _is_api_request():
                abort(401)
            return redirect(url_for('login'))
        if not session.get('is_admin'):
            if _is_api_request():
                abort(403)
            flash("Accès réservé aux administrateurs.", "danger")
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    decorated._garde = 'admin'          # cf. login_required
    return decorated


# ── Socle d'authentification, rapatrie de app.py le 28/08 ───────────────────
# CSRF, connexion de secours, LDAP, anti-force-brute et ouverture de session
# etaient disperses dans le monolithe sous quatre bannieres differentes. Ils
# forment pourtant UN sujet, et le blueprint d'administration en a besoin.
#
# _csrf_protect et _inject_csrf ont perdu leurs decorateurs @app.* : ils sont
# enregistres depuis app.py (before_request / context_processor), sinon il
# faudrait importer l'application ici — le cycle qu'on evite depuis db.py.
#
# La MECANIQUE de connexion de secours est deplacee telle quelle, sans une
# ligne de logique changee : LDAP et le SSO etant eteints, c'est le seul
# acces a la plateforme.

# ── Validation d'identifiant ──

USERNAME_RE = re.compile(r'^[a-zA-Z0-9._-]{1,64}$')

# ── Jeton CSRF par session ──

def _ensure_csrf():
    """Return the session token, creating it if needed.

    LAZY creation, and that's essential: doing it in before_request
    mutated the session on every request, so every response returned a
    Set-Cookie. On the login page, the browser fires /api/csrf and
    /api/whoami in parallel with no cookie; both then created a fresh
    session with a DIFFERENT token, the last Set-Cookie to arrive overwrote
    the other, and the token the page had memorized no longer matched the
    actually-stored cookie → POST /login as 400, shown to the user
    as "Invalid credentials". By touching the session only where the
    token is really requested, a single request can create it.

    """
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(32)
    return session['csrf']

def _csrf_protect():
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        sent = request.form.get('csrf_token') or request.headers.get('X-CSRFToken', '')
        expected = session.get('csrf')
        # .encode() required: hmac.compare_digest raises TypeError on
        # str containing non-ASCII, which would turn an exotic token
        # into a 500 instead of the expected 400. We compare bytes.
        if not expected or not hmac.compare_digest(str(expected).encode(), str(sent).encode()):
            abort(400, description='CSRF token manquant ou invalide.')

def _inject_csrf():
    return {'csrf_token': _ensure_csrf}

# ── Statut administrateur ──

_admin_username_cache = {}

def is_admin_username(username):
    """Admin status of an account, without an active session (used by
    /internal/authcheck, called by Traefik for EVERY external API request
    in maintenance mode — hence the cache, to avoid hitting LDAP each
    time).
    """
    now = time.time()
    cached = _admin_username_cache.get(username)
    if cached and now - cached[0] < 60:
        return cached[1]
    is_admin = (ldap_lookup_admin(username)
                or _local_user_admin(username))
    _admin_username_cache[username] = (now, is_admin)
    return is_admin


def _local_user_admin(username):
    """True if a managed local account (local_users table) is an admin.

    is_admin_username feeds /internal/authcheck, which decides whether an API key
    bypasses maintenance mode. It previously only looked at the plaintext debug
    admin list + LDAP, so an admin created through the local-users UI had a
    working web session (session['is_admin']) but their key was rejected (503)
    in maintenance mode — two sources of truth for "admin". This closes that gap.
    """
    try:
        row = get_db().execute(
            "SELECT * FROM local_users WHERE username=? AND enabled=1", (username,)).fetchone()
        if not row:
            return False
        if row['is_admin']:
            return True
        g = get_db().execute(
            "SELECT is_admin FROM user_groups WHERE name=?", (row['group_name'],)).fetchone()
        return bool(g and g['is_admin'])
    except Exception:
        return False

# ── LDAP ──

def _is_admin_group(dn):
    """True if one of the DN's RDN components is exactly cn=adm_cronos.
    Avoids the false positive of a plain `'adm_cronos' in dn` (which would match
    cn=adm_cronos_readonly, cn=notadm_cronos, etc.).
    """
    for part in dn.split(','):
        attr, _, val = part.strip().partition('=')
        if attr.strip().lower() == 'cn' and val.strip().lower() == 'adm_cronos':
            return True
    return False

def ldap_authenticate(username, password):
    """Return (ok, is_admin, display_name)."""
    # Strict rejection: an empty password triggers an LDAP "unauthenticated bind"
    # that succeeds on some directories → authentication bypass.
    # An identifier outside the allowed charset is refused before any LDAP access.
    if not password or not USERNAME_RE.match(username):
        return False, False, username
    try:
        server = Server(LDAP_URI, get_info=ALL)
        # Anti-injection escaping: RDN for the bind DN, filter for the search.
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

def ldap_lookup_admin(username):
    """Determines is_admin via an LDAP lookup by uid (service account).
    Used for SSO when the OIDC 'groups' claim is absent.
    """
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
    """User's email via the LDAP service account (to notify them)."""
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

# ── Anti-force-brute (persiste en base) ──

LOGIN_MAX_FAILS = 6           # attempts before lockout
LOGIN_WINDOW    = 900         # sliding window (15 min)
LOGIN_LOCK      = 900         # lockout duration (15 min)

def _login_locked(key):
    """Return the number of lockout seconds remaining, or 0."""
    row = get_db().execute("SELECT locked_until FROM login_attempts WHERE key=?", (key,)).fetchone()
    if not row or not row['locked_until']:
        return 0
    return max(0, int(row['locked_until'] - time.time()))

def _login_fail(key):
    now = time.time()
    db = get_db()
    row = db.execute("SELECT fails, first_at FROM login_attempts WHERE key=?", (key,)).fetchone()
    if not row or now - row['first_at'] > LOGIN_WINDOW:
        fails, first_at = 1, now
    else:
        fails, first_at = row['fails'] + 1, row['first_at']
    locked_until = now + LOGIN_LOCK if fails >= LOGIN_MAX_FAILS else 0
    db.execute(
        "INSERT INTO login_attempts (key, fails, first_at, locked_until) VALUES (?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET fails=excluded.fails, first_at=excluded.first_at, "
        "locked_until=excluded.locked_until",
        (key, fails, first_at, locked_until))
    db.commit()

def _login_reset(key):
    db = get_db()
    db.execute("DELETE FROM login_attempts WHERE key=?", (key,))
    db.commit()

def _client_ip():
    """Real visitor IP, not that of the last proxy.

    ProxyFix(x_for=1) only walks back ONE hop, but the chain is
    client → Cloudflare → Traefik → Next.js → Flask: request.remote_addr
    therefore always held the frontend container's IP (172.19.0.x), identical
    for everyone. Consequence: the global brute-force lock
    _login_locked(ip) triggered on the SUM of everyone's failures
    and blocked login for the entire portal for 15 min.

    Cf-Connecting-Ip is set by Cloudflare and normalized by Traefik's
    cloudflarewarp plugin; port 5000 is only reachable from
    Traefik and the docker bridge (see cronos-docker-restrict.service), so
    the header isn't spoofable from the outside.

    """
    # We only trust the header if its value is a valid IP: otherwise
    # a client reaching Traefik outside the Cloudflare path (LAN) could set
    # an arbitrary (or even non-IP) Cf-Connecting-Ip on each attempt and reset
    # to zero on a different lockout key, or poison the
    # chat-quota buckets that share the login_attempts table. An invalid value is
    # ignored and we fall back on the real connection address.
    def _valid_ip(v):
        try:
            ipaddress.ip_address(v)
            return v
        except ValueError:
            return None
    cf = _valid_ip((request.headers.get('Cf-Connecting-Ip') or '').strip())
    if cf:
        return cf
    fwd = (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
    if _valid_ip(fwd):
        return fwd
    return request.remote_addr or 'unknown'

# ── Ouverture de session ──

def _apply_session(username, fullname, is_admin, via_sso=False):
    session.clear()
    # session.clear() also erases 'csrf' (set up by _csrf_protect in
    # before_request, before the view calls _apply_session). Without
    # regenerating it here, the session leaves without a CSRF token: the first
    # subsequent request regenerates it via _csrf_protect, but if several requests
    # go out in parallel right after login (real case: the frontend
    # home page fires several fetches on mount), each
    # can independently regenerate a different token — the last response
    # to set its cookie "wins", and a token grabbed by a losing
    # request no longer matches the actually-stored cookie → 400 CSRF
    # invalid. Fixing it here eliminates the race window.
    session['csrf'] = secrets.token_urlsafe(32)
    session['username'] = username
    session['fullname'] = fullname
    session['is_admin'] = is_admin
    session['sso'] = via_sso
    # Authentication timestamp: without it, the signed cookie stayed valid
    # indefinitely. A stolen cookie (or a machine left open) gave permanent
    # access, and the is_admin flag frozen inside survived a removal from the
    # admin group on the directory side. See _session_expired().
    session['auth_at'] = int(time.time())
    # Registre serveur : le cookie signé ne porte que ce sid aléatoire ; la
    # ligne en base (user_sessions) permet de le révoquer à volonté. Un
    # sid est créé à chaque ouverture de session (login local/LDAP/SSO).
    sid = secrets.token_urlsafe(32)
    db = get_db()
    db.execute(
        "INSERT INTO user_sessions (sid, username, auth_at, expires_at, revoked, created_at) "
        "VALUES (?, ?, ?, ?, 0, ?)",
        (sid, username, session['auth_at'], session['auth_at'] + SESSION_MAX_AGE, time.time()))
    db.commit()
    session['sid'] = sid


def _revoke_current_session():
    """Révoque la session courante (logout) : le sid en base passe à revoked,
    la demande suivante la considérera expirée."""
    sid = session.get('sid')
    if not sid:
        return
    db = get_db()
    db.execute("UPDATE user_sessions SET revoked=1 WHERE sid=?", (sid,))
    db.commit()


def _revoke_user_sessions(username):
    """Révoque toutes les sessions actives d'un compte (verrouillage, admin).
    Ne lève pas si le compte n'a pas de session en base."""
    db = get_db()
    db.execute("UPDATE user_sessions SET revoked=1 WHERE username=?", (username,))
    db.commit()
