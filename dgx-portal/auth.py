"""Authentication guards and session lifetime.

Extracted from app.py on 28/08 — THIRD piece of the core, after db.py and
config.py, and the one without which no blueprint was possible: a route
module must import login_required/admin_required, yet those decorators
lived in app.py, which that same module cannot re-import.

Depends only on flask and the environment. url_for('login') and
url_for('index') are resolved AT CALL time, not at import: the matching
routes stay registered on the application in app.py, so nothing to pass here.
"""
import os
import time
from datetime import datetime
from functools import wraps

import hmac
import ipaddress
import re
import secrets

from flask import abort, flash, redirect, request, session, url_for
from ldap3 import ALL, SIMPLE, Connection, Server
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import escape_rdn

from config import (LDAP_BASE, LDAP_BIND_DN, LDAP_BIND_PW,
                    LDAP_LOGIN_ATTR, LDAP_URI, LDAP_USERS_DN)
from db import get_db, log_audit
from local_users import _local_user_is_admin

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
    # Server-side registry: the session is no longer limited by age alone, it
    # can be revoked at will (logout, locked account, admin).
    sid = session.get('sid')
    if not sid:
        # Session predating the registry (or test session without sid):
        # we keep expiry by age alone — it is not revocable but will
        # expire naturally. New sessions carry a sid and are. No mass
        # logout at migration time.
        return False
    row = get_db().execute(
        "SELECT revoked, expires_at FROM user_sessions WHERE sid=?", (sid,)).fetchone()
    if row is None or row['revoked'] or row['expires_at'] < time.time():
        return True
    return False


# ── CURRENT account state ─────────────────────────────────────────────────
# The session cookie only carries a name and a role copied at login.
# Without a re-read, deleting an account, disabling it, blocking it or
# demoting it had NO effect before the cookie expired (12 h by default):
# the bearer kept their access, admin included. These functions are called
# at every kept request.

def est_bloque(username):
    """Account refused at login, whatever its source."""
    if not username:
        return False
    return get_db().execute(
        "SELECT 1 FROM blocked_users WHERE username=?", (username,)).fetchone() is not None


def etat_compte(username):
    """(valid, is_admin, reason) re-read from database, never the one frozen at login.

    `is_admin` is **None** when the portal has no authoritative source on
    the role: the caller then keeps the one carried by the session. We
    never infer a demotion from missing data — inventing a « pas admin »
    from a missing row would break a legitimate administrator.

    `raison` ('bloque', 'desactive') serves the audit log and the admin
    diagnosis — never the HTTP answer: telling « ce compte est bloqué » to
    whoever presents the right password is still information not to give
    to a third party testing credentials.
    """
    db = get_db()
    if est_bloque(username):
        return False, False, 'bloque'
    row = db.execute("SELECT * FROM local_users WHERE username=?", (username,)).fetchone()
    if row is not None:
        # Local account: the portal is the sole master of the role and the state.
        if not row['enabled']:
            return False, False, 'desactive'
        return True, _local_user_is_admin(row), None
    # LDAP/SSO account: no local row, and we cannot query the directory at
    # every request (one LDAP bind per call). The role is thus read on the last
    # recorded login; if there is none, we have no opinion.
    src = db.execute(
        "SELECT last_is_admin FROM user_sources WHERE username=?", (username,)).fetchone()
    return True, (bool(src['last_is_admin']) if src is not None else None), None


def _revalide_session():
    """Discards the session if the account is no longer valid, else refreshes the role.

    Returns True if the session holds. A role change is rewritten in the
    cookie: this is the ONLY place authoritative on is_admin.
    """
    username = session.get('username')
    if not username:
        return True
    valide, is_admin, raison = etat_compte(username)
    if not valide:
        # One audit line per invalidated session: session.clear() replaces the
        # cookie, so the next requests have no name anymore.
        log_audit(username, 'session.invalidee', f'compte {raison} — session fermée')
        session.clear()
        return False
    if is_admin is not None and bool(session.get('is_admin')) != is_admin:
        log_audit(username, 'session.role_rafraichi',
                  f'is_admin {bool(session.get("is_admin"))} -> {is_admin}')
        session['is_admin'] = is_admin
    return True


def bloquer_compte(username, raison, par):
    """Refuses the account at login, cuts its sessions AND its API keys.

    The portal's only mechanism that works for an LDAP/SSO account: it has
    no row in local_users, hence no `enabled` to flip.

    The KEYS are part of the blocking since the 2026-10-02 audit: LiteLLM
    validates the keys itself and the portal is not in the API path, so a
    blocked account kept a full `Bearer` access (shared GPU consumed on its
    envelope). A block that leaves a live key has revoked nothing.
    Trade-off: « débloquer » does not restore the keys.

    Returns a report: {'sessions', 'keys_revoked', 'keys_failed', 'keys'}.
    """
    db = get_db()
    db.execute(
        "INSERT INTO blocked_users (username, reason, blocked_by, blocked_at) VALUES (?,?,?,?) "
        "ON CONFLICT(username) DO UPDATE SET reason=excluded.reason, "
        "blocked_by=excluded.blocked_by, blocked_at=excluded.blocked_at",
        (username, (raison or None), (par or '?'), datetime.now().isoformat()))
    db.commit()
    # The admin role is cached 60 s for /internal/authcheck: without a purge, a
    # blocked admin's key would still cross maintenance for a minute.
    _admin_username_cache.pop(username, None)
    revoquees = _revoke_user_sessions(username)
    from user_lifecycle import revoquer_cles_compte        # late import: cycle auth <-> user_lifecycle
    cles_ok, cles_ko, cles_total = revoquer_cles_compte(username, par, 'blocage du compte')
    log_audit(par, 'user.block',
              f'{username}' + (f' — motif : {raison}' if raison else '')
              + f' — {revoquees} session(s) révoquée(s), '
              + f'{cles_ok}/{cles_total} clé(s) API révoquée(s)'
              + (f', {cles_ko} ÉCHEC(S) À RÉVOQUER' if cles_ko else ''))
    return {'sessions': revoquees, 'keys_revoked': cles_ok,
            'keys_failed': cles_ko, 'keys': cles_total}


def debloque_compte(username, par):
    db = get_db()
    n = db.execute("DELETE FROM blocked_users WHERE username=?", (username,)).rowcount
    db.commit()
    # Same reason as in bloquer_compte: the cached role must not delay the
    # return to normal by a minute.
    _admin_username_cache.pop(username, None)
    if n:
        log_audit(par, 'user.unblock', username)
    return n


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if _session_expired() or not _revalide_session():
            session.clear()
        if 'username' not in session:
            if _is_api_request():
                abort(401)
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    # Marker read at runtime by the route guard test: @wraps erases every trace
    # of the decorator, so without it one would have to parse the source —
    # fragile, and blind to a route registered other than as a literal.
    decorated._garde = 'login'
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if _session_expired() or not _revalide_session():
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


# ── Authentication foundation, brought over from app.py on 28/08 ────────────
# CSRF, fallback login, LDAP, anti-brute-force and session opening were
# scattered in the monolith under four different banners. They form ONE
# topic though, and the admin blueprint needs them.
#
# _csrf_protect and _inject_csrf lost their @app.* decorators: they are
# registered from app.py (before_request / context_processor), otherwise
# one would have to import the application here — the cycle avoided since db.py.
#
# The fallback login MECHANICS is moved as-is, without a single logic
# line changed: LDAP and SSO being off, it is the only
# access to the platform.

# ── Credential validation ──

USERNAME_RE = re.compile(r'^[a-zA-Z0-9._-]{1,64}$')

# ── Session CSRF token ──

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

# ── Admin status ──

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

    BLOCKING counts as an admin loss (audit of 2026-10-02): a blocked
    account is refused at login, but its `enabled` stays at 1 — without
    this test, its API key kept crossing maintenance. `est_bloque` is the
    same predicate as the login one, so the two paths cannot diverge anymore.
    """
    try:
        if est_bloque(username):
            return False
        row = get_db().execute(
            "SELECT * FROM local_users WHERE username=? AND enabled=1", (username,)).fetchone()
        if not row:
            return False
        if row['is_admin']:
            return True
        groupe = get_db().execute(
            "SELECT is_admin FROM user_groups WHERE name=?", (row['group_name'],)).fetchone()
        return bool(groupe and groupe['is_admin'])
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
        user_dn = f"{LDAP_LOGIN_ATTR}={escape_rdn(username)},{LDAP_USERS_DN},{LDAP_BASE}"
        conn = Connection(server, user=user_dn, password=password,
                          authentication=SIMPLE, auto_bind=True)
        conn.search(
            search_base=f"{LDAP_USERS_DN},{LDAP_BASE}",
            search_filter=f"({LDAP_LOGIN_ATTR}={escape_filter_chars(username)})",
            attributes=['cn', 'displayName', 'memberOf']
        )
        if not conn.entries:
            conn.unbind()
            return False, False, username
        entry = conn.entries[0]
        # displayName (e.g. « Alice Dupont ») when present; else cn, else the
        # identifier. Authentik stores displayName base64 for accents, ldap3
        # already decodes it.
        if hasattr(entry, 'displayName') and getattr(entry, 'displayName'):
            fullname = str(entry.displayName)
        elif hasattr(entry, 'cn'):
            fullname = str(entry.cn)
        else:
            fullname = username
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
        conn.search(search_base=f"{LDAP_USERS_DN},{LDAP_BASE}",
                    search_filter=f"({LDAP_LOGIN_ATTR}={escape_filter_chars(username)})",
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
        conn.search(search_base=f"{LDAP_USERS_DN},{LDAP_BASE}",
                    search_filter=f"({LDAP_LOGIN_ATTR}={escape_filter_chars(username)})", attributes=['mail'])
        email = None
        if conn.entries and hasattr(conn.entries[0], 'mail') and conn.entries[0].mail:
            email = str(conn.entries[0].mail)
        conn.unbind()
        return email or None
    except Exception:
        return None

def ldap_resolve_sso_identity(sub=None, email=None):
    """Resolves the canonical *username* (cn) for an SSO login, from the
    authoritative LDAP directory — never from user-editable OIDC claims.

    The OIDC token's `preferred_username`/`nickname` are modifiable by the user
    in many IdPs; trusting them would let an SSO account claim another account's
    identity (and its data, API keys, admin rights). So the callback binds on
    immutable/authoritative signals instead:

      - `sub`   : the IdP's stable per-user identifier. In Authentik this
                 corresponds to the LDAP `uid` (the long hash, not the cn).
      - `email` : the verified email, unique in the directory and present in
                 both the token (email claim) and LDAP (`mail`).

    Returns (username, is_admin, fullname) or (None, False, None) when the
    directory does not recognise the identity — the caller then REFUSES the
    login (fail closed) rather than trusting the token's own username.
    """
    if not (LDAP_BIND_DN and LDAP_BIND_PW):
        return None, False, None
    try:
        conn = Connection(Server(LDAP_URI, get_info=ALL), user=LDAP_BIND_DN,
                          password=LDAP_BIND_PW, authentication=SIMPLE, auto_bind=True)
        entry = None
        # 1) bind by the immutable sub (Authentik LDAP maps it to `uid`).
        if sub and re.match(r'^[a-zA-Z0-9._:@-]{1,256}$', sub):
            conn.search(search_base=f"{LDAP_USERS_DN},{LDAP_BASE}",
                        search_filter=f"(uid={escape_filter_chars(sub)})",
                        attributes=['cn', 'displayName', 'mail', 'memberOf'])
            if conn.entries:
                entry = conn.entries[0]
        # 2) fall back to the verified email (unique directory key).
        if entry is None and email and '@' in email:
            conn.search(search_base=f"{LDAP_USERS_DN},{LDAP_BASE}",
                        search_filter=f"(mail={escape_filter_chars(email)})",
                        attributes=['cn', 'displayName', 'mail', 'memberOf'])
            if conn.entries:
                entry = conn.entries[0]
        if entry is None:
            conn.unbind()
            return None, False, None
        username = str(entry.cn) if hasattr(entry, 'cn') and entry.cn else None
        if not username or not USERNAME_RE.match(username):
            conn.unbind()
            return None, False, None
        fullname = (str(entry.displayName) if hasattr(entry, 'displayName') and getattr(entry, 'displayName')
                    else username)
        groups = [str(g) for g in entry.memberOf] if hasattr(entry, 'memberOf') else []
        is_admin = any(_is_admin_group(g) for g in groups)
        conn.unbind()
        return username, is_admin, fullname
    except Exception:
        return None, False, None


# ── Anti-brute-force (persisted in database) ──

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
    if locked_until:
        # A lockout is a security event: it must remain a trace on the admin side
        # (the counter itself expires at the end of the window).
        log_audit(key.split('|')[-1] if '|' in key else key, 'login.verrouillage',
                  f'{key} — {fails} échecs, verrouillé {int(LOGIN_LOCK // 60)} min')
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

# ── Session opening ──

def _apply_session(username, fullname, is_admin, via_sso=False):
    # The client's CSRF token is KEPT, not replaced: it is the one holding the
    # value the page already has in memory.
    #
    # `session.clear()` erases it, and regenerating it here (what the code did
    # until 2026-09-14) changed the token BEHIND the browser's back: the page
    # kept sending the old one, and every kept POST answered 400 « CSRF token
    # manquant ou invalide ». Reported symptom: « je me déconnecte après un
    # login SSO et j'obtiens Bad Request ». A full reload (what `/login` does
    # after a local login) masked the problem; the SSO, whose return does not
    # always go through a fresh document, exposed it.
    #
    # Why keeping the token relaxes nothing: it lives in a SIGNED and HttpOnly
    # cookie, so neither readable nor forgeable by a page — CSRF fixation
    # assumes an attacker able to impose a value, which the signature forbids.
    # Protection against session fixation remains assured by the fresh `sid`
    # created below, which is the real revocable identifier.
    #
    # If there is no token yet (new browser, missing cookie), we create one:
    # the session must never leave without one, else several parallel requests
    # would each mint one and the last to set its cookie would win — the token
    # memorized by the page would no longer match. This is the race this block
    # already avoided, and it keeps avoiding it.
    csrf = session.get('csrf')
    session.clear()
    session['csrf'] = csrf or secrets.token_urlsafe(32)
    session['username'] = username
    session['fullname'] = fullname
    session['is_admin'] = is_admin
    session['sso'] = via_sso
    # Authentication timestamp: without it, the signed cookie stayed valid
    # indefinitely. A stolen cookie (or a machine left open) gave permanent
    # access, and the is_admin flag frozen inside survived a removal from the
    # admin group on the directory side. See _session_expired().
    session['auth_at'] = int(time.time())
    # Server-side registry: the signed cookie carries only this random sid; the
    # database row (user_sessions) allows revoking it at will. A sid is created
    # at every session opening (local/LDAP/SSO login).
    sid = secrets.token_urlsafe(32)
    db = get_db()
    db.execute(
        "INSERT INTO user_sessions (sid, username, auth_at, expires_at, revoked, created_at, ip, user_agent) "
        "VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
        (sid, username, session['auth_at'], session['auth_at'] + SESSION_MAX_AGE, time.time(),
         _client_ip(), (request.headers.get('User-Agent') or '')[:400]))
    db.commit()
    session['sid'] = sid


def completer_origine_session():
    """Fills in the IP and user-agent of the CURRENT session when missing.

    Sessions opened before these columns were added (2026-09-13) have
    none: the person saw a dash instead of « ce navigateur, cette IP »,
    that is exactly the information that lets one spot a session they do
    not recognize. We fill in ONLY the row whose sid is the one of the
    calling cookie — thus their own — and only when the value is absent:
    overwriting an already noted IP would erase the trace we want to keep,
    and a stolen cookie would rewrite its own origin.
    """
    sid, username = session.get('sid'), session.get('username')
    if not sid or not username:
        return 0
    db = get_db()
    n = db.execute(
        "UPDATE user_sessions SET ip=?, user_agent=? WHERE sid=? AND username=? "
        "AND (ip IS NULL OR user_agent IS NULL)",
        (_client_ip(), (request.headers.get('User-Agent') or '')[:400], sid, username)).rowcount
    db.commit()
    return n


def _revoke_current_session():
    """Revokes the current session (logout): the sid in database moves to
    revoked, the next request will consider it expired."""
    sid = session.get('sid')
    if not sid:
        return
    db = get_db()
    db.execute("UPDATE user_sessions SET revoked=1 WHERE sid=?", (sid,))
    db.commit()
    return 1


def _revoke_user_sessions(username):
    """Revokes all active sessions of an account (lockout, admin).
    Does not raise if the account has no session in database."""
    db = get_db()
    n = db.execute(
        "UPDATE user_sessions SET revoked=1 WHERE username=? AND revoked=0", (username,)).rowcount
    db.commit()
    return n
